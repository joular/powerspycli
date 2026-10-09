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
import time    # sleep/time
import errno   # IOError numbers
import csv     # for csv handling
import threading

# PowerSpy commands used here (see specifications/)
CMD_ID = '?'
CMD_EEPROM_READ = 'V'
CMD_START = 'S'
CMD_CANCEL = 'C'
CMD_FREQUENCY = 'F'
CMD_RT = 'J'
CMD_RT_STOP = 'Q'

# Generic responses
CMD_OK = 'K'
CMD_FAILED = 'Z'

# Constants
DEFAULT_TIMEOUT = 3.0 # secs (float allowed, timeout to receive response from PowerSpy, except in realtime mode)
MAX_MISSED_READS = 3  # stop realtime capture after this many reads in a row without data

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

# GUI colors, close to Joular Energy Meter
BG = "#DCE5E7"
CARD = "#F8FAFA"
BORDER = "#C3CED1"
INK = "#17222A"
MUTED = "#5E6E76"
ACCENT = "#1E7471"
ACCENT_DARK = "#165956"
DANGER = "#B03A22"
DANGER_DARK = "#8C2E1B"
LIVE = "#2E8B57"
WAIT = "#C98A1B"

PROJECT_URL = "https://github.com/joular/powerspycli"

# Smallest 1, 2 or 5 times a power of ten that is >= value (trend scale)
def nice_ceiling(value):
  if value <= 0:
    return 1
  magnitude = 10 ** math.floor(math.log10(value))
  for k in (1, 2, 5, 10):
    if value <= k * magnitude:
      return k * magnitude

class PSCGUI:
  def __init__(self, powerspy_):
    self.powerspy = powerspy_
    self.thread = None
    self.history = deque(maxlen=120) # last 2 minutes at one value per second
    self._reset_stats()

    self.root = tk.Tk()
    self.root.title("PowerSpyCli")
    self.root.configure(bg=BG)
    self.root.minsize(460, 0)

    family = font.nametofont("TkDefaultFont").actual("family")
    self.font_small = (family, 10)
    normal, bold = (family, 12), (family, 12, "bold")

    style = ttk.Style(self.root)
    style.theme_use("clam") # the built-in theme that honors custom colors on every OS
    style.configure("TEntry", fieldbackground="white", foreground=INK, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=5)
    style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              fieldbackground=[("disabled", CARD)], foreground=[("disabled", MUTED)])
    style.configure("TCheckbutton", background=CARD, foreground=INK, font=normal,
                    indicatorbackground="white", indicatorforeground="white")
    style.map("TCheckbutton", background=[("active", CARD)], foreground=[("disabled", MUTED)],
              indicatorbackground=[("selected", ACCENT), ("disabled", CARD)])
    style.configure("TButton", background=CARD, foreground=INK, bordercolor=BORDER, lightcolor=CARD,
                    darkcolor=CARD, focuscolor=CARD, font=normal, padding=(10, 4))
    style.map("TButton", background=[("disabled", CARD), ("active", BG)], foreground=[("disabled", MUTED)])
    for name, color, dark in (("Accent.TButton", ACCENT, ACCENT_DARK), ("Danger.TButton", DANGER, DANGER_DARK)):
      style.configure(name, background=color, foreground="white", bordercolor=color, lightcolor=color,
                      darkcolor=color, focuscolor=color, font=(family, 13, "bold"), padding=(10, 9))
      style.map(name, background=[("disabled", BORDER), ("active", dark)], foreground=[("disabled", MUTED)],
                bordercolor=[("disabled", BORDER), ("active", dark)], lightcolor=[("disabled", BORDER), ("active", dark)],
                darkcolor=[("disabled", BORDER), ("active", dark)])

    main = tk.Frame(self.root, bg=BG, padx=16, pady=14)
    main.pack(fill=tk.BOTH, expand=True)

    # Header: title and connection status
    header = tk.Frame(main, bg=BG)
    header.pack(fill=tk.X)
    tk.Label(header, text="PowerSpyCli", bg=BG, fg=INK, font=(family, 20, "bold")).pack(side=tk.LEFT)
    self.status_label = tk.Label(header, text="Idle", bg=BG, fg=INK, font=normal)
    self.status_label.pack(side=tk.RIGHT)
    self.status_dot = tk.Canvas(header, width=12, height=12, bg=BG, highlightthickness=0)
    self.status_dot.pack(side=tk.RIGHT, padx=(0, 6))
    self.dot = self.status_dot.create_oval(1, 1, 11, 11, fill=MUTED, outline="")

    # Device and recording
    device = self._card(main)
    device.columnconfigure(1, weight=1)
    tk.Label(device, text="MAC address", bg=CARD, fg=INK, font=normal).grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
    self.mac_entry = ttk.Entry(device, font=normal)
    self.mac_entry.grid(row=0, column=1, columnspan=2, sticky=tk.EW)
    self.mac_entry.bind("<Key>", lambda e: self._set_message())
    self.mac_entry.bind("<Return>", lambda e: self.toggle_capture())

    self.write_to_file = tk.BooleanVar(value=False)
    self.file_path = tk.StringVar()
    self.record_check = ttk.Checkbutton(device, text="Record to", variable=self.write_to_file,
                                        command=self.toggle_file_selector)
    self.record_check.grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
    self.file_entry = ttk.Entry(device, textvariable=self.file_path, font=normal, state=tk.DISABLED)
    self.file_entry.grid(row=1, column=1, sticky=tk.EW, pady=(10, 0))
    self.file_button = ttk.Button(device, text="Choose…", command=self.select_file, state=tk.DISABLED)
    self.file_button.grid(row=1, column=2, sticky=tk.E, padx=(8, 0), pady=(10, 0))

    self.message = tk.Label(device, bg=CARD, fg=DANGER, font=normal, justify=tk.LEFT, anchor=tk.W, wraplength=400)
    self.message.grid(row=2, column=0, columnspan=3, sticky=tk.EW, pady=(10, 0))
    self.message.grid_remove()

    # Live reading and session statistics
    reading = self._card(main)
    top = tk.Frame(reading, bg=CARD)
    top.pack(fill=tk.X)
    tk.Label(top, text="Power", bg=CARD, fg=MUTED, font=normal).pack(side=tk.LEFT)
    self.time_value = tk.Label(top, text="", bg=CARD, fg=MUTED, font=normal)
    self.time_value.pack(side=tk.RIGHT)
    value = tk.Frame(reading, bg=CARD)
    value.pack(fill=tk.X)
    self.power_value = tk.Label(value, text="—", bg=CARD, fg=INK, font=(family, 44, "bold"))
    self.power_value.pack(side=tk.LEFT)
    tk.Label(value, text="W", bg=CARD, fg=MUTED, font=(family, 20)).pack(side=tk.LEFT, anchor=tk.S, padx=(6, 0), pady=(0, 10))
    tk.Frame(reading, bg=BORDER, height=1).pack(fill=tk.X, pady=(6, 10))
    stats = tk.Frame(reading, bg=CARD)
    stats.pack(fill=tk.X)
    self.stat_values = {}
    for column, name in enumerate(("Average", "Peak", "Energy", "Time")):
      stats.columnconfigure(column, weight=1, uniform="stats")
      tk.Label(stats, text=name, bg=CARD, fg=MUTED, font=self.font_small).grid(row=0, column=column, sticky=tk.W)
      self.stat_values[name] = tk.Label(stats, text="—", bg=CARD, fg=INK, font=(family, 14, "bold"))
      self.stat_values[name].grid(row=1, column=column, sticky=tk.W)

    # Trend
    trend = self._card(main)
    tk.Label(trend, text="Last 2 minutes", bg=CARD, fg=MUTED, font=normal).pack(anchor=tk.W)
    self.trend = tk.Canvas(trend, height=110, bg=CARD, highlightthickness=0)
    self.trend.pack(fill=tk.X, pady=(6, 0))
    self.trend.bind("<Configure>", lambda e: self._draw_trend())

    # Start/Stop
    self.action = ttk.Button(main, text="Start", style="Accent.TButton", command=self.toggle_capture, cursor="hand2")
    self.action.pack(fill=tk.X, pady=(14, 0))

    # Footer
    footer = tk.Frame(main, bg=BG)
    footer.pack(fill=tk.X, pady=(12, 0))
    tk.Label(footer, text="© 2021-2026 Adel Noureddine", bg=BG, fg=MUTED, font=self.font_small).pack(side=tk.LEFT)
    link = tk.Label(footer, text="github.com/joular/powerspycli", bg=BG, fg=ACCENT, font=self.font_small, cursor="hand2")
    link.pack(side=tk.RIGHT)
    link.bind("<Button-1>", lambda e: webbrowser.open(PROJECT_URL))

  def _card(self, parent):
    card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                    highlightcolor=BORDER, padx=16, pady=14)
    card.pack(fill=tk.X, pady=(12, 0))
    return card

  def _reset_stats(self):
    self.count = 0
    self.total = self.peak = self.energy = 0.0
    self.first_time = self.last_time = None
    self.history.clear()

  def _set_status(self, text, color):
    self.status_label.config(text=text)
    self.status_dot.itemconfig(self.dot, fill=color)

  def _set_message(self, text=""):
    self.message.config(text=text)
    if text:
      self.message.grid()
    else:
      self.message.grid_remove()

  def _set_inputs(self, state):
    self.mac_entry.config(state=state)
    self.record_check.config(state=state)
    file_state = state if self.write_to_file.get() else tk.DISABLED
    self.file_entry.config(state=file_state)
    self.file_button.config(state=file_state)

  def _draw_trend(self):
    c = self.trend
    c.delete("all")
    width, height = c.winfo_width(), c.winfo_height()
    left, top, bottom = 44, 8, height - 8
    scale = nice_ceiling(max(self.history, default=0))
    c.create_line(left, top, width, top, fill=BORDER, dash=(2, 4))
    c.create_line(left, bottom, width, bottom, fill=BORDER)
    c.create_text(left - 8, top, text="%g W" % scale, anchor=tk.E, fill=MUTED, font=self.font_small)
    c.create_text(left - 8, bottom, text="0", anchor=tk.E, fill=MUTED, font=self.font_small)
    if len(self.history) > 1:
      # Newest value on the right edge
      step = (width - left) / (self.history.maxlen - 1)
      start = width - (len(self.history) - 1) * step
      points = []
      for i, power in enumerate(self.history):
        points += [start + i * step, bottom - power / scale * (bottom - top)]
      c.create_line(points, fill=ACCENT, width=2)

  def update_data_fields(self, timestamp, power):
    """Show a new measurement and update the session statistics."""
    if self.first_time is None:
      self.first_time = timestamp
    # Energy over the time since the previous value (one second for the first one)
    self.energy += power * (timestamp - self.last_time if self.last_time else 1.0)
    self.last_time = timestamp
    self.count += 1
    self.total += power
    self.peak = max(self.peak, power)
    self.history.append(power)

    elapsed = int(timestamp - self.first_time)
    self.power_value.config(text="%.3f" % power)
    self.time_value.config(text=time.strftime("%H:%M:%S", time.localtime(timestamp)))
    self.stat_values["Average"].config(text="%.3f W" % (self.total / self.count))
    self.stat_values["Peak"].config(text="%.3f W" % self.peak)
    self.stat_values["Energy"].config(text="%.1f J" % self.energy)
    self.stat_values["Time"].config(text="%d:%02d:%02d" % (elapsed // 3600, elapsed // 60 % 60, elapsed % 60))
    self._draw_trend()

  def _schedule(self, callback, *args):
    """Marshal a worker-thread callback onto Tk's main thread."""
    self.root.after(0, callback, *args)

  def toggle_capture(self):
    if self.thread is not None and self.thread.is_alive():
      self.stop_process()
    else:
      self.start_process()

  def start_process(self):
    mac_address = normalize_mac(self.mac_entry.get())
    if mac_address is None:
      self._set_message("Enter a valid MAC address, for example 00:11:22:33:44:55.")
      self.mac_entry.focus_set()
      return
    self._set_message()

    file_path = ""
    if self.write_to_file.get():
      if not self.file_path.get().strip():
        self.file_path.set("powerspy-%d.csv" % time.time())
      file_path = self.file_path.get().strip()

    self._reset_stats()
    self.power_value.config(text="—")
    self.time_value.config(text="")
    for label in self.stat_values.values():
      label.config(text="—")
    self._draw_trend()
    self._set_inputs(tk.DISABLED)
    self.action.config(text="Stop", style="Danger.TButton")
    self._set_status("Connecting…", WAIT)

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
          self._schedule(self.update_data_fields, float(match.group(1)), float(match.group(2)))
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
    """Connect and capture on a worker thread (Linux and Windows)."""
    try:
      if self.powerspy.connect((mac_address, 1)) != 0:
        message = self.powerspy.last_error or "Connection error. Check the device and retry."
        self._schedule(self._connection_failed, message)
        return
      if not self.powerspy.running:
        return
      if not self.powerspy.init():
        message = self.powerspy.last_error or "Device cannot be initialized. Check the connection and retry."
        self._schedule(self._connection_failed, message)
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
    self._set_status("Live", LIVE)

  def _connection_failed(self, message):
    self.powerspy.last_error = message
    self._set_message(message)
    self._set_status("Error", DANGER)

  def _measurement_received(self, timestamp, power):
    self._schedule(self.update_data_fields, timestamp, power)

  def _capture_finished(self):
    self.powerspy.running = False
    self._set_inputs(tk.NORMAL)
    self.action.config(text="Start", style="Accent.TButton", state=tk.NORMAL)
    if self.powerspy.last_error:
      self._set_message(self.powerspy.last_error)
      self._set_status("Error", DANGER)
    else:
      self._set_status("Stopped", MUTED)

  def stop_process(self):
    self._set_status("Stopping…", WAIT)
    self.action.config(text="Stopping…", state=tk.DISABLED)
    self.powerspy.running = False
    self._interrupt_cli_process()

  def toggle_file_selector(self):
    if self.write_to_file.get() and not self.file_path.get().strip():
      self.file_path.set("powerspy-%d.csv" % time.time())
    self._set_inputs(tk.NORMAL)

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
    self.rx = bytearray() # received bytes not yet returned as a frame
    self.last_error = None
    self.status = None
    self.hw_version = None
    self.uscale = self.iscale = self.pscale = None
    self.frequency = None
    self.max_avg_period = None
    self.running = True

  def connect(self, address):
    if self.sock is not None:
      logging.warning("Already connected")
      return 1

    # Try to connect 3 times before raising error
    for attempt in range(3):
      self.last_error = None
      self.rx.clear()
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
        if isinstance(error, (ImportError, PermissionError)) or not self.running:
          break
        if attempt < 2:
          time.sleep(3) # Sleep 3 seconds before retrying
    return 1

  def sendCmd(self, c):
    assert(self.sock is not None)
    # All powerspy commands are tagged with < >
    buf = '<%s>' % c
    logging.debug("SEND: %s" % buf)
    self.sock.sendall(buf.encode())

  # Return the body of the next <...> frame, or "" on timeout or error.
  # Bytes after the frame are kept for the next call.
  def recvCmd(self):
    assert(self.sock is not None)
    while True:
      end = self.rx.find(b'>')
      if end >= 0:
        frame = self.rx[:end]
        del self.rx[:end + 1]
        start = frame.rfind(b'<')
        if start < 0:
          continue # tail of a frame we only got partially, drop it
        buf = frame[start + 1:].decode('ascii', 'replace')
        logging.debug("RECV: <%s>" % buf)
        return buf
      try:
        r = self.sock.recv(256)
      except socket.timeout as err:
        logging.warning("Socket timeout while receiving command: %s" % err)
        return ""
      except OSError as err:
        if err.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
          logging.debug("EAGAIN or EWOULDBLOCK due to signal interrupt. Try to quit.")
          self.running = False
        else:
          logging.error("Socket error while receiving command: %s" % err)
        return ""
      if not r:
        logging.error("PowerSpy closed the Bluetooth connection")
        self.running = False
        return ""
      self.rx.extend(r)

  # Check identity
  def checkID(self):
    self.sendCmd(CMD_ID)
    # Skip realtime frames left over if a previous run did not stop them
    for _ in range(5):
      s = self.recvCmd()
      if not s or s.startswith('POWERSPY'):
        break
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
    self.hw_version = extra[6:8]
    logging.debug('status: %s pll_locked: %s trigger_status: %s sw_version: %s hw_version: %s hw_serial: %s' % (self.status, extra[0:2], extra[2:4], extra[4:6], extra[6:8], extra[8:12]))
    return True

  # Read a 32 bits float (REAL4, little-endian) stored in 4 EEPROM bytes from address
  def get_eeprom_float(self, address):
    val = ""
    for i in range(address, address + 4):
      self.sendCmd("%s%02X" % (CMD_EEPROM_READ, i))
      val += self.recvCmd()
    return struct.unpack('<f', bytes.fromhex(val))[0]

  def get_frequency(self):
    self.sendCmd(CMD_FREQUENCY)
    f = int(self.recvCmd()[1:], 16)
    if self.hw_version == "02":
      self.frequency = 1000000.0 / f
    else:
      self.frequency = 1382400.0 / f
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
    # Actual correction coefficients (same as factory ones unless the user recalibrated)
    self.uscale = self.get_eeprom_float(0x0E)
    self.iscale = self.get_eeprom_float(0x12)
    self.pscale = self.uscale * self.iscale
    logging.debug("uscale:%.8f iscale:%.8f pscale:%.8f" % (self.uscale, self.iscale, self.pscale))
    # PowerSpy v1 (hw_version "02") supports 100 averaging periods, v2 65535
    self.max_avg_period = 100 if self.hw_version == "02" else 65535
    return True

  # deinitialize the PowerSpy device and close the serial port
  def close(self):
    if self.sock is not None:
      sock = self.sock
      self.sock = None
      self.rx.clear()
      try:
        sock.close()
      except Exception as error:
        logging.warning("Cannot close Bluetooth transport cleanly: %s", error)

  def acquisition_start(self):
    self.sendCmd(CMD_START)
    if self.recvCmd() != CMD_OK:
      logging.error('CMD_START FAILED')
      return False
    # Switching to acquisition mode requires some time
    # FIXME Ask alciom how long should we wait
    time.sleep(0.1)
    return True

  def acquisition_stop(self):
    self.sendCmd(CMD_CANCEL)
    # FIXME Documentation says it returns CMD_OK but always get CMD_FAILED
    self.recvCmd()
    return True

  # Start real time monitoring with the specified averaging periods
  # rt_stop() must be called if the function succeed
  def rt_start(self, avg_period):
    # Check if exceeded number of avg_period
    if avg_period > self.max_avg_period:
      logging.error('Your PowerSpy does not support %d averaging periods (only %d).' % (avg_period, self.max_avg_period))
      return False
    # Change the socket timeout to wait at least the interval plus the default timeout
    self.sock.settimeout(avg_period / self.frequency + DEFAULT_TIMEOUT)
    # Encoding of the J command is different between v1 and v2
    if self.hw_version == "02":
      # PowerSpy v1 (hw_version == "02") format for CMD_RT <JXX>
      self.sendCmd("%s%02X" % (CMD_RT, avg_period))
    else:
      self.sendCmd("%s%04X" % (CMD_RT, avg_period))
    if self.recvCmd() != CMD_OK:
      logging.error('CMD_RT FAILED')
      return False
    return True

  # Read one realtime measurement: (voltage, current, power, peak voltage, peak current), or None
  def rt_read(self):
    # Square of the RMS voltage, square of the RMS current, RMS power (8 hex digits each),
    # peak voltage and peak current (4 hex digits each)
    values = self.recvCmd().split()
    if len(values) != 5:
      if values:
        logging.warning("Invalid response: %s", values)
      return None
    try:
      u2, i2, p, pu, pi = [int(v, 16) for v in values]
    except ValueError:
      logging.warning("Invalid response: %s", values)
      return None
    return (math.sqrt(self.uscale * self.uscale * u2),
            math.sqrt(self.iscale * self.iscale * i2),
            self.pscale * p,
            self.uscale * pu,
            self.iscale * pi)

  # Stop the real time monitoring
  def rt_stop(self):
    # Reset the timeout to default
    self.sock.settimeout(DEFAULT_TIMEOUT)
    self.sendCmd(CMD_RT_STOP)
    # Skip realtime frames still in flight until the answer
    for _ in range(10):
      a = self.recvCmd()
      if a == CMD_OK:
        return True
      if a == CMD_FAILED or a == "":
        break
    logging.error('CMD_RT_STOP FAILED')
    return False

  # Display measurements every 1 second
  # If one second is more than the PowerSpy averaging capacity, average several PowerSpy measurements
  def rt_capture(self, filename="", measurement_callback=None, allmetrics=False):
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

      # Averaging periods for one second, split over several reads if above the device capacity
      every = math.ceil(self.frequency / self.max_avg_period)
      avg_period = int(round(self.frequency / every))
      if every > 1:
        logging.warning('PowerSpy capacity exceeded: each value is the average of %d measurements.', every)

      if not self.rt_start(avg_period):
        self.last_error = 'Realtime acquisition failed'
        logging.error(self.last_error)
        return False
      realtime_started = True

      if measurement_callback is None:
        if allmetrics:
          print("# Timestamp\tV\tA\tW\tV\tA")
        else:
          print("# Timestamp\tW")

      if filename:
        new_file = not os.path.exists(filename) or os.path.getsize(filename) == 0
        csv_file = open(filename, "a", newline='')
        writer = csv.writer(csv_file, delimiter=';', quoting=csv.QUOTE_NONE)
        if new_file:
          writer.writerow(["Timestamp", "Power"])

      samples = []
      missed = 0
      while self.running:
        values = self.rt_read()
        if not self.running:
          break
        if values is None:
          missed += 1
          if missed >= MAX_MISSED_READS:
            self.last_error = "No data from PowerSpy. Check that it is powered on and in range."
            logging.error(self.last_error)
            break
          continue
        missed = 0

        samples.append(values)
        if len(samples) < every:
          continue
        cols = list(zip(*samples))
        samples = []
        voltage, current, power = (sum(c) / len(c) for c in cols[:3])
        pvoltage, pcurrent = max(cols[3]), max(cols[4])

        timestamp = time.time()
        if measurement_callback is not None:
          measurement_callback(timestamp, power)
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

# Return the MAC address as XX:XX:XX:XX:XX:XX (the form sockets expect), or None if invalid.
# Accepts 00:1A:2B:3C:4D:5E, 00-1A-2B-3C-4D-5E and 001A.2B3C.4D5E (Cisco format).
def normalize_mac(address):
  digits = re.sub(r'[\s:.-]', '', address or '')
  if not re.fullmatch(r'[0-9A-Fa-f]{12}', digits):
    return None
  return ':'.join(digits[i:i + 2] for i in range(0, 12, 2)).upper()

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
  parser.add_argument('-f', '--file', type=str, nargs='?', const="powerspy-"+str(int(time.time()))+".csv", default=None,
  help='Name of csv file to store power data. If used without argument, a default name is assigned.')

  args = parser.parse_args()

  if args.verbose:
    logging.basicConfig(level=logging.DEBUG)

  if args.gui:
    # GUI modules, all from the standard library
    import tkinter as tk
    import webbrowser
    from collections import deque
    from tkinter import ttk, filedialog, font
    # Start GUI
    app = PSCGUI(PowerSpy())
    app.run()
  else:
    # Start CLI
    mac_address = normalize_mac(args.devicemac)
    if mac_address is None:
      print("MAC address is not valid: %s" % args.devicemac)
      sys.exit(1)

    print("Please wait while connecting and getting data from PowerSpy")

    dev = PowerSpy()

    # First CTRL-C stops the capture cleanly, a second one quits immediately
    def stop_capture(signum, frame):
      dev.running = False
      signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGINT, stop_capture)

    # RFCOMM channel 1 is the PowerSpy default (macOS discovers it through SDP)
    err = dev.connect((mac_address, 1))
    if err:
      detail = ": %s" % dev.last_error if dev.last_error else ""
      print("Cannot connect to the device %s%s" % (mac_address, detail))
      sys.exit(1)

    try:
      initialized = dev.init()
    except Exception as error:
      dev.last_error = "Device initialization failed: %s" % error
      logging.error(dev.last_error)
      initialized = False
    except KeyboardInterrupt:
      dev.close()
      sys.exit(130)
    if not initialized:
      detail = ": %s" % dev.last_error if dev.last_error else ""
      print("Device cannot be initialized%s" % detail)
      dev.close()
      sys.exit(1)

    try:
      captured = dev.rt_capture(args.file or "", allmetrics=args.allmetrics)
    except KeyboardInterrupt:
      sys.exit(130)
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
