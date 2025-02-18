import ttkbootstrap as ttk
import tkinter as tk
from tkinter import filedialog, messagebox
from ttkbootstrap.constants import *
import time
import re
import threading

class PSCGUI:
    def __init__(self, powerspy_):
        self.powerspy = powerspy_
        self.root = ttk.Window(themename="flatly")
        self.root.title("PowerSpyGUI")

        # Initialize theme (default is light)
        self.current_theme = "flatly"  # Light theme
        self.dark_theme = "darkly"    # Dark theme

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
        self.mac_frame = ttk.LabelFrame(self.main_frame, text="MAC Address", padding="10")
        self.mac_frame.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

        ttk.Label(self.mac_frame, text="Enter MAC Address:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.mac_entry = ttk.Entry(self.mac_frame, width=25)
        self.mac_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

        # File Options
        self.file_path = tk.StringVar()
        self.file_frame = ttk.LabelFrame(self.main_frame, text="File Options", padding="10")
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
            self.control_frame, text="Stop", command=self.stop_process, bootstyle=DANGER, width=10, state=DISABLED, cursor="hand2"
        )
        self.stop_button.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

        # Status
        self.status_frame = ttk.LabelFrame(self.main_frame, text="Status", padding="10")
        self.status_frame.grid(row=4, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

        self.status_label = ttk.Label(self.status_frame, text="Status: Idle", bootstyle=INFO)
        self.status_label.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        #  Data Fields
        self.data_frame = ttk.LabelFrame(self.main_frame, text="Power values", padding="10")
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
        self.info_frame = ttk.LabelFrame(self.main_frame, text="Info", padding="10")
        self.info_frame.grid(row=6, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)

        self.info_label = ttk.Label(self.info_frame, text="PowerSpyCli GUI - Copyright (c) 2021-2025 Adel Noureddine", bootstyle=SECONDARY)
        self.info_label.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        # Clickable link label
        self.link_label = ttk.Label(self.info_frame, text="https://github.com/joular/powerspycli", bootstyle=SECONDARY, cursor="hand2")
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

    def start_process(self):
        mac_address = self.mac_entry.get()
        if not self.is_valid_mac(mac_address):
            messagebox.showerror("Invalid MAC Address", "Please enter a valid MAC address.")
            return

        self.start_button.config(state=DISABLED)
        self.root.update_idletasks()

        if self.write_to_file.get():
            file_path = self.file_path.get()
            if not file_path:
                timestamp = int(time.time())
                file_path = f"powerspy-{timestamp}.csv"
                self.file_path.set(file_path)
                messagebox.showinfo("Default File Created", f"No file selected. Using default file: {file_path}")
            self.file_label.config(text=f"Saving to: {file_path}", bootstyle=SUCCESS)

        if self.powerspy.sock is None:
            self.status_label.config(text="Status: Connecting...", bootstyle=WARNING)
            self.root.update_idletasks()
            if self.powerspy.connect((mac_address, 1)) == 0:
                if not self.powerspy.init():
                    self.status_label.config(text="Status: Device cannot be initialized. Retry.", bootstyle=DANGER)
                    self.start_button.config(state=NORMAL)
                    return
            else:
                self.status_label.config(text="Connection error. Retry several times.", bootstyle=DANGER)
                self.start_button.config(state=NORMAL)
                return

        self.status_label.config(text=f"Status: Listening to PowerSpy [{mac_address}]", bootstyle=SUCCESS)
        self.stop_button.config(state=NORMAL)
        self.mac_entry.config(state=DISABLED)
        self.toggle_capture(self.file_path.get())

    def stop_process(self):
        self.status_label.config(text="Status: Stopped", bootstyle=DANGER)
        self.start_button.config(state=NORMAL)
        self.stop_button.config(state=DISABLED)
        self.mac_entry.config(state=NORMAL)
        self.powerspy.running = False

    def toggle_file_selector(self):
        if self.write_to_file.get():
            self.file_selector_button.config(state=NORMAL)
        else:
            self.file_selector_button.config(state=DISABLED)

    def select_file(self):
        file_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if file_path:
            self.file_path.set(file_path)

    def toggle_capture(self, file):
        self.powerspy.running = True
        self.thread = threading.Thread(target=self.powerspy.rt_capture, args=(file,), daemon=True)
        self.thread.start()

    def is_valid_mac(self, address):
        address_regex = re.compile(r"""
            (^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$) |  # 00:1A:2B:3C:4D:5E or 00-1A:2B-3C-4D-5E
            (^([0-9A-Fa-f]{4}\.){2}([0-9A-Fa-f]{4})$)      # 001A.2B3C.4D5E (Cisco format)
        """, re.VERBOSE)
        return bool(address_regex.match(address))

    def run(self):
        self.root.mainloop()