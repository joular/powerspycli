#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021-2026 Adel Noureddine <adel.noureddine@outlook.com>
#
# Contributors:
# - Ali Bouhjira, 2025.
#
# Authors of the original 2014 version (which we forked from):
# - Patrick Marlier, 2014
# - 2014 Mascha Kurpicz, 2014
#
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the
# GNU Lesser General Public License v3.0 or later (LGPL-3.0-or-later)
# which accompanies this distribution, and is available at:
# https://www.gnu.org/licenses/lgpl-3.0.en.html

# To disable Python compile cache
import sys
sys.dont_write_bytecode = True

# For connecting and collecting data from the power meter
import logging
import os
import socket
import subprocess
import struct  # conversion of type
import re      # matching responses
import math    # sqrt
import signal  # signal handler
import sys     # system exit and sys.stdout.write
import time    # sleep/time
import errno   # IOError numbers
import codecs  # for hex decoder
import csv     # for csv handling
import contextlib
import threading

# All powerspy commands
CMD_ID = '?'
CMD_CAPTURE_LENGTH = 'L'
CMD_TRIGGER_CONF = 'T'
CMD_RESET = 'R'
CMD_EEPROM_READ = 'V'
CMD_EEPROM_WRITE = 'W'
CMD_START = 'S'
CMD_CANCEL = 'C'
CMD_FREQUENCY = 'F'
CMD_ASCII = 'A'
CMD_BINARY = 'B'
CMD_RT = 'J'
CMD_RT_STOP = 'Q'
CMD_RTC_SET = 'E'
CMD_RTC_GET = 'G'
CMD_LOG_PERIOD = 'M'
CMD_LOG_START = 'O'
CMD_LOG_STOP = 'P'
CMD_FILE_LIST = 'U'
CMD_FILE_DEL = 'Y'
CMD_FILE_GET = 'X'

# Generic responses
CMD_OK = 'K'
CMD_FAILED = 'Z'

# Variable to show all metrisc or not
allmetrics = False

# Variable to identify if GUI or CLI running
is_gui = False

# Constants
DEFAULT_TIMEOUT = 3.0 # secs (float allowed, timeout to receive response from PowerSpy, except in realtime mode)

decode_hex = codecs.getdecoder("hex_codec")

#-------------------------------------------------------------------------------------------
# Bluetooth transports
#-------------------------------------------------------------------------------------------

MACOS_SPP_UUID = 0x1101
MACOS_RFCOMM_CHANNEL_MIN = 1
MACOS_RFCOMM_CHANNEL_MAX = 30
_MACOS_RFCOMM_DELEGATE_CLASS = None

IOBLUETOOTH_STATUS_NAMES = {
  -536870212: "general I/O error",
  -536870208: "Bluetooth device unavailable",
  -536870207: "Bluetooth permission denied",
  -536870203: "RFCOMM channel is already in use",
  -536870187: "Bluetooth device is busy",
  -536870186: "Bluetooth operation timed out",
  -536870185: "Bluetooth device is offline",
  -536870184: "Bluetooth device is not ready",
  -536870163: "Bluetooth device is not responding",
}


def _macos_worker_autorelease_pool():
  """Give PyObjC calls made by a secondary Python thread a Cocoa pool."""
  if sys.platform != "darwin":
    return contextlib.nullcontext()
  try:
    import objc
  except ImportError:
    # The transport loader will provide the actionable dependency error.
    return contextlib.nullcontext()
  return objc.autorelease_pool()


def _describe_iobluetooth_status(status):
  try:
    numeric_status = int(status)
  except (TypeError, ValueError):
    return str(status)
  name = IOBLUETOOTH_STATUS_NAMES.get(numeric_status, "unknown IOBluetooth error")
  return "%s (%s, 0x%08X)" % (name, numeric_status, numeric_status & 0xffffffff)


def _rfcomm_open_hint(status):
  if status == -536870207:
    return "Allow Bluetooth access for your terminal or Python in System Settings > Privacy & Security > Bluetooth."
  if status in (-536870203, -536870187):
    return "Close any other PowerSpy or serial-port client, wait a few seconds, and retry."
  return "Make sure no other client is using PowerSpy, keep it nearby and powered on, then retry."


def _load_macos_bluetooth_modules():
  """Load the macOS-only PyObjC modules without affecting other platforms."""
  try:
    import Foundation
    import IOBluetooth
  except ImportError as error:
    raise ImportError(
      "macOS Bluetooth support requires PyObjC. Install it with: "
      "python3 -m pip install pyobjc-framework-IOBluetooth"
    ) from error
  return IOBluetooth, Foundation


def _create_macos_rfcomm_delegate(foundation, owner):
  """Create the Objective-C delegate that forwards RFCOMM events to owner."""
  global _MACOS_RFCOMM_DELEGATE_CLASS
  import objc

  if _MACOS_RFCOMM_DELEGATE_CLASS is None:
    class PowerSpyRFCOMMDelegate(foundation.NSObject):
      def initWithOwner_(self, delegate_owner):
        self = objc.super(PowerSpyRFCOMMDelegate, self).init()
        if self is not None:
          self.owner = delegate_owner
        return self

      def sdpQueryComplete_status_(self, device, status):
        self.owner._handle_sdp_complete(status)

      def rfcommChannelData_data_length_(self, channel, data, length):
        self.owner._handle_data(bytes(data[:length]))

      def rfcommChannelClosed_(self, channel):
        self.owner._handle_closed()

    _MACOS_RFCOMM_DELEGATE_CLASS = PowerSpyRFCOMMDelegate

  return _MACOS_RFCOMM_DELEGATE_CLASS.alloc().initWithOwner_(owner)


class MacOSRFCOMMSocket:
  """A small socket-compatible wrapper around macOS IOBluetooth RFCOMM."""

  def __init__(self, bluetooth_module=None, foundation_module=None, delegate_factory=None):
    if bluetooth_module is None or foundation_module is None:
      bluetooth_module, foundation_module = _load_macos_bluetooth_modules()
    self.bluetooth = bluetooth_module
    self.foundation = foundation_module
    self.timeout = DEFAULT_TIMEOUT
    self.device = None
    self.channel = None
    self.closed = False
    self.incoming = bytearray()
    self.condition = threading.Condition()
    self.sdp_complete = False
    self.sdp_status = None
    factory = delegate_factory or _create_macos_rfcomm_delegate
    self.delegate = factory(self.foundation, self)

  def _handle_sdp_complete(self, status):
    self.sdp_status = status
    self.sdp_complete = True

  def _handle_data(self, data):
    with self.condition:
      self.incoming.extend(data)
      self.condition.notify_all()

  def _handle_closed(self):
    with self.condition:
      self.closed = True
      self.condition.notify_all()

  def _pump_run_loop(self, seconds):
    seconds = max(0.0, min(seconds, 0.05))
    until = self.foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds)
    self.foundation.NSRunLoop.currentRunLoop().runMode_beforeDate_(
      self.foundation.NSDefaultRunLoopMode, until
    )

  def _wait_until(self, predicate, timeout):
    deadline = None if timeout is None else time.monotonic() + timeout
    while not predicate():
      if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          return False
      else:
        remaining = 0.05
      self._pump_run_loop(remaining)
    return True

  def _discover_rfcomm_channel(self, fallback_channel):
    """Resolve the Serial Port Profile channel, retaining channel 1 compatibility."""
    self.sdp_complete = False
    self.sdp_status = None
    uuid = self.bluetooth.IOBluetoothSDPUUID.uuid16_(MACOS_SPP_UUID)
    try:
      result = self.device.performSDPQuery_uuids_(self.delegate, [uuid])
    except (AttributeError, TypeError):
      logging.debug("Targeted SDP queries are unavailable; using RFCOMM channel %d", fallback_channel)
      return fallback_channel

    if result != 0:
      logging.debug("Cannot start SDP query (status %s); using RFCOMM channel %d", result, fallback_channel)
      return fallback_channel

    if not self._wait_until(lambda: self.sdp_complete, self.timeout):
      logging.debug("SDP query timed out; using RFCOMM channel %d", fallback_channel)
      return fallback_channel
    if self.sdp_status != 0:
      logging.debug("SDP query failed (status %s); using RFCOMM channel %d", self.sdp_status, fallback_channel)
      return fallback_channel

    service = self.device.getServiceRecordForUUID_(uuid)
    if service is None:
      logging.debug("No Serial Port Profile service found; using RFCOMM channel %d", fallback_channel)
      return fallback_channel

    # PyObjC does not expose getRFCOMMChannelID_'s output pointer reliably on
    # all supported versions. Checking the valid channel range is equivalent
    # and uses the service record's public channel matcher.
    for channel_id in range(MACOS_RFCOMM_CHANNEL_MIN, MACOS_RFCOMM_CHANNEL_MAX + 1):
      if service.usesRFCOMMChannelID_(channel_id):
        logging.debug("Discovered PowerSpy Serial Port Profile on RFCOMM channel %d", channel_id)
        return channel_id

    logging.debug("Serial Port Profile has no RFCOMM channel; using channel %d", fallback_channel)
    return fallback_channel

  def connect(self, address):
    try:
      mac_address, fallback_channel = address
      self.closed = False
      self.device = self.bluetooth.IOBluetoothDevice.deviceWithAddressString_(mac_address)
      if self.device is None:
        raise OSError("macOS could not create a Bluetooth device for %s" % mac_address)
      if not self.device.isPaired():
        raise PermissionError(
          "PowerSpy %s is not paired. Pair it in System Settings > Bluetooth before connecting."
          % mac_address
        )

      channel_id = self._discover_rfcomm_channel(fallback_channel)
      try:
        result, self.channel = self.device.openRFCOMMChannelSync_withChannelID_delegate_(
          None, channel_id, self.delegate
        )
      except TypeError:
        # Compatibility with older PyObjC selector metadata.
        result, self.channel = self.device.openRFCOMMChannelSync_withChannelID_delegate_(
          channel_id, self.delegate
        )

      if result != 0 or self.channel is None:
        self.channel = None
        raise OSError(
          "macOS could not open RFCOMM channel %d: %s. %s"
          % (channel_id, _describe_iobluetooth_status(result), _rfcomm_open_hint(result))
        )
      self.channel.setDelegate_(self.delegate)
    except OSError:
      raise
    except Exception as error:
      raise OSError("macOS Bluetooth connection failed: %s" % error) from error

  def sendall(self, data):
    if self.channel is None or self.closed:
      raise OSError(errno.ENOTCONN, "Bluetooth RFCOMM channel is not connected")
    if not isinstance(data, (bytes, bytearray)):
      raise TypeError("data must be bytes-like")

    data = bytes(data)
    mtu = int(self.channel.getMTU()) or len(data) or 1
    for offset in range(0, len(data), mtu):
      chunk = data[offset:offset + mtu]
      try:
        result = self.channel.writeSync_length_(chunk, len(chunk))
      except Exception as error:
        raise OSError("RFCOMM write failed: %s" % error) from error
      if result != 0:
        raise OSError("RFCOMM write failed (IOBluetooth status %s)" % result)

  def recv(self, size):
    if size < 0:
      raise ValueError("negative buffersize in recv")
    if size == 0:
      return b""
    if self.channel is None:
      raise OSError(errno.ENOTCONN, "Bluetooth RFCOMM channel is not connected")

    try:
      ready = self._wait_until(lambda: bool(self.incoming) or self.closed, self.timeout)
    except Exception as error:
      raise OSError("RFCOMM receive failed: %s" % error) from error
    if not ready:
      raise socket.timeout("Bluetooth RFCOMM receive timed out")
    with self.condition:
      if not self.incoming and self.closed:
        raise ConnectionResetError(errno.ECONNRESET, "PowerSpy closed the Bluetooth connection")
      data = bytes(self.incoming[:size])
      del self.incoming[:size]
      return data

  def settimeout(self, timeout):
    if timeout is not None and not isinstance(timeout, (int, float)):
      raise TypeError("timeout must be a number or None")
    if timeout is not None and timeout < 0:
      raise ValueError("timeout cannot be negative")
    self.timeout = timeout

  def gettimeout(self):
    return self.timeout

  def close(self):
    channel, device = self.channel, self.device
    self.channel = None
    self.device = None
    self._handle_closed()
    if channel is not None:
      try:
        channel.setDelegate_(None)
      except (AttributeError, TypeError):
        pass
      try:
        channel.closeChannel()
      except Exception:
        pass
    if device is not None:
      try:
        device.closeConnection()
      except Exception:
        pass


def create_rfcomm_socket():
  """Create the platform's RFCOMM transport."""
  if sys.platform == "darwin":
    return MacOSRFCOMMSocket()
  try:
    return socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
  except AttributeError as error:
    raise OSError(
      "This Python installation does not provide Bluetooth RFCOMM sockets on %s."
      % sys.platform
    ) from error

#-------------------------------------------------------------------------------------------
# GUI class
#-------------------------------------------------------------------------------------------

class PSCGUI:
  def __init__(self, powerspy_):
    self.powerspy = powerspy_
    self.root = ttk.Window(themename="flatly")
    self.root.title("PowerSpyGUI")

    # Initialize theme (default is light)
    self.current_theme = "flatly"  # Light theme
    self.dark_theme = "darkly"  # Dark theme

    # Main container frame
    self.main_frame = ttk.Frame(self.root, padding="10")
    self.main_frame.pack(fill=tk.BOTH, expand=True)

    # Title
    self.title_label = ttk.Label(self.main_frame, text="PowerSpyCli GUI", font=("Arial", 14))
    self.title_label.grid(row=0, column=0, padx=5, pady=5, sticky=tk.E)

    # Theme Toggle Button (with icon)
    self.theme_button = ttk.Button(
      self.main_frame,
      text="🌙",  # Moon icon for dark theme
      command=self.toggle_theme,
      bootstyle=(LINK, OUTLINE),
      width=3,
      cursor="hand2"
    )
    self.theme_button.grid(row=0, column=1, padx=5, pady=5, sticky=tk.E)

    # MAC Address Entry
    self.mac_frame = ttk.Labelframe(self.main_frame, text="MAC Address", padding="10")
    self.mac_frame.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

    ttk.Label(self.mac_frame, text="Enter MAC Address:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
    self.mac_entry = ttk.Entry(self.mac_frame, width=25)
    self.mac_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

    # File Options
    self.file_path = tk.StringVar()
    self.file_frame = ttk.Labelframe(self.main_frame, text="File Options", padding="10")
    self.file_frame.grid(row=2, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

    self.write_to_file = tk.BooleanVar(value=False)
    self.file_checkbox = ttk.Checkbutton(
      self.file_frame, text="Write to file", variable=self.write_to_file, command=self.toggle_file_selector
    )
    self.file_checkbox.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

    self.file_selector_button = ttk.Button(
      self.file_frame, text="Select File", command=self.select_file, bootstyle=INFO, state=DISABLED, cursor="hand2"
    )
    self.file_selector_button.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

    self.file_label = ttk.Label(self.file_frame, text="File: none", bootstyle=SECONDARY)
    self.file_label.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

    # Control Buttons
    self.control_frame = ttk.Frame(self.main_frame, padding="10")
    self.control_frame.grid(row=3, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

    self.start_button = ttk.Button(
      self.control_frame, text="Start", command=self.start_process, bootstyle=SUCCESS, width=10, cursor="hand2"
    )
    self.start_button.grid(row=0, column=0, padx=5, pady=5, sticky=tk.EW)

    self.stop_button = ttk.Button(
      self.control_frame, text="Stop", command=self.stop_process, bootstyle=DANGER, width=10, state=DISABLED,
      cursor="hand2"
    )
    self.stop_button.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

    # Status
    self.status_frame = ttk.Labelframe(self.main_frame, text="Status", padding="10")
    self.status_frame.grid(row=4, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

    self.status_label = ttk.Label(self.status_frame, text="Status: Idle", bootstyle=INFO)
    self.status_label.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

    #  Data Fields
    self.data_frame = ttk.Labelframe(self.main_frame, text="Power values", padding="10")
    self.data_frame.grid(row=5, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

    self.timestamp_label = ttk.Label(self.data_frame, text="Timestamp:", bootstyle=SECONDARY)
    self.timestamp_value = ttk.Label(self.data_frame, text="", bootstyle=SECONDARY)
    self.power_label = ttk.Label(self.data_frame, text="Power (Watts):", bootstyle=SECONDARY)
    self.power_value = ttk.Label(self.data_frame, text="", bootstyle=INFO)

    self.timestamp_label.grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
    self.timestamp_value.grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)
    self.power_label.grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
    self.power_value.grid(row=2, column=1, padx=5, pady=5, sticky=tk.W)

    #  Data Fields
    self.info_frame = ttk.Labelframe(self.main_frame, text="Info", padding="10")
    self.info_frame.grid(row=6, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

    self.info_label = ttk.Label(self.info_frame, text="PowerSpyCli GUI - Copyright (c) 2021-2026 Adel Noureddine",
                                bootstyle=SECONDARY)
    self.info_label.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

    # Clickable link label
    self.link_label = ttk.Label(self.info_frame, text="https://github.com/joular/powerspycli", bootstyle=SECONDARY,
                                cursor="hand2")
    self.link_label.grid(row=1, column=0, padx=10, pady=5, sticky=tk.E)
    self.link_label.bind("<Button-1>", lambda e: self.open_link("https://github.com/joular/powerspycli"))

  def toggle_theme(self):
    """Toggle between light and dark themes."""
    if self.current_theme == "flatly":
      self.current_theme = "darkly"
      self.theme_button.config(text="☀️")  # Sun icon for light theme
    else:
      self.current_theme = "flatly"
      self.theme_button.config(text="🌙")  # Moon icon for dark theme

    # Update the theme
    self.root.style.theme_use(self.current_theme)

  def update_data_fields(self, timestamp, power):
    """Update displayed timestamp and power values."""
    self.timestamp_value.config(text=str(timestamp))
    self.power_value.config(text=str(power))

  def _schedule(self, callback, *args):
    """Marshal a worker-thread callback onto Tk's main thread."""
    self.root.after(0, callback, *args)

  def start_process(self):
    mac_address = self.mac_entry.get()
    if not is_valid_mac(mac_address):
      messagebox.showerror("Invalid MAC Address", "Please enter a valid MAC address.")
      return

    if getattr(self, "thread", None) is not None and self.thread.is_alive():
      return

    self.start_button.config(state=DISABLED)
    self.stop_button.config(state=NORMAL)
    self.mac_entry.config(state=DISABLED)
    self.status_label.config(text="Status: Connecting...", bootstyle=WARNING)
    self.root.update_idletasks()

    file_path = ""
    if self.write_to_file.get():
      file_path = self.file_path.get()
      if not file_path:
        timestamp = int(time.time())
        file_path = f"powerspy-{timestamp}.csv"
        self.file_path.set(file_path)
        messagebox.showinfo("Default File Created", f"No file selected. Using default file: {file_path}")
      self.file_label.config(text=f"Saving to: {file_path}", bootstyle=SUCCESS)

    self.powerspy.running = True
    self.powerspy.last_error = None
    worker = self._run_macos_cli_capture if sys.platform == "darwin" else self._run_capture
    self.thread = threading.Thread(
      target=worker, args=(mac_address, file_path), daemon=True
    )
    self.thread.start()

  def _run_macos_cli_capture(self, mac_address, file_path):
    """Run macOS IOBluetooth on a child process's main thread."""
    command = [sys.executable, "-u", os.path.abspath(__file__), "-m", mac_address]
    if file_path:
      command.extend(["-f", file_path])

    process = None
    diagnostics = []
    saw_measurement = False
    try:
      process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
      )
      self._cli_process = process
      self._cli_interrupt_sent = False
      if not self.powerspy.running:
        self._interrupt_cli_process()
      for line in process.stdout:
        text = line.strip()
        match = re.match(
          r"^(\d+(?:\.\d+)?)\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$",
          text,
        )
        if match:
          if not saw_measurement:
            saw_measurement = True
            self._schedule(self._capture_started, mac_address)
          self._schedule(self.update_data_fields, match.group(1), match.group(2))
        elif text and not text.startswith("Please wait while connecting"):
          diagnostics.append(text)

        if not self.powerspy.running:
          self._interrupt_cli_process()
          break

      try:
        return_code = process.wait(timeout=10)
      except subprocess.TimeoutExpired:
        process.terminate()
        return_code = process.wait(timeout=5)
      if self.powerspy.running and return_code != 0:
        message = diagnostics[-1] if diagnostics else "PowerSpy CLI capture failed."
        self.powerspy.last_error = message
        self._schedule(self._connection_failed, message)
    except Exception as error:
      self.powerspy.last_error = "PowerSpy GUI capture failed: %s" % error
      self._schedule(self._connection_failed, self.powerspy.last_error)
    finally:
      if process is not None and process.poll() is None:
        try:
          process.send_signal(signal.SIGINT)
          process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
          process.terminate()
      self._cli_process = None
      self.powerspy.running = False
      self._schedule(self._capture_finished)

  def _interrupt_cli_process(self):
    process = getattr(self, "_cli_process", None)
    if (process is not None and process.poll() is None
        and not getattr(self, "_cli_interrupt_sent", False)):
      try:
        process.send_signal(signal.SIGINT)
        self._cli_interrupt_sent = True
      except OSError:
        pass

  def _run_capture(self, mac_address, file_path):
    """Own connection and capture on one thread for macOS run-loop delivery."""
    with _macos_worker_autorelease_pool():
      self._run_capture_with_transport(mac_address, file_path)

  def _run_capture_with_transport(self, mac_address, file_path):
    try:
      if self.powerspy.connect((mac_address, 1)) != 0:
        message = self.powerspy.last_error or "Connection error. Check the device and retry."
        self._schedule(self._connection_failed, message)
        return
      if not self.powerspy.running:
        return
      if not self.powerspy.init():
        self._schedule(self._connection_failed, "Device cannot be initialized. Check the connection and retry.")
        return
      if not self.powerspy.running:
        return
      self._schedule(self._capture_started, mac_address)
      self.powerspy.rt_capture(file_path, measurement_callback=self._measurement_received)
    except Exception as error:
      self.powerspy.last_error = "PowerSpy operation failed: %s" % error
      self._schedule(self._connection_failed, self.powerspy.last_error)
    finally:
      self.powerspy.close()
      self._schedule(self._capture_finished)

  def _capture_started(self, mac_address):
    self.status_label.config(text=f"Status: Listening to PowerSpy [{mac_address}]", bootstyle=SUCCESS)

  def _connection_failed(self, message):
    self.status_label.config(text=message, bootstyle=DANGER)

  def _measurement_received(self, timestamp, power):
    self._schedule(self.update_data_fields, f"{timestamp:0.0f}", f"{power:.3f}")

  def _capture_finished(self):
    was_stopped = not self.powerspy.running
    self.powerspy.running = False
    self.start_button.config(state=NORMAL)
    self.stop_button.config(state=DISABLED)
    self.mac_entry.config(state=NORMAL)
    if self.powerspy.last_error:
      self.status_label.config(text=self.powerspy.last_error, bootstyle=DANGER)
    elif was_stopped and "error" not in self.status_label.cget("text").lower():
      self.status_label.config(text="Status: Stopped", bootstyle=DANGER)

  def stop_process(self):
    self.status_label.config(text="Status: Stopping...", bootstyle=WARNING)
    self.stop_button.config(state=DISABLED)
    self.powerspy.running = False
    self._interrupt_cli_process()

  def toggle_file_selector(self):
    if self.write_to_file.get():
      self.file_selector_button.config(state=NORMAL)
    else:
      self.file_selector_button.config(state=DISABLED)

  def select_file(self):
    file_path = filedialog.asksaveasfilename(defaultextension=".csv",
                                             filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
    if file_path:
      self.file_path.set(file_path)

  def run(self):
    self.root.mainloop()

#-------------------------------------------------------------------------------------------
# PowerSpy class
#-------------------------------------------------------------------------------------------

class PowerSpy:
  def __init__ (self, socket_factory=create_rfcomm_socket):
    self.sock = None
    self.socket_factory = socket_factory
    self.last_error = None
    self.status = None
    self.pll_locked = None
    self.trigger_status = None
    self.sw_version = None
    self.hw_version = None
    self.hw_serial = None
    self.uscale_factory = self.iscale_factory = self.pscale_factory = None
    self.uscale_current = self.iscale_current = self.pscale_current = None
    self.frequency = None
    self.max_avg_period = None
    self.running = True

  def connect(self, address):
    if self.sock != None:
      logging.warning("Already connected")
      return 1

    # Try to connect 3 times before raising error
    for attempt in range(3):
      self.last_error = None
      try:
        self.sock = self.socket_factory()
        logging.debug("Connecting to %s..." % str(address))
        self.sock.connect(address)
        # Should not set timeout before connect (connect may require more time)
        self.sock.settimeout(DEFAULT_TIMEOUT)
        return 0
      except (ImportError, OSError, RuntimeError) as error:
        self.last_error = str(error)
        logging.error("Cannot connect to %s (%s)" % (str(address), str(error)))
        if self.sock is not None:
          try:
            self.sock.close()
          except Exception:
            pass
          self.sock = None
        if isinstance(error, (ImportError, PermissionError)):
          break
        if attempt < 2:
          time.sleep(3) # Sleep 3 seconds before retrying
    return 1

  def sendCmd(self, c):
    assert(self.sock != None)
    # All powerspy commands are tagged with < >
    buf = '<%s>' % c
    logging.debug("SEND: %s" % buf)
    self.sock.sendall(buf.encode())

  def recvCmd(self, size = 1):
    assert(self.sock != None)
    # All powerspy commands are tagged with < >
    buf = ""
    while True:
      try:
        r = self.sock.recv(size)
        # then read one by one
        size = 1
      # TODO: fix in case of multiple ctrl+c
      except socket.timeout as err:
        logging.warning("Socket timeout while receiving command: %s" % err)
        break
      except OSError as err:
        if err.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
          logging.debug("EAGAIN or EWOULDBLOCK due to signal interrupt. Try to quit.")
          self.running = False
        else:
          logging.error("Socket error while receiving command: %s" % err)
        break
      if not r:
        logging.error("PowerSpy closed the Bluetooth connection")
        self.running = False
        break
      # FIXME what to do for multiple message? keep it in buffer...
      buf = "%s%s" % (buf,r.decode())
      mat = re.search('<(.*)>', buf, re.MULTILINE)
      if mat:
        buf = mat.group(1)
        logging.debug("RECV: <%s>" % buf)
        break
    return buf

  # Check identity
  def checkID(self):
    self.sendCmd(CMD_ID)
    s = self.recvCmd(23)
    mat = re.match('POWERSPY(.)(.{12})', s)
    if not mat:
      # Unable to process response for ID
      return False
    self.status = mat.group(1)
    # 'R': Ready
    # 'W': Waiting trigger
    # 'A': Acquisition in progress
    # 'C': Acquisition complete
    # 'T': FIXME Undocumented status, triggered? Realtime activated?
    extra = mat.group(2)
    self.pll_locked     = extra[0:2]  # PLL Locked (0x01 if locked, 0x00 if not)
    self.trigger_status = extra[2:4]  # Trigger status (2 characters)
    self.sw_version     = extra[4:6]  # SW version (1 byte / 2 characters)
    self.hw_version     = extra[6:8]  # HW version (1 byte / 2 characters)
    self.hw_serial      = extra[8:12] # HW serial number (2 bytes / 4 characters)
    logging.debug('status: %s pll_locked: %s trigger_status: %s sw_version: %s hw_version: %s hw_serial: %s' % (self.status, self.pll_locked, self.trigger_status, self.sw_version, self.hw_version, self.hw_serial))
    return True

  # Read EEPROM float (values: must be an array of 4 elements)
  def get_eeprom_float(self, values):
    val = ""
    for i in values:
      self.sendCmd(CMD_EEPROM_READ + i)
      val += self.recvCmd(4)
    # Format 32 bits, REAL4
    # < indicates little-endian encoding
    f = struct.unpack('<f', decode_hex(val)[0])
    return f[0]

  # Factory correction voltage coefficient
  def get_uscale_factory(self):
    self.uscale_factory = self.get_eeprom_float(["02", "03", "04", "05"])
    return self.uscale_factory

  # Factory correction current coefficient
  def get_iscale_factory(self):
    self.iscale_factory = self.get_eeprom_float(["06", "07", "08", "09"])
    return self.iscale_factory

  # Actual correction voltage coefficient
  def get_uscale_current(self):
    self.uscale_current = self.get_eeprom_float(["0E", "0F", "10", "11"])
    return self.uscale_current

  # Actual correction current coefficient
  def get_iscale_current(self):
    self.iscale_current = self.get_eeprom_float(["12", "13", "14", "15"])
    return self.iscale_current

  def calc_pscale(self):
    if self.pscale_factory == None:
      if self.uscale_factory != None and self.iscale_factory != None:
        self.pscale_factory = self.uscale_factory * self.iscale_factory
    if self.pscale_current == None:
      if self.uscale_current != None and self.iscale_current != None:
        self.pscale_current = self.uscale_current * self.iscale_current

  def get_frequency(self):
    self.sendCmd(CMD_FREQUENCY)
    f = self.recvCmd(7)
    f = struct.unpack('>H', decode_hex(f[1:])[0])
    if self.hw_version == "02":
      self.frequency = 1000000.0 / f[0]
    else:
      self.frequency = 1382400.0 / f[0]
    return self.frequency

  # check and get PowerSpy parameters
  def init(self):
    if not self.checkID():
      self.last_error = "Cannot identify the PowerSpy device"
      logging.error(self.last_error)
      self.close()
      return False
    if self.status != 'R' and self.status != 'C':
      # Device is busy let's force it to abort
      logging.warning("Device is in status %s, try to stop running action.", self.status)
      self.rt_stop()
      self.acquisition_stop()
    self.get_frequency()
    logging.debug("frequency:%.8f" % (self.frequency))
    # Retrieve device parameters
    self.get_uscale_factory()
    self.get_iscale_factory()
    self.get_uscale_current()
    self.get_iscale_current()
    self.calc_pscale()
    logging.debug("uscale_factory:%.8f iscale_factory:%.8f pscale_factory:%.8f" % (self.uscale_factory, self.iscale_factory, self.pscale_factory))
    logging.debug("uscale_current:%.8f iscale_current:%.8f pscale_current:%.8f" % (self.uscale_current, self.iscale_current, self.pscale_current))
    if self.hw_version == "02":
      # PowerSpy v1 supports 100 averaging periods.
      self.max_avg_period = 100
    else:
      # PowerSpy v2 (assuming all other versions) supports 65535 averaging periods.
      self.max_avg_period = 65535
    return True

  # deinitialize the PowerSpy device and close the serial port
  def close(self):
    if self.sock != None:
      # TODO deinit, if status ACQUIRING, ...
      sock = self.sock
      self.sock = None
      try:
        sock.close()
      except Exception as error:
        logging.warning("Cannot close Bluetooth transport cleanly: %s", error)

  def acquisition_start(self):
    self.sendCmd(CMD_START)
    a = self.recvCmd(3)
    if a != CMD_OK:
      logging.error('CMD_START FAILED')
      return False
    # Switching to acquisition mode requires some time
    # FIXME Ask alciom how long should we wait
    time.sleep(0.1)
    return True

  def acquisition_stop(self):
    self.sendCmd(CMD_CANCEL)
    a = self.recvCmd(3)
    # FIXME Documentation says it returns CMD_OK but always get CMD_FAILED
    #if a != CMD_OK:
    #  logging.error('CMD_CANCEL FAILED')
    #  return False
    return True

  # Start real time monitoring with the specified averaging periods
  # rt_stop() must be called if the function succeed
  def rt_start(self, avg_period):
    # Change the socket timeout to wait at least the interval plus the default timeout
    interval = avg_period / self.frequency
    self.sock.settimeout(interval + DEFAULT_TIMEOUT)
    # Check if exceeded number of avg_period
    if avg_period > self.max_avg_period:
      logging.error('Your PowerSpy does not support %d averaging periods (only %d).' % (avg_period, self.max_avg_period))
      return False
    # Encoding of the J command is different between v1 and v2
    if self.hw_version == "02":
      # PowerSpy v1 (hw_version == "02") format for CMD_RT <JXX>
      self.sendCmd("%s%02X" % (CMD_RT, avg_period))
    else:
      self.sendCmd("%s%04X" % (CMD_RT, avg_period))
    a = self.recvCmd(3)
    if a != CMD_OK:
      logging.error('CMD_RT FAILED')
      return False
    return True

  # Read monitored values and display them
  def rt_read(self):
    # Periodically read the input
    res = self.recvCmd(40) # 38 without the end of line or 40 with
    # RMS (Root Mean Square)
    # square of the RMS voltage (8 hex digits)
    # square of the RMS current (8 hex digits)
    # square of the RMS power (8 hex digits)
    # peak voltage (4 hex digits)
    # peak current (4 hex digits)
    values = res.split()
    # Should be an array of 5 elements
    if len(values) != 5:
      logging.warning("Invalid response")
      return [0,0,0,0,0]
    # convert string to values
    conv = []
    for i in range(5):
      hexa = decode_hex(values[i])[0]
      if len(hexa) == 2:
        fmt = '>H'
      elif len(hexa) == 4:
        fmt = '>I'
      else:
        logging.error("Invalid data %d %s" % (len(hexa), hexa))
      conv.insert(i, struct.unpack(fmt, hexa)[0])

    # Note: Initially scale_factory and scale_current are the same but in case of user calibration, scale_current must be used
    # Corrected RMS voltage = squareroot [ (square of the RMS voltage returned by fonction) x (Uscale_current)2 ]
    # Corrected RMS current = squareroot [ (square of the RMS current returned by fonction) x (Iscale_current)2 ]
    # Corrected RMS power = (square of the RMS current returned by fonction) x (Uscale_factory) x (Iscale_current)
    # Corrected peak voltage = peak voltage returned by fonction x Uscale_current
    # Corrected peak current = peak current returned by fonction x Iscale_current
    voltage = math.sqrt(self.uscale_current * self.uscale_current * conv[0])
    current = math.sqrt(self.iscale_current * self.iscale_current * conv[1])
    power = self.pscale_current * conv[2]
    pvoltage = self.uscale_current * conv[3]
    pcurrent = self.iscale_current * conv[4]

    return voltage, current, power, pvoltage, pcurrent

  # Stop the real time monitoring
  def rt_stop(self):
    # Reset the timeout to default
    self.sock.settimeout(DEFAULT_TIMEOUT)
    # TODO can check status before to stop
    self.sendCmd(CMD_RT_STOP)
    # flush input because it can have still data to read
    while True:
      a = self.recvCmd(3)
      if a == CMD_FAILED:
        logging.error('CMD_RT_STOP FAILED')
        return False
      if a == CMD_OK:
        return True
    return True

  # Display measurements every 1 second
  # If interval is higher than the PowerSpy device capacity, it will be an average of the averaged PowerSpy measurements
  def rt_capture(self, filename="", measurement_callback=None):
    self.last_error = None
    csv_file = None
    writer = None
    acquisition_started = False
    realtime_started = False
    try:
      if not self.acquisition_start():
        self.last_error = 'Acquisition failed'
        logging.error(self.last_error)
        return False
      acquisition_started = True

      # Convert the interval using frequency to find the averaging periods
      avg_period = int(round(self.frequency))
      if avg_period > self.max_avg_period:
        logging.warning('PowerSpy capacity exceeded: it will be average of averaged values for one second.')
        avg_period = int(round(self.frequency))
        every = 1
      else:
        every = 0

      if not self.rt_start(avg_period):
        self.last_error = 'Realtime acquisition failed'
        logging.error(self.last_error)
        return False
      realtime_started = True

      if not is_gui and measurement_callback is None:
        if allmetrics:
          print("# Timestamp\tV\tA\tW\tV\tA")
        else:
          print("# Timestamp\tW")

      if filename != "":
        csv_file = open(filename, "a+", newline='')
        csv_file.seek(0)
        writer = csv.writer(csv_file, delimiter=';', quoting=csv.QUOTE_NONE)
        if csv_file.read(1) == "":
          writer.writerow(["Timestamp", "Power"])

      # Accumulate samples only when the requested one-second interval exceeds
      # the device's native averaging capacity.
      voltages = []
      currents = []
      powers = []
      pvoltages = []
      pcurrents = []
      while self.running:
        voltage, current, power, pvoltage, pcurrent = self.rt_read()
        if not self.running:
          break
        # TODO should we check if rt_read returns [0,0,0,0,0] or None?
        if every != 0:
          voltages.append(voltage)
          currents.append(current)
          powers.append(power)
          pvoltages.append(pvoltage)
          pcurrents.append(pcurrent)
          if len(voltages) != every:
            continue
          voltage = sum(voltages) / float(len(voltages))
          current = sum(currents) / float(len(currents))
          power = sum(powers) / float(len(powers))
          pvoltage = max(pvoltages)
          pcurrent = max(pcurrents)
          voltages = []
          currents = []
          powers = []
          pvoltages = []
          pcurrents = []

        timestamp = time.time()
        if measurement_callback is not None:
          measurement_callback(timestamp, power)
        elif is_gui:
          # Compatibility for callers that still attach a GUI directly.
          self.gui.update_data_fields(f"{timestamp:0.0f}", f"{power:.3f}")
        else:
          # Write to terminal
          if allmetrics:
            output = "%0.0f\t%0.3f\t%0.3f\t%0.3f\t%0.3f\t%0.3f" % (
              timestamp, voltage, current, power, pvoltage, pcurrent
            )
          else:
            output = "%0.0f\t%0.3f" % (timestamp, power)
          if sys.stdout.isatty():
            sys.stdout.write("\r%s          " % output)
          else:
            sys.stdout.write("%s\n" % output)
          sys.stdout.flush()

        # Save to CSV file
        if csv_file is not None:
          writer.writerow(['{:.0f}'.format(timestamp), '{:.3f}'.format(power)])
          csv_file.flush()
          
    except Exception as e:
      self.last_error = "Realtime capture failed (%s)" % e
      logging.error(self.last_error)
      self.running = False
    finally:
      if csv_file is not None:
        csv_file.close()
      if realtime_started:
        try:
          self.rt_stop()
        except Exception as error:
          logging.warning("Cannot stop realtime mode cleanly: %s", error)
      if acquisition_started:
        try:
          self.acquisition_stop()
        except Exception as error:
          logging.warning("Cannot stop acquisition cleanly: %s", error)
    return self.last_error is None

  # Signal handler to exit properly on SIGINT
  def exit_gracefully(self, signal, frame):
    self.running = False

def is_valid_mac(address):
  address_regex = re.compile(r"""
      (^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$) |  # 00:1A:2B:3C:4D:5E or 00-1A-2B-3C-4D-5E
      (^([0-9A-Fa-f]{4}\.){2}([0-9A-Fa-f]{4})$)      # 001A.2B3C.4D5E (Cisco format)
  """, re.VERBOSE)
  return bool(address_regex.match(address))

#-------------------------------------------------------------------------------------------
# Program main
#-------------------------------------------------------------------------------------------

if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser(description='Alciom PowerSpy reader.')
  parser.add_argument('-m', '--devicemac', metavar='MAC', help='MAC address of the PowerSpy device.')
  parser.add_argument('-g', '--gui', action='store_true', help='GUI interface.')
  parser.add_argument('-v', '--verbose', action='store_true', help='Verbose mode.')
  parser.add_argument('-a', '--allmetrics', action='store_true', help='Show all metrics.')
  parser.add_argument('-f', '--file', type=str, nargs='?', const="powerspy_"+str(int(time.time()))+".csv", default=None,
  help='Name of csv file to store power data. If used without argument, a default name is assigned.')

  args = parser.parse_args()

  if args.gui:
    # Import GUI modules
    # For the GUI interface
    import ttkbootstrap as ttk
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from ttkbootstrap.constants import *
    # Start GUI
    is_gui = True
    dev = PowerSpy()
    app = PSCGUI(dev)
    dev.gui = app
    app.run()
  else:
    # Start CLI
    if (args.devicemac is None) or (not is_valid_mac(args.devicemac)):
      print("MAC address is not valid: %s" % args.devicemac)
      sys.exit(1)

    print("Please wait while connecting and getting data from PowerSpy")

    if args.verbose:
      if args.verbose > 0:
        logging.basicConfig(level=logging.DEBUG)

    if args.allmetrics:
        allmetrics = True

    dev = PowerSpy()

    # Setup signal handler for CTRL-C
    signal.signal(signal.SIGINT, lambda s, f: dev.exit_gracefully(s, f))

    # TODO set port to 1 but can be different?
    port = 1
    err = dev.connect((args.devicemac, port))
    if err:
      detail = ": %s" % dev.last_error if dev.last_error else ""
      print("Cannot connect to the device %s%s" % (args.devicemac, detail))
      sys.exit(1)

    try:
      initialized = dev.init()
    except Exception as error:
      dev.last_error = "Device initialization failed: %s" % error
      logging.error(dev.last_error)
      initialized = False
    if not initialized:
      detail = ": %s" % dev.last_error if dev.last_error else ""
      print("Device cannot be initialized%s" % detail)
      dev.close()
      sys.exit(1)

    if args.file is None:
      args.file = ""

    try:
      captured = dev.rt_capture(args.file)
    except Exception as error:
      dev.last_error = "PowerSpy capture failed: %s" % error
      logging.error(dev.last_error)
      print(dev.last_error)
      sys.exit(1)
    finally:
      dev.close()
    if not captured:
      print(dev.last_error or "PowerSpy capture failed")
      sys.exit(1)
