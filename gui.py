import ttkbootstrap as ttk
import tkinter as tk  
from tkinter import filedialog, messagebox
from ttkbootstrap.constants import *
import time
import re
import threading
import sys


class PowerSpyApp:
    def __init__(self, powerspy_):
        
        self.powerspy = powerspy_
        self.root = ttk.Window(themename="flatly")
        self.root.title("MAC Address Processor")

        # MAC Address Entry
        ttk.Label(self.root, text="Enter MAC Address:").grid(row=0, column=0, padx=10, pady=10)
        self.mac_entry = ttk.Entry(self.root, width=20)
        self.mac_entry.grid(row=0, column=1, padx=10, pady=10)

        # Start Button
        self.start_button = ttk.Button(self.root, text="Start", command=self.start_process, bootstyle=SUCCESS)
        self.start_button.grid(row=1, column=0, padx=10, pady=10)

        # Stop Button
        self.stop_button = ttk.Button(self.root, text="Stop", command=self.stop_process, bootstyle=DANGER, state=DISABLED)
        self.stop_button.grid(row=1, column=1, padx=10, pady=10)

        # Checkbox for writing to file
        self.write_to_file = tk.BooleanVar()
        self.write_to_file.set(False)
        self.file_checkbox = ttk.Checkbutton(self.root, text="Write to file", variable=self.write_to_file, command=self.toggle_file_selector)
        self.file_checkbox.grid(row=2, column=0, padx=10, pady=10)

        # File Selector
        self.file_path = tk.StringVar()
        self.file_selector_button = ttk.Button(self.root, text="Select File", command=self.select_file, bootstyle=INFO, state=DISABLED)
        self.file_selector_button.grid(row=2, column=1, padx=10, pady=10)

        # File Label, if any, show the name of the file where data is written to
        self.file_label = ttk.Label(self.root, text="File: none", bootstyle=INFO)
        self.file_label.grid(row=3, column=0, columnspan=2, padx=10, pady=10)

        # Status Label
        self.status_label = ttk.Label(self.root, text="Status: Idle", bootstyle=INFO)
        self.status_label.grid(row=4, column=0, columnspan=2, padx=10, pady=10)

        # Timestamp and power label (shown when capture starts)
        self.timestamp_label = ttk.Label(self.root, text="Timestamp")
        self.timestamp_value = ttk.Label(self.root, text="")
        self.power_label = ttk.Label(self.root, text="Power (Watts)")
        self.power_value = ttk.Label(self.root, text="")

    def show_data_fields(self):
        """ Affiche les champs timestamp et power """
        self.timestamp_label.grid(row=5, column=0, padx=10, pady=10)
        self.timestamp_value.grid(row=6, column=0, padx=10, pady=10)
        self.power_label.grid(row=5, column=1, padx=10, pady=10)
        self.power_value.grid(row=6, column=1, padx=10, pady=10)

    def hide_data_fields(self):
        """ Cache les champs timestamp et power """
        self.timestamp_label.grid_remove()
        self.timestamp_value.grid_remove()
        self.power_label.grid_remove()
        self.power_value.grid_remove()

    #called in powerspy class (rt_capture method) to update time and power on the gui
    def update_data_fields(self, timestamp, power):
        """ Met à jour les valeurs affichées pour timestamp et power """
        self.timestamp_value.config(text=str(timestamp))
        self.power_value.config(text=str(power))

    #check mac_address, connect to powerspi, initialize the powerspy and launch rt_capture from powerspy class to get data
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
                # Create a default file with the current Unix timestamp
                timestamp = int(time.time())
                file_path = f"powerspy-{timestamp}.csv"
                self.file_path.set(file_path)
                messagebox.showinfo("Default File Created", f"No file selected. Using default file: {file_path}")
            self.file_label.config(text="Saving to: " + file_path, bootstyle=SUCCESS)

        if self.powerspy.sock == None :
            self.status_label.config(text="Status: connecting ...", bootstyle=WARNING)
            self.root.update_idletasks()
            if self.powerspy.connect((mac_address,1)) == 0 :
                if not self.powerspy.init():
                    self.status_label.config(text="Status: Device cannot be initialized retry", bootstyle=DANGER)
                    self.start_button.config(state=NORMAL)
                    return
            else:
                self.status_label.config(text="connexion error, retry several times", bootstyle=DANGER)
                self.start_button.config(state=NORMAL)
        self.status_label.config(text="Status: listening to : Powerspy [" + mac_address + "]", bootstyle=SUCCESS)
        self.stop_button.config(state=NORMAL)
        self.mac_entry.config(state=DISABLED)
        self.toggle_capture(self.file_path.get())
        self.show_data_fields() 

    def stop_process(self):
        self.status_label.config(text="Status: Stopped", bootstyle=DANGER)
        self.start_button.config(state=NORMAL)
        self.stop_button.config(state=DISABLED)
        self.mac_entry.config(state=NORMAL)
        self.powerspy.running = False
        self.hide_data_fields()  

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
            (^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$) |  # 00:1A:2B:3C:4D:5E or 00-1A-2B-3C-4D-5E
            (^([0-9A-Fa-f]{4}\.){2}([0-9A-Fa-f]{4})$)      # 001A.2B3C.4D5E (Cisco format)
        """, re.VERBOSE)
        return bool(address_regex.match(address))

    def run(self):
        self.root.mainloop()
