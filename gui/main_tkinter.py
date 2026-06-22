# main_tkinter_6_v2.py - Enhanced GUI with Queue Parameter Management
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText
from probebot import ProbeBot
import pico
import importlib
import pandas as pd
import os
import sys
import threading
import time
import copy

# Shared cell-scan orchestration (used by BOTH this GUI and the PUDA edge service).
from probot_drivers import orchestrator_probot

# Initialize ProbeBot
probe_bot = ProbeBot()

# Equipment options
EQUIPMENTS = ['Keithley', 'Keysight', 'Ivium']
OPTIONAL_EQUIPMENT = ['Pico']

class App:
    def __init__(self, root):
        self.root = root
        self.root.title('Measurement Application')
        
        # Set initial window size and make it resizable
        self.root.geometry('1150x750')
        self.root.minsize(1000, 700)
        
        # Initialize variables
        self.selected_equipment_name = None
        self.selected_measurement = None
        self.parameters = None
        self.optional_equipment = False
        self.pause_event = None
        self.stop_event = None

        # Unified measurement queue for both regular and custom measurements
        self.measurement_queue = []
        self.is_custom_mode = tk.BooleanVar(value=False)

        #pico
        self.pico_instrument = pico.PicoInstrument()
        
        #added for manual input of cell number to measure
        self.custom_cells_var = tk.BooleanVar()
        self.custom_cells_entry = None
        
        # Parameter update mode
        self.update_queued_params = tk.BooleanVar(value=False)

        # Build GUI
        self.build_gui()

        # Bind the window close event
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def build_gui(self):
        # Set style
        style = ttk.Style()
        style.theme_use('default')

        # Main frame - directly in root window (no canvas wrapper needed)
        main_frame = ttk.Frame(self.root, padding="8")
        main_frame.pack(fill='both', expand=True)

        # Configure grid layout - columns adjust proportionally, no minimum size
        main_frame.columnconfigure(0, weight=1)  # Left column
        main_frame.columnconfigure(1, weight=2)  # Right column (wider)
        
        # Rows expand proportionally
        main_frame.rowconfigure(0, weight=1)  # Equipment (smaller)
        main_frame.rowconfigure(1, weight=2)  # Measurements (larger)
        main_frame.rowconfigure(2, weight=1)  # Parameters (medium)
        main_frame.rowconfigure(3, weight=2)  # Queue (larger)

        # ===== EQUIPMENT SELECTION FRAME =====
        equipment_frame = ttk.LabelFrame(main_frame, text='Equipment Selection', padding="8")
        equipment_frame.grid(row=0, column=0, sticky='nsew', padx=5, pady=5)
        equipment_frame.columnconfigure(0, weight=1)
        equipment_frame.rowconfigure(0, weight=1)  # Equipment listbox

        self.equipment_listbox = tk.Listbox(equipment_frame, height=3, exportselection=False)
        for item in EQUIPMENTS:
            self.equipment_listbox.insert(tk.END, item)
        self.equipment_listbox.grid(row=0, column=0, sticky='nsew', padx=3, pady=3)
        self.equipment_listbox.bind('<<ListboxSelect>>', self.on_equipment_select)

        # Measurement Selection Frame with Scrollbar
        measurement_frame = ttk.LabelFrame(main_frame, text='Measurement Selection', padding="8")
        measurement_frame.grid(row=1, column=0, sticky='nsew', padx=5, pady=5)
        measurement_frame.columnconfigure(0, weight=1)
        measurement_frame.rowconfigure(0, weight=1)  
        measurement_frame.rowconfigure(1, weight=0)
        
        # Create frame for measurement listbox with scrollbar
        measurement_list_frame = ttk.Frame(measurement_frame)
        measurement_list_frame.grid(row=0, column=0, sticky='nsew', padx=3, pady=2)
        measurement_list_frame.columnconfigure(0, weight=1)
        measurement_list_frame.rowconfigure(0, weight=1)

        measurement_scrollbar = ttk.Scrollbar(measurement_list_frame, orient='vertical')
        self.measurement_listbox = tk.Listbox(measurement_list_frame, height=5, exportselection=False,
                                              yscrollcommand=measurement_scrollbar.set)
        measurement_scrollbar.config(command=self.measurement_listbox.yview)
        
        self.measurement_listbox.grid(row=0, column=0, sticky='nsew')
        measurement_scrollbar.grid(row=0, column=1, sticky='ns')
        
        self.measurement_listbox.bind('<<ListboxSelect>>', self.on_measurement_select)
        
        # Bind mousewheel to measurement listbox
        self.measurement_listbox.bind("<Enter>", lambda e: self.measurement_listbox.bind_all("<MouseWheel>", 
            lambda ev: self.measurement_listbox.yview_scroll(int(-1*(ev.delta/120)), "units")))
        self.measurement_listbox.bind("<Leave>", lambda e: self.measurement_listbox.unbind_all("<MouseWheel>"))

        # Use Pico Checkbox
        self.use_pico_var = tk.BooleanVar()
        self.pico_checkbox = ttk.Checkbutton(measurement_frame, text='Use Pico', variable=self.use_pico_var)
        self.pico_checkbox.grid(row=1, column=0, sticky='w', padx=5, pady=(5,2))
        self.use_pico_var.trace_add('write', self.on_use_pico_change)

        # ===== MEASUREMENT PARAMETERS FRAME =====
        parameters_frame = ttk.LabelFrame(main_frame, text='Measurement Parameters', padding="8")
        parameters_frame.grid(row=2, column=0, sticky='nsew', padx=5, pady=5)
       
        parameters_frame.columnconfigure(0, weight=1)
        parameters_frame.rowconfigure(0, weight=1)
        parameters_frame.rowconfigure(1, weight=0)
        parameters_frame.rowconfigure(2, weight=0)
        
        # Parameter input fields - directly in frame
        param_grid = ttk.Frame(parameters_frame)
        param_grid.grid(row=0, column=0, sticky='nsew', padx=3, pady=3)
        param_grid.columnconfigure(1, weight=1)
        param_grid.columnconfigure(3, weight=1)

        ttk.Label(param_grid, text='Start Cell:').grid(row=0, column=0, sticky='e', padx=(2,5), pady=2)
        self.start_cell_entry = ttk.Entry(param_grid, width=8)
        self.start_cell_entry.grid(row=0, column=1, sticky='ew', padx=2, pady=2)

        ttk.Label(param_grid, text='Last Cell:').grid(row=0, column=2, sticky='e', padx=(8,5), pady=2)
        self.last_cell_entry = ttk.Entry(param_grid, width=8)
        self.last_cell_entry.grid(row=0, column=3, sticky='ew', padx=2, pady=2)

        ttk.Label(param_grid, text='No. of Loops:').grid(row=1, column=0, sticky='e', padx=(2,5), pady=2)
        self.num_loop_entry = ttk.Entry(param_grid, width=8)
        self.num_loop_entry.grid(row=1, column=1, sticky='ew', padx=2, pady=2)

        # Custom Cells
        ttk.Label(param_grid, text='Selected Cells:').grid(row=2, column=0, sticky='e', padx=(2,5), pady=2)
        # one more nested frame for checkbox and entry
        custom_cell_frame = ttk.Frame(param_grid)
        custom_cell_frame.grid(row=2, column=1, columnspan=3, sticky='ew', padx=2, pady=2)
        custom_cell_frame.columnconfigure(1, weight=1)
        
        self.custom_cells_checkbox = ttk.Checkbutton(custom_cell_frame, variable=self.custom_cells_var)
        self.custom_cells_checkbox.grid(row=0, column=0, sticky='w')
        
        self.custom_cells_entry = ttk.Entry(custom_cell_frame)
        self.custom_cells_entry.grid(row=0, column=1, sticky='ew', padx=(5,0))

        # Measurement Mode Selection
        ttk.Separator(param_grid, orient='horizontal').grid(row=3, column=0, columnspan=4, sticky='ew', pady=5)
        
        mode_label = ttk.Label(param_grid, text='Measurement Mode:', font=('Arial', 9, 'bold'))
        mode_label.grid(row=4, column=0, columnspan=4, sticky='ew', padx=2, pady=2)
        
        mode_radio_frame = ttk.Frame(param_grid)
        mode_radio_frame.grid(row=5, column=0, columnspan=4, sticky='ew', padx=2, pady=2)
        
        ttk.Radiobutton(mode_radio_frame, text='Regular (Probe Each)', 
                       variable=self.is_custom_mode, value=False, 
                       command=self.update_mode_display).pack(side='left', padx=(0,15))
        
        ttk.Radiobutton(mode_radio_frame, text='Custom (Stay at Cell)', 
                       variable=self.is_custom_mode, value=True, 
                       command=self.update_mode_display).pack(side='left')
        
        self.mode_info_label = ttk.Label(param_grid, text='', foreground='blue', font=('Arial', 8), wraplength=380)
        self.mode_info_label.grid(row=6, column=0, columnspan=4, sticky='w', padx=2, pady=(2,2))
        
        # Edit Parameters Button
        self.edit_params_button = ttk.Button(parameters_frame, text='Edit Parameters', 
                                             state='disabled', command=self.edit_parameters)
        self.edit_params_button.grid(row=1, column=0, padx=5, pady=(5,3), sticky='ew')
        
        # Parameter update mode checkbox
        param_update_frame = ttk.Frame(parameters_frame)
        param_update_frame.grid(row=2, column=0, padx=5, pady=(0,3), sticky='ew')
        
        self.update_params_checkbox = ttk.Checkbutton(
            param_update_frame, 
            text='Update parameters in queued measurements', 
            variable=self.update_queued_params
        )
        self.update_params_checkbox.pack(side='left')
        
        # Add info button
        info_button = ttk.Button(param_update_frame, text='?', width=3, 
                                command=self.show_parameter_update_info)
        info_button.pack(side='left', padx=(5,0))

        # ===== MEASUREMENT QUEUE FRAME =====
        queue_frame = ttk.LabelFrame(main_frame, text='Measurement Sequence', padding="8")
        queue_frame.grid(row=3, column=0, sticky='nsew', padx=5, pady=5)
        
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(0, weight=1)
        queue_frame.rowconfigure(1, weight=0)
        queue_frame.rowconfigure(2, weight=0)  # NEW: Row for additional buttons
        
        queue_container = ttk.Frame(queue_frame)
        queue_container.grid(row=0, column=0, sticky='nsew', padx=3, pady=3)
        queue_container.columnconfigure(0, weight=1)
        queue_container.rowconfigure(0, weight=1)
        
        queue_scroll = ttk.Scrollbar(queue_container, orient='vertical')
        self.queue_listbox = tk.Listbox(queue_container, height = 5, yscrollcommand=queue_scroll.set)
        queue_scroll.config(command=self.queue_listbox.yview)
        
        self.queue_listbox.grid(row=0, column=0, sticky='nsew')
        queue_scroll.grid(row=0, column=1, sticky='ns')
        
        # NEW: Bind double-click to show parameters
        self.queue_listbox.bind('<Double-Button-1>', self.show_queue_item_parameters)
        
        # Bind mousewheel to queue listbox
        self.queue_listbox.bind("<Enter>", lambda e: self.queue_listbox.bind_all("<MouseWheel>", 
            lambda ev: self.queue_listbox.yview_scroll(int(-1*(ev.delta/120)), "units")))
        self.queue_listbox.bind("<Leave>", lambda e: self.queue_listbox.unbind_all("<MouseWheel>"))
        
        # Queue control buttons (first row)
        button_frame = ttk.Frame(queue_frame)
        button_frame.grid(row=1, column=0, sticky='ew', padx=3, pady=(5,3))
        button_frame.columnconfigure((0, 1, 2), weight=1)
        
        self.add_to_queue_button = ttk.Button(button_frame, text='Add to Queue', 
                                              command=self.add_to_queue, state='disabled')
        self.add_to_queue_button.grid(row=0, column=0, padx=2, sticky='ew')
        
        self.remove_from_queue_button = ttk.Button(button_frame, text='Remove Selected', 
                                                   command=self.remove_from_queue)
        self.remove_from_queue_button.grid(row=0, column=1, padx=2, sticky='ew')
        
        self.clear_queue_button = ttk.Button(button_frame, text='Clear Queue', 
                                            command=self.clear_queue)
        self.clear_queue_button.grid(row=0, column=2, padx=2, sticky='ew')

        # NEW: Additional queue management buttons (second row)
        additional_button_frame = ttk.Frame(queue_frame)
        additional_button_frame.grid(row=2, column=0, sticky='ew', padx=3, pady=(0,3))
        additional_button_frame.columnconfigure((0, 1, 2, 3), weight=1)
        
        # View Parameters button
        self.view_params_button = ttk.Button(additional_button_frame, text='View Parameters', 
                                             command=self.show_queue_item_parameters)
        self.view_params_button.grid(row=0, column=0, padx=2, sticky='ew')
        
        # Edit Selected Parameters button
        self.edit_queue_params_button = ttk.Button(additional_button_frame, text='Edit Selected', 
                                                   command=self.edit_queue_item_parameters)
        self.edit_queue_params_button.grid(row=0, column=1, padx=2, sticky='ew')
        
        # Move Up button
        self.move_up_button = ttk.Button(additional_button_frame, text='↑ Move Up', 
                                        command=self.move_queue_item_up)
        self.move_up_button.grid(row=0, column=2, padx=2, sticky='ew')
        
        # Move Down button
        self.move_down_button = ttk.Button(additional_button_frame, text='↓ Move Down', 
                                          command=self.move_queue_item_down)
        self.move_down_button.grid(row=0, column=3, padx=2, sticky='ew')

        # ===== RIGHT COLUMN - ProbeBot Controls and Output =====
        right_container = ttk.Frame(main_frame)
        right_container.grid(row=0, column=1, rowspan=4, sticky='nsew', padx=5, pady=5)
        right_container.rowconfigure(0, weight=0)  # ProbeBot controls - fixed
        right_container.rowconfigure(1, weight=1)  # Output - expands
        right_container.columnconfigure(0, weight=1)

        # ===== PROBEBOT CONTROLS FRAME =====
        controls_frame = ttk.LabelFrame(right_container, text='ProbeBot Controls', padding="8")
        controls_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 5))
        controls_frame.columnconfigure((0, 1, 2), weight=1)

        self.cell_number_entry = ttk.Entry(controls_frame, width=10)
        self.cell_number_entry.grid(row=0, column=0, padx=3, pady=3, sticky='ew')

        self.move_to_cell_button = ttk.Button(controls_frame, text='→ To Cell', command=self.move_to_cell)
        self.move_to_cell_button.grid(row=0, column=1, padx=3, pady=3, sticky='ew')

        self.move_safe_button = ttk.Button(controls_frame, text='Safe Position', command=self.move_to_safeposition)
        self.move_safe_button.grid(row=0, column=2, padx=3, pady=3, sticky='ew')

        # Measurement control buttons
        self.start_button = ttk.Button(controls_frame, text='Start Measurement', 
                                       state='disabled', command=self.start_measurement)
        self.start_button.grid(row=1, column=0, padx=3, pady=3, sticky='ew')

        self.pause_button = ttk.Button(controls_frame, text='Pause', 
                                       state='disabled', command=self.pause_measurement)
        self.pause_button.grid(row=1, column=1, padx=3, pady=3, sticky='ew')

        self.stop_button = ttk.Button(controls_frame, text='Stop', 
                                      state='disabled', command=self.stop_measurement)
        self.stop_button.grid(row=1, column=2, padx=3, pady=3, sticky='ew')

        # ===== OUTPUT FRAME =====
        output_frame = ttk.LabelFrame(right_container, text='Output', padding="8")
        output_frame.grid(row=1, column=0, sticky='nsew')
        
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        
        self.output_text = ScrolledText(output_frame, wrap='word', font=('Courier', 9))
        self.output_text.grid(row=0, column=0, sticky='nsew')
        
        # Bind mousewheel to output text
        self.output_text.bind("<Enter>", lambda e: self.output_text.bind_all("<MouseWheel>", 
            lambda ev: self.output_text.yview_scroll(int(-1*(ev.delta/120)), "units")))
        self.output_text.bind("<Leave>", lambda e: self.output_text.unbind_all("<MouseWheel>"))

        # Redirect stdout
        sys.stdout = self
        
        # Initialize mode display
        self.update_mode_display()

    def show_parameter_update_info(self):
        """Show information about parameter update mode"""
        info_text = (
            "Parameter Update Mode:\n\n"
            "☐ UNCHECKED (Default):\n"
            "Each queued measurement keeps its OWN independent parameters.\n"
            "Use this when you want different parameter values for each measurement.\n\n"
            "☑ CHECKED:\n"
            "When you save parameters, ALL queued measurements of the same type\n"
            "will be updated with the new parameters.\n"
            "Use this when you want to apply the same parameter changes everywhere."
        )
        messagebox.showinfo("Parameter Update Mode", info_text)

    def update_mode_display(self):
        """Update UI based on selected measurement mode"""
        if self.is_custom_mode.get():
            self.mode_info_label.config(text='Custom Mode: Probes stay at selected cell(s), runs queued measurements')
        else:
            self.mode_info_label.config(text='Regular Mode: Probes each cell separately, runs single measurement')

    def on_use_pico_change(self, *args):
        if self.use_pico_var.get():
            self.pico_instrument.light_on()
        else:
            self.pico_instrument.light_off()

    def write(self, message):
        self.output_text.insert(tk.END, message)
        self.output_text.see(tk.END)

    def flush(self):
        pass

    def on_equipment_select(self, event):
        selection = event.widget.curselection()
        if selection:
            index = selection[0]
            self.selected_equipment_name = event.widget.get(index)
            try:
                self.equipment_module = importlib.import_module(self.selected_equipment_name.lower())
            except ImportError:
                messagebox.showerror("Error", f"Failed to import module for {self.selected_equipment_name}.")
                self.measurement_listbox.delete(0, tk.END)
                self.selected_equipment_name = None
                return

            try:            
                measurement_options = self.equipment_module.measurement_list()
            except AttributeError:
                messagebox.showerror("Error", f"Module for {self.selected_equipment_name} does not have a 'measurement_list' function.")
                self.selected_equipment_name = None
                self.measurement_listbox.delete(0, tk.END)
                return
            # Populate the measurement listbox
            self.measurement_listbox.delete(0, tk.END)
            for item in measurement_options:
                self.measurement_listbox.insert(tk.END, item)
        # No selection; reset relevant components    
        else:
            self.measurement_listbox.delete(0, tk.END)
            self.selected_equipment_name = None
        
        # Disable dependent controls
        self.edit_params_button.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.add_to_queue_button.configure(state='disabled')

    def on_measurement_select(self, event):
        selection = event.widget.curselection()
        if selection:
            index = selection[0]
            self.selected_measurement = event.widget.get(index)
            self.edit_params_button.configure(state='normal')
            self.start_button.configure(state='normal')
            self.add_to_queue_button.configure(state='normal')
        else:
            self.selected_measurement = None
            self.edit_params_button.configure(state='disabled')
            self.start_button.configure(state='disabled')
            self.add_to_queue_button.configure(state='disabled')

    def add_to_queue(self):
        """Add selected measurement to the unified queue with independent parameters"""
        if not self.selected_measurement:
            messagebox.showwarning("Warning", "Please select a measurement first.")
            return
        
        # Load parameters for this measurement
        parameters_file = os.path.join('Parameters', f'parameter_{self.selected_measurement}.csv')
        if os.path.isfile(parameters_file):
            parameters_df = pd.read_csv(parameters_file)
            # Create a DEEP COPY of parameters so each queue item has independent parameters
            parameters_copy = parameters_df.copy(deep=True)
        else:
            parameters_copy = None
        
        queue_item = {
            'equipment': self.selected_equipment_name,
            'measurement': self.selected_measurement,
            'parameters': parameters_copy  # Each item gets its own copy
        }
        
        self.measurement_queue.append(queue_item)
        self.queue_listbox.insert(tk.END, f"{len(self.measurement_queue)}. {self.selected_equipment_name} - {self.selected_measurement}")
        
        # NEW: Print parameters to output when added to queue
        self.print_to_output(f"\n{'='*50}")
        self.print_to_output(f"Added to queue: {self.selected_measurement}")
        self.print_parameters_to_output(parameters_copy)
        self.print_to_output(f"{'='*50}\n")
    
    def print_parameters_to_output(self, parameters_df):
        """
        NEW METHOD: Print parameters in a formatted way to the output text box
        """
        if parameters_df is None or parameters_df.empty:
            self.print_to_output("  No parameters stored for this measurement.")
            return
        
        self.print_to_output("  Parameters:")
        self.print_to_output("  " + "-" * 40)
        
        # Find the maximum parameter name length for alignment
        max_param_length = max(len(str(row['Parameter'])) for _, row in parameters_df.iterrows())
        
        for _, row in parameters_df.iterrows():
            param_name = str(row['Parameter'])
            param_value = str(row['Value'])
            # Format with padding for alignment
            self.print_to_output(f"  {param_name:<{max_param_length}} : {param_value}")
        
        self.print_to_output("  " + "-" * 40)
    
    def show_queue_item_parameters(self, event=None):
        """
        NEW METHOD: Show parameters for selected queue item in a popup window
        Can be called by double-click or button press
        """
        selection = self.queue_listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a measurement from the queue first.")
            return
        
        index = selection[0]
        queue_item = self.measurement_queue[index]
        
        # Create popup window
        param_window = tk.Toplevel(self.root)
        param_window.title(f'Parameters - {queue_item["measurement"]}')
        param_window.geometry('450x500')
        
        # Header label
        header_frame = ttk.Frame(param_window, padding="10")
        header_frame.pack(fill='x')
        
        ttk.Label(header_frame, text=f'Equipment: {queue_item["equipment"]}', 
                 font=('Arial', 10, 'bold')).pack(anchor='w')
        ttk.Label(header_frame, text=f'Measurement: {queue_item["measurement"]}', 
                 font=('Arial', 10, 'bold')).pack(anchor='w')
        
        ttk.Separator(param_window, orient='horizontal').pack(fill='x', pady=5)
        
        # Parameters display
        if queue_item['parameters'] is None or queue_item['parameters'].empty:
            ttk.Label(param_window, text='No parameters stored for this measurement.', 
                     font=('Arial', 10), foreground='red', padding="20").pack()
        else:
            # Create scrollable frame for parameters
            container = ttk.Frame(param_window)
            container.pack(fill='both', expand=True, padx=10, pady=5)
            
            canvas = tk.Canvas(container)
            scrollbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
            scrollable_frame = ttk.Frame(canvas)
            
            scrollable_frame.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
            )
            
            canvas.create_window((0, 0), window=scrollable_frame, anchor='nw')
            canvas.configure(yscrollcommand=scrollbar.set)
            
            canvas.pack(side='left', fill='both', expand=True)
            scrollbar.pack(side='right', fill='y')
            
            # Bind mousewheel
            canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", 
                lambda ev: canvas.yview_scroll(int(-1*(ev.delta/120)), "units")))
            canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
            
            # Display parameters in a grid
            ttk.Label(scrollable_frame, text='Parameter', font=('Arial', 9, 'bold')).grid(
                row=0, column=0, sticky='w', padx=10, pady=5)
            ttk.Label(scrollable_frame, text='Value', font=('Arial', 9, 'bold')).grid(
                row=0, column=1, sticky='w', padx=10, pady=5)
            
            for idx, (_, row) in enumerate(queue_item['parameters'].iterrows(), start=1):
                ttk.Label(scrollable_frame, text=str(row['Parameter'])).grid(
                    row=idx, column=0, sticky='w', padx=10, pady=2)
                ttk.Label(scrollable_frame, text=str(row['Value'])).grid(
                    row=idx, column=1, sticky='w', padx=10, pady=2)
        
        # Close button
        ttk.Button(param_window, text='Close', command=param_window.destroy).pack(pady=10)
    
    def edit_queue_item_parameters(self):
        """
        NEW METHOD: Edit parameters for selected queue item directly (not from CSV)
        """
        selection = self.queue_listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a measurement from the queue first.")
            return
        
        index = selection[0]
        queue_item = self.measurement_queue[index]
        
        if queue_item['parameters'] is None or queue_item['parameters'].empty:
            messagebox.showerror("Error", "No parameters available for this measurement.")
            return
        
        # Create a working copy of the parameters
        parameters_df = queue_item['parameters'].copy(deep=True)
        
        # Create edit window
        param_window = tk.Toplevel(self.root)
        param_window.title(f'Edit Parameters - Queue Item #{index+1}')
        param_window.geometry('450x550')
        
        # Header
        header_frame = ttk.Frame(param_window, padding="10")
        header_frame.pack(fill='x')
        
        ttk.Label(header_frame, text=f'Editing Queue Item #{index+1}', 
                 font=('Arial', 11, 'bold')).pack(anchor='w')
        ttk.Label(header_frame, text=f'Equipment: {queue_item["equipment"]}').pack(anchor='w')
        ttk.Label(header_frame, text=f'Measurement: {queue_item["measurement"]}').pack(anchor='w')
        
        ttk.Separator(param_window, orient='horizontal').pack(fill='x', pady=5)
        
        # Create scrollable parameter edit area
        param_window.rowconfigure(0, weight=0)
        param_window.rowconfigure(1, weight=1)
        param_window.rowconfigure(2, weight=0)
        param_window.columnconfigure(0, weight=1)
        
        container = ttk.Frame(param_window)
        container.pack(fill='both', expand=True, padx=10, pady=5)
        
        canvas = tk.Canvas(container)
        scrollbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        # Bind mousewheel
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", 
            lambda ev: canvas.yview_scroll(int(-1*(ev.delta/120)), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        
        ttk.Label(scrollable_frame, text='Edit Parameters:', 
                 font=('Arial', 10, 'bold')).pack(anchor='w', padx=10, pady=10)
        
        # Create entry fields for each parameter
        entries = []
        for param_index, row in parameters_df.iterrows():
            frame = ttk.Frame(scrollable_frame)
            frame.pack(fill='x', padx=10, pady=3)
            label = ttk.Label(frame, text=row['Parameter'], width=25)
            label.pack(side='left')
            entry = ttk.Entry(frame, width=20)
            entry.insert(0, str(row['Value']))
            entry.pack(side='left', padx=5)
            entries.append((param_index, entry))
        
        # Save button
        def save_queue_parameters():
            """Save the edited parameters back to the queue item only"""
            for param_index, entry in entries:
                value = entry.get()
                parameters_df.at[param_index, 'Value'] = value
            
            # Update the queue item's parameters (NOT the CSV file)
            self.measurement_queue[index]['parameters'] = parameters_df.copy(deep=True)
            
            messagebox.showinfo("Success", 
                f"Parameters updated for queue item #{index+1}.\n"
                "Note: CSV file was NOT modified.")
            
            self.print_to_output(f"\nUpdated parameters for queue item #{index+1}: {queue_item['measurement']}")
            param_window.destroy()
        
        button_frame = ttk.Frame(param_window)
        button_frame.pack(fill='x', padx=10, pady=10)
        
        ttk.Button(button_frame, text='Save Parameters', 
                  command=save_queue_parameters).pack(side='left', padx=5)
        ttk.Button(button_frame, text='Cancel', 
                  command=param_window.destroy).pack(side='left', padx=5)
    
    def move_queue_item_up(self):
        """
        NEW METHOD: Move selected queue item up in the list
        """
        selection = self.queue_listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a measurement from the queue first.")
            return
        
        index = selection[0]
        
        # Can't move first item up
        if index == 0:
            messagebox.showinfo("Info", "This item is already at the top.")
            return
        
        # Swap in the measurement_queue list
        self.measurement_queue[index], self.measurement_queue[index-1] = \
            self.measurement_queue[index-1], self.measurement_queue[index]
        
        # Refresh the listbox display with updated numbering
        self.refresh_queue_display()
        
        # Select the moved item at its new position
        self.queue_listbox.selection_clear(0, tk.END)
        self.queue_listbox.selection_set(index-1)
        self.queue_listbox.see(index-1)
        
        self.print_to_output(f"Moved queue item from position {index+1} to {index}")
    
    def move_queue_item_down(self):
        """
        NEW METHOD: Move selected queue item down in the list
        """
        selection = self.queue_listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a measurement from the queue first.")
            return
        
        index = selection[0]
        
        # Can't move last item down
        if index == len(self.measurement_queue) - 1:
            messagebox.showinfo("Info", "This item is already at the bottom.")
            return
        
        # Swap in the measurement_queue list
        self.measurement_queue[index], self.measurement_queue[index+1] = \
            self.measurement_queue[index+1], self.measurement_queue[index]
        
        # Refresh the listbox display with updated numbering
        self.refresh_queue_display()
        
        # Select the moved item at its new position
        self.queue_listbox.selection_clear(0, tk.END)
        self.queue_listbox.selection_set(index+1)
        self.queue_listbox.see(index+1)
        
        self.print_to_output(f"Moved queue item from position {index+1} to {index+2}")
    
    def refresh_queue_display(self):
        """
        NEW METHOD: Refresh the queue listbox display with correct numbering
        """
        self.queue_listbox.delete(0, tk.END)
        for i, item in enumerate(self.measurement_queue):
            self.queue_listbox.insert(tk.END, 
                f"{i+1}. {item['equipment']} - {item['measurement']}")
    
    def remove_from_queue(self):
        """Remove selected item from queue"""
        selection = self.queue_listbox.curselection()
        if selection:
            index = selection[0]
            self.queue_listbox.delete(index)
            removed_item = self.measurement_queue.pop(index)
            
            # Renumber remaining items using the new refresh method
            self.refresh_queue_display()
            
            self.print_to_output(f"Removed from queue: {removed_item['measurement']}")
    
    def clear_queue(self):
        """Clear all items from queue"""
        self.queue_listbox.delete(0, tk.END)
        self.measurement_queue.clear()
        self.print_to_output("Queue cleared")

    def edit_parameters(self):
        parameters_file = os.path.join('Parameters', f'parameter_{self.selected_measurement}.csv')
        if not os.path.isfile(parameters_file):
            messagebox.showerror("Error", f"Parameters file '{parameters_file}' not found.")
            return

        parameters_df = pd.read_csv(parameters_file)
        self.parameters = parameters_df

        param_window = tk.Toplevel(self.root)
        param_window.title('Edit Parameters')
        param_window.geometry('400x500')

        param_window.rowconfigure(0, weight=1)
        param_window.rowconfigure(1, weight=0)
        param_window.columnconfigure(0, weight=1)

        container = ttk.Frame(param_window)
        container.grid(row=0, column=0, sticky='nsew')

        canvas = tk.Canvas(container)
        scrollbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        # Bind mousewheel to edit parameters canvas
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", 
            lambda ev: canvas.yview_scroll(int(-1*(ev.delta/120)), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(scrollable_frame, text='Edit Parameters:', font=('Arial', 11, 'bold')).pack(anchor='w', padx=10, pady=10)

        entries = []
        for index, row in parameters_df.iterrows():
            frame = ttk.Frame(scrollable_frame)
            frame.pack(fill='x', padx=10, pady=3)
            label = ttk.Label(frame, text=row['Parameter'], width=20)
            label.pack(side='left')
            entry = ttk.Entry(frame, width=15)
            entry.insert(0, str(row['Value']))
            entry.pack(side='left', padx=5)
            entries.append((index, entry))

        save_button = ttk.Button(param_window, text='Save Parameters', 
                                command=lambda: self.save_parameters(entries, parameters_df, parameters_file, param_window))
        save_button.grid(row=1, column=0, pady=10, padx=10, sticky='ew')

    def save_parameters(self, entries, parameters_df, parameters_file, param_window):
        """Save parameters and optionally update queued measurements"""
        for index, entry in entries:
            value = entry.get()
            parameters_df.at[index, 'Value'] = value
        self.parameters = parameters_df
        
        try:
            parameters_df.to_csv(parameters_file, index=False)
            
            # Check if we should update queued measurements
            if self.update_queued_params.get():
                updated_count = 0
                for queue_item in self.measurement_queue:
                    if (queue_item['equipment'] == self.selected_equipment_name and 
                        queue_item['measurement'] == self.selected_measurement):
                        # Update this queued item's parameters
                        queue_item['parameters'] = parameters_df.copy(deep=True)
                        updated_count += 1
                
                if updated_count > 0:
                    messagebox.showinfo("Success", 
                        f"Parameters saved to CSV and updated in {updated_count} queued measurement(s).")
                    self.print_to_output(f"Updated parameters in {updated_count} queued measurement(s)")
                else:
                    messagebox.showinfo("Success", "Parameters saved to CSV. No matching queued measurements to update.")
            else:
                messagebox.showinfo("Success", "Parameters saved to CSV. Queued measurements keep their original parameters.")
                
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save parameters to CSV file: {e}")
        
        param_window.destroy()

    def start_measurement(self):
        """Unified start function for both regular and custom measurements"""
        
        # Check if queue is being used or single measurement
        use_queue = len(self.measurement_queue) > 0
        
        if not use_queue:
            # Single measurement mode - need equipment and measurement selected
            if not self.selected_equipment_name or not self.selected_measurement:
                messagebox.showerror("Error", "Please select both equipment and measurement, or add measurements to queue.")
                return
            
            if self.parameters is None:
                messagebox.showerror("Error", "Parameters not loaded. Please edit parameters first.")
                return

        # Get cell parameters
        start_cell_input = self.start_cell_entry.get().strip()
        last_cell_input = self.last_cell_entry.get().strip()
        num_loop_input = self.num_loop_entry.get().strip()

        # Parse cells
        custom_cells = None
        if self.custom_cells_var.get():
            custom_cells_input = self.custom_cells_entry.get().strip()
            try:
                custom_cells = [int(cell.strip()) for cell in custom_cells_input.split(',')]
                if not all(1 <= cell <= 81 for cell in custom_cells):
                    messagebox.showerror("Error", "All custom cell numbers must be between 1 and 81.")
                    return
            except ValueError:
                messagebox.showerror("Error", "Custom cell numbers must be integers separated by commas.")
                return
        else:
            # Use start and last cell
            try:
                start_cell = int(start_cell_input)
                last_cell = int(last_cell_input)
                if not (1 <= start_cell <= 81) or not (1 <= last_cell <= 81):
                    messagebox.showerror("Error", "Start Cell and Last Cell must be between 1 and 81.")
                    return
                if start_cell > last_cell:
                    messagebox.showerror("Error", "Start Cell must be less than or equal to Last Cell.")
                    return
                custom_cells = list(range(start_cell, last_cell + 1))
            except ValueError:
                messagebox.showerror("Error", "Start Cell and Last Cell must be integers.")
                return

        try:
            num_loop = int(num_loop_input)
            if num_loop < 1:
                messagebox.showerror("Error", "Number of loops must be at least 1.")
                return
        except ValueError:
            messagebox.showerror("Error", "Number of loops must be an integer.")
            return

        # Setup optional equipment
        self.optional_equipment = self.use_pico_var.get()
        if self.optional_equipment:
            self.pico_instrument.light_on()
            pico_equipment = self.pico_instrument
        else:
            pico_equipment = None

        # Create events
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()

        # Disable buttons
        self.start_button.configure(state='disabled')
        self.pause_button.configure(state='normal')
        self.stop_button.configure(state='normal')

        # Choose measurement mode
        if self.is_custom_mode.get():
            # Custom mode - stay at cell
            self.print_to_output(f"\n{'='*50}\nStarting CUSTOM measurement mode\n{'='*50}")
            threading.Thread(target=self.custom_measurement_thread, 
                           args=(custom_cells, num_loop, use_queue), daemon=True).start()
        else:
            # Regular mode - probe each cell
            self.print_to_output(f"\n{'='*50}\nStarting REGULAR measurement mode\n{'='*50}")
            threading.Thread(target=self.regular_measurement_thread, 
                           args=(custom_cells, num_loop, use_queue), daemon=True).start()

    def _build_scan_plan(self, use_queue):
        """Build the (plan, run_measurement) pair for the shared orchestrator.

        Keeps the GUI's existing plugin dispatch (execute_measurement /
        execute_single_measurement) as the per-measurement step, so multi-equipment
        queues and the current parameter handling are unchanged.
        """
        if use_queue:
            plan = list(self.measurement_queue)
            run_measurement = lambda item, cell: self.execute_measurement(item, cell)
        else:
            plan = [{'measurement': self.selected_measurement}]
            run_measurement = lambda item, cell: self.execute_single_measurement(cell)
        return plan, run_measurement

    def regular_measurement_thread(self, cell_list, num_loop, use_queue):
        """Regular measurement: probe/unprobe for each cell (shared orchestrator)."""
        try:
            plan, run_measurement = self._build_scan_plan(use_queue)
            orchestrator_probot.run_scan(
                None, probe_bot, plan,
                cells=cell_list, num_loops=num_loop, mode="regular",
                should_stop=self.stop_event.is_set,
                is_paused=self.pause_event.is_set,
                on_progress=self.print_to_output,
                run_measurement=run_measurement,
            )
            self.print_to_output(f"\n{'='*50}\nRegular measurement completed\n{'='*50}")
            self.root.after(0, self.measurement_complete)

        except Exception as e:
            self.print_to_output(f"Error: {e}")
            self.root.after(0, self.measurement_error, str(e))

    def custom_measurement_thread(self, cell_list, num_loop, use_queue):
        """Custom measurement: probe once per cell, no auto return-to-safe (GUI prompts)."""
        try:
            plan, run_measurement = self._build_scan_plan(use_queue)
            orchestrator_probot.run_scan(
                None, probe_bot, plan,
                cells=cell_list, num_loops=num_loop, mode="custom",
                should_stop=self.stop_event.is_set,
                is_paused=self.pause_event.is_set,
                on_progress=self.print_to_output,
                run_measurement=run_measurement,
            )
            self.print_to_output(f"\n{'='*50}\nCustom measurement completed\n{'='*50}")
            self.root.after(0, self.custom_measurement_complete)

        except Exception as e:
            self.print_to_output(f"Error: {e}")
            self.root.after(0, self.measurement_error, str(e))

    def execute_measurement(self, queue_item, cell_number):
        """Execute a single measurement from queue using its stored parameters"""
        try:
            equipment_module = importlib.import_module(queue_item['equipment'].lower())
            equipment_class_name = f'{queue_item["equipment"]}Instrument'
            equipment_class = getattr(equipment_module, equipment_class_name, None)
            
            if equipment_class is None:
                self.print_to_output(f"Error: Equipment class not found")
                return
            
            equipment = equipment_class()
            
            # Load the parameters from this specific queue item
            if queue_item['parameters'] is not None:
                # Save queue item's parameters temporarily to the CSV file
                parameters_file = os.path.join('Parameters', f'parameter_{queue_item["measurement"]}.csv')
                queue_item['parameters'].to_csv(parameters_file, index=False)
            
            measurement_function = getattr(equipment, queue_item['measurement'], None)
            
            if measurement_function is None:
                self.print_to_output(f"Error: Measurement function not found")
                return
            
            measurement_function(cell_number)
            
        except Exception as e:
            self.print_to_output(f"    Error executing measurement: {e}")

    def execute_single_measurement(self, cell_number):
        """Execute the currently selected single measurement"""
        try:
            equipment_class_name = f'{self.selected_equipment_name}Instrument'
            equipment_class = getattr(self.equipment_module, equipment_class_name, None)
            
            if equipment_class is None:
                self.print_to_output(f"Error: Equipment class not found")
                return
            
            equipment = equipment_class()
            measurement_function = getattr(equipment, self.selected_measurement, None)
            
            if measurement_function is None:
                self.print_to_output(f"Error: Measurement function not found")
                return
            
            measurement_function(cell_number)
            
        except Exception as e:
            self.print_to_output(f"Error executing measurement: {e}")

    def measurement_complete(self):
        """Called when regular measurement completes"""
        messagebox.showinfo('Info', 'Measurement process completed successfully.')
        self.start_button.configure(state='normal')
        self.pause_button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.pause_button.configure(text='Pause')
        self.pause_event = None
        self.stop_event = None

    def custom_measurement_complete(self):
        """Called when custom measurement completes - asks about safe position"""
        self.start_button.configure(state='normal')
        self.pause_button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.pause_button.configure(text='Pause')
        self.pause_event = None
        self.stop_event = None
        
        # Ask user if they want to move to safe position
        response = messagebox.askyesno('Custom Measurement Complete', 
                                       'All measurements completed.\n\nMove probes to safe position?')
        if response:
            self.print_to_output("\nMoving to safe position...")
            threading.Thread(target=self.move_to_safe_with_feedback, daemon=True).start()
        else:
            self.print_to_output("Probes remain at current position.")

    def move_to_safe_with_feedback(self):
        """Move to safe position and provide feedback when complete"""
        try:
            probe_bot.move_to_safeposition()
            self.print_to_output("✓ Reached safe position")
        except Exception as e:
            self.print_to_output(f"Error moving to safe position: {e}")

    def measurement_error(self, error_message):
        messagebox.showerror('Error', f"An error occurred during measurement:\n{error_message}")
        self.start_button.configure(state='normal')
        self.pause_button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.pause_button.configure(text='Pause')
        self.pause_event = None
        self.stop_event = None

    def pause_measurement(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.configure(text='Pause')
            self.print_to_output("Resuming measurement...")
        else:
            self.pause_event.set()
            self.pause_button.configure(text='Resume')
            self.print_to_output("Pausing measurement after current operation...")

    def stop_measurement(self):
        self.stop_event.set()
        self.pause_button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.print_to_output("Stopping measurement...")

    def move_to_cell(self):
        cell_input = self.cell_number_entry.get().strip()
        try:
            cell_number = int(cell_input)
            if 1 <= cell_number <= 81:
                threading.Thread(target=self.move_to_cell_backend, args=(cell_number,), daemon=True).start()
            else:
                messagebox.showerror("Error", "Cell number must be between 1 and 81.")
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid integer for the cell number.")

    def move_to_cell_backend(self, cell_number):
        self.print_to_output(f"Moving to cell {cell_number}...")
        coordinates = probe_bot.cell_coordinates()
        position = coordinates[cell_number - 1]
        probe_bot.move_to(position)
        self.print_to_output(f"✓ Reached cell {cell_number}")
        
    def move_to_safeposition(self):
        self.print_to_output("Moving to safe position...")
        threading.Thread(target=self.move_to_safe_with_feedback, daemon=True).start()

    def print_to_output(self, message):
        self.output_text.insert(tk.END, message + '\n')
        self.output_text.see(tk.END)

    def on_closing(self):
        if self.optional_equipment:
            self.pico_instrument.light_off()
        self.root.destroy()

    def __del__(self):
        if self.optional_equipment:
            self.pico_instrument.light_off()

root = tk.Tk()
app = App(root)
root.mainloop()