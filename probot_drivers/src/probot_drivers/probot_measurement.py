"""Keysight SMU measurement primitives for the probot platform.

The ~22 ``Keysight_*`` routines (plus their SCPI/data helpers) are ported
*verbatim* from the original ``keysight.py`` ``KeysightInstrument`` class and
exposed as a mixin on
:class:`~probot_drivers.probot_machine_smu.SMUKeysightProbotMachine`.

The host class must provide:

* ``self.smu`` - the raw PyVISA resource (supplied by
  :class:`~probot_drivers.probot_smu_keysight.SMUKeysightProbot`),
* ``self.pico_instrument`` - the light driver
  (:class:`~probot_drivers.probot_pico.PicoProbot`),
* ``self._param_dir`` / ``self._data_dir`` - configurable I/O directories.

Only the original ``__init__`` (hardware wiring) was dropped and the I/O paths /
heavy imports were made configurable / lazy; the measurement bodies are unchanged
so behaviour matches the original platform exactly. Parameters are still injected
into the module globals via ``globals()[name] = value`` and read as bare names,
which is why every measurement lives in this single module.
"""

import csv
import os
import ast
import functools
from datetime import datetime
from typing import Any, Dict
import logging

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from scipy.stats import linregress

from .analysis import pv_param as PV_calc

logger = logging.getLogger(__name__)


def _measurement_result(fn):
    """Decorator giving a measurement a uniform ``Dict[str, Any]`` return.

    The measurement bodies were ported verbatim and mostly persist their data to
    CSV rather than returning it. This wrapper resets the per-run output
    accumulator, runs the body, and packages a JSON-serialisable result dict
    without changing the body itself.

    Returns:
        Dict[str, Any]: result envelope with keys:
            - ``measurement`` (str): the measurement name.
            - ``cell_number`` (int): the cell that was measured.
            - ``outputs`` (list[dict]): one record per saved file, each
              ``{"file": str, "keyword": str, "data": {column: [values]}}``,
              collected by :meth:`ProbotMeasurement.savefile` /
              :meth:`ProbotMeasurement.savefile_1`.
            - ``result`` (Any): whatever the underlying routine returned (often
              ``None`` or, for the fitting routines, a results DataFrame).
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self._outputs = []
        result = fn(self, *args, **kwargs)
        cell_number = args[0] if args else kwargs.get("cell_number")
        return {
            "measurement": fn.__name__,
            "cell_number": cell_number,
            "outputs": self._outputs,
            "result": result,
        }
    return wrapper


# Canonical ordered list of measurement primitives (from the original keysight.py).
MEASUREMENT_NAMES = [
    'Keysight_Voc_decay_ON_OFF_Variation', 'Keysight_Voc_decay_indiv_soaking',
    'Keysight_Voc_decay', 'Keysight_HT_PotDep', 'Keysight_Potent_Depress_2',
    'Keysight_Potent_Depress', 'Keysight_Voltage_Steady', 'Keysight_Voltage_list',
    'Keysight_JV_PV', 'Keysight_Substrate_R', 'Keysight_Digital_Sweep',
    'Keysight_Digital_Retention', 'Keysight_Analog_Sweep', 'Keysight_set_reset_sweep',
    'Keysight_analog_pulse', 'Keysight_Paired_Pulse_Facilitation',
    'Keysight_Spike_Duration_DP', 'Keysight_Spike_Voltage_DP', 'Keysight_Light_Pulse',
    'Keysight_Voc_profile', 'Keysight_Jsc_profile', 'Keysight_Time_Gap',
]


def measurement_list():
    """Return the ordered list of available measurement primitive names."""
    return list(MEASUREMENT_NAMES)


class ProbotMeasurement:
    """Mixin providing the probot Keysight SMU measurement routines.

    Mixed into :class:`~probot_drivers.probot_machine_smu.SMUKeysightProbotMachine`,
    this class contributes the ~22 ``Keysight_*`` measurement primitives plus their
    low-level SCPI / data-handling helpers. The routines were ported verbatim from
    the original ``keysight.py`` so on-instrument behaviour is unchanged.

    The mixin relies on the host class to provide:

    * ``self.smu`` - the raw PyVISA resource (the SCPI session),
    * ``self.pico_instrument`` - the Pico light driver (used by the
      light-synchronized measurements),
    * ``self._param_dir`` / ``self._data_dir`` - configurable I/O directories,
      resolved through :meth:`_param_file` and :meth:`_data_path`.

    Method families:

    * **SCPI / data helpers** - :meth:`make_voltage_pulses`,
      :meth:`send_pulse_train_to_keysight`,
      :meth:`send_pulse_train_to_keysight_light_pulse`, :meth:`string_to_dataframe`.
    * **Persistence / plotting** - :meth:`savefile`, :meth:`savefile_1`,
      :meth:`make_graph`, :meth:`make_graph_IV`, :meth:`make_graph_IV_1`.
    * **Measurements** - the ``Keysight_*`` routines, each taking a 1-based
      ``cell_number`` and loading its settings from
      ``parameter_<MeasurementName>.csv`` in the parameter directory.
    * **Analysis** - :meth:`Pot_Dep_Calculation` (and ``Keysight_HT_PotDep``)
      delegate to :mod:`probot_drivers.analysis`.

    Note: most measurements read their CSV parameters into the *module globals*
    (``globals()[name] = value``) and then reference them as bare names, which is
    why every measurement lives in this single module.
    """

    def _param_file(self, name):
        """Resolve a parameter CSV inside the configurable parameter directory.

        Falls back to a case-insensitive match (the source uses inconsistent
        casing, e.g. ``Voltage_Steady`` vs ``voltage_steady``) so the routines
        work on case-sensitive filesystems too.
        """
        path = os.path.join(self._param_dir, name)
        if os.path.exists(path):
            return path
        try:
            lower = name.lower()
            for fn in os.listdir(self._param_dir):
                if fn.lower() == lower:
                    return os.path.join(self._param_dir, fn)
        except OSError:
            pass
        return path

    def _data_path(self, *parts):
        """Return (creating if needed) a data output directory under the data root."""
        d = os.path.join(self._data_dir, *parts)
        os.makedirs(d, exist_ok=True)
        return d

    def _htpd(self):
        """Lazily import the HT_PotDep analysis module and point it at our I/O dirs."""
        from .analysis import ht_potdep as HTPD
        HTPD.PARAM_DIR = self._param_dir
        HTPD.DATA_DIR = self._data_dir
        return HTPD

    def _record_output(self, file_path, keyword, table) -> Dict[str, Any]:
        """Record one saved-file payload on the per-run output accumulator.

        Called by :meth:`savefile` / :meth:`savefile_1`. The collected records are
        returned to the caller by the :func:`_measurement_result` wrapper.

        Args:
            file_path: path the data was written to.
            keyword: file-name keyword for this save.
            table: the saved measurement table (DataFrame).

        Returns:
            Dict[str, Any]: ``{"file", "keyword", "data"}`` where ``data`` is the
            table as ``{column: [values]}`` (or ``None`` if it cannot be serialised).
        """
        try:
            data = table.to_dict(orient="list") if hasattr(table, "to_dict") else None
        except Exception:
            data = None
        rec = {"file": file_path, "keyword": keyword, "data": data}
        self._outputs = getattr(self, "_outputs", [])
        self._outputs.append(rec)
        return rec

    def make_voltage_pulses(self,pulse_voltage,read_voltage,trigger_period,measure_delay_position,pulse_duration,read_duration,no_of_pulses):
        #code to make voltage pulses
        """Build a repeated read+write voltage pulse train.

        Constructs one pulse (``read_voltage`` held for ``read_duration`` followed by
        ``pulse_voltage`` held for ``pulse_duration``, sampled every ``trigger_period``
        seconds) and repeats it ``no_of_pulses`` times.

        Returns:
            tuple: ``(pulse_train, pulse_train_string, trigger_count, measure_delay)`` -
            the numpy waveform, its comma-joined string for the SCPI ``:LIST:VOLT``
            command, the total trigger count, and the per-trigger measurement delay.
        """
        pulse_single = np.empty(int((read_duration+pulse_duration)/trigger_period),dtype = float)
        for i in range (0,int(read_duration/trigger_period)):
            pulse_single[i] = read_voltage
        for i in range(int(read_duration/trigger_period),int((read_duration+pulse_duration)/trigger_period)):
            pulse_single[i] = pulse_voltage
        
        pulse_train = np.array(pulse_single)
        for i in range (1,no_of_pulses):
            pulse_train = np.append(pulse_train, pulse_single)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(no_of_pulses*(read_duration+pulse_duration)/trigger_period))
        measure_delay = str(trigger_period * measure_delay_position)

        return pulse_train, pulse_train_string, trigger_count, measure_delay
        
        #pass
    
    
    def send_pulse_train_to_keysight(self, compliance, pulse_train_string,trigger_count,trigger_period,measure_delay):
        
        """Run one voltage-list sweep on the SMU and return the raw buffer string.

        Resets and configures the SMU as a voltage-list source that measures current and
        voltage with the given ``compliance`` (mA), loads ``pulse_train_string``, runs
        ``trigger_count`` timer-triggered points spaced by ``trigger_period`` seconds
        (measuring ``measure_delay`` into each point), then fetches the buffer.

        Returns:
            str: comma-separated ``time,voltage,current`` triples, or ``'NONE'`` on error.
        """
        print('SENDING COMMANDS TO INSTRUMENT')
        try:
            # ********** INITIALIZE SMU **********
            self.smu.timeout = 10000000  # 10000s
            self.smu.write_termination = '\n' # To define end of command.
            self.smu.read_termination = '\n' # To define end of command.
            self.smu.write("*RST") # Resets the volatile memory.
            self.smu.write("*CLS") # Clears the command queue.
           
            # ********** CONFIGURE MEASUREMENT **********

            # All the commands are for channel 1 (front). Please make sure thatthe connections are made to channel 1.
            self.smu.write(":TRAC:CLE")  # Clear buffer
            #self.smu.write(f":TRAC:POIN {trigger_count}") # This command will not work if FEED CONT NEXT. Set the number of points in the buffer. The buffer can store 100000 points.
            self.smu.write(":TRAC:FEED SENS") # The buffer stores measured data.
            self.smu.write(":TRAC:FEED:CONT NEXT") # Make the buffer editable.
            self.smu.write(":TRAC:TST:FORM ABS") # Format of stored timestamps.
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR") # Reading operation stores time, voltage, and current to the buffer.
            self.smu.write(":SOUR:FUNC:MODE VOLT") # Set the source to supply voltage.
            self.smu.write(":SOUR:VOLT:MODE LIST") # Supply voltage as a list.
            self.smu.write(":SENS:FUNCtion 'CURR', 'VOLT'")
            self.smu.write(f":SENS:CURR:PROT {str(compliance/1000)}") # Set compliance current.
            #self.smu.write(":SENS:REM ON") #4-wire measurement mode
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON")
            self.smu.write(f":LIST:VOLT {pulse_train_string}") # Send the voltage list that was previously generated.
            self.smu.write(f":TRIG:COUN {trigger_count}") # Set the number of triggers.
            self.smu.write(":TRIG:SOUR TIM") # Set the source of commands trigger as time. The trigger duration depends on time. 
            self.smu.write(f":TRIG:TIM {str(trigger_period)}") # Set the duration of trigger.
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}") # Set the measure delay. Preferably, the measurement is performed at the centre of the trigger.

                # ********** RUN MEASUREMENT ********** 
            print('INNITIATE MEASURREMENT')
            self.smu.write(":OUTP ON") # Turns SMU output on.
            self.smu.write(":INIT") # Initiate measurement.
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            print(self.smu.query("SYST:ERR?")) # Check error buffer. If there was any error in the execution of SCPI commands. 
            #self.smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        return output_string


    
    def string_to_dataframe(self,output_string):# change the output data (string) to dataframe
        """Parse the SMU buffer string into a DataFrame.

        Args:
            output_string: comma-separated ``time,voltage,current`` values from the SMU.

        Returns:
            pandas.DataFrame with columns ``['Voltage (V)', 'Current (A)', 'Time (s)']``.
        """
        try:
            output_array = np.array(output_string.split(','), float) # Split the string into a 1-D float array.
            split_columns = np.reshape(output_array, (int(output_array.size/3),3)) # Reshape the array into a 2-D matrix such that the time, voltage, and current values are separated into columns.
            output_table = pd.DataFrame(split_columns, columns = ['Voltage (V)', 'Current (A)', 'Time (s)']) # Store the output as a datframe for easier handling. 
            print('Change output_string to output_table')

            return output_table
        except:
            print('No output_string to convert to df')
            pass
    
    def savefile(self,output_table,df_parameters,cell_number,keyword):
        """Save a measurement + its parameters to a timestamped CSV.

        Writes ``{keyword}_{cell:02d}_{datetime}.csv`` under the data directory
        (``1_reservoir``), concatenating ``output_table`` with ``df_parameters``.
        """
        try:
            #folder to save
            folder = self._data_path('1_reservoir')
            os.makedirs(folder, exist_ok=True)
            #current date time
            current_datetime = datetime.now().strftime('%Y%m%d_T%H%M%S')
            
            df = pd.concat([output_table, df_parameters], axis=1)

            sampleid = cell_number
            if (sampleid < 10):
                sampleid = "0"+str(sampleid)

            file_name = f'{keyword}_{sampleid}_{current_datetime}.csv'
            #df.to_csv(file_name, index=False)
            #file_name = "Sample{}.csv".format(sampleid)
            file_path = os.path.join(folder, file_name)
            df.to_csv(file_path, index=False)
            print("File saved as: " + file_name)
            return self._record_output(file_path, keyword, output_table)
        except:
            print('no file to save')
            return None

    def savefile_1(self,output_table,df_parameters,cell_number,keyword,plot_type):
        """Save a measurement + parameters to a timestamped CSV and render its plot.

        Like :meth:`savefile` but stores under the ``Rohit_data/Carbon_solar_cell``
        folder, indexes rows from 1, and calls :meth:`make_graph_IV_1` with ``plot_type``
        to also save the figure.
        """
        try:
            #folder to save

            folder = self._data_path('Rohit_data', 'Carbon_solar_cell')
            os.makedirs(folder, exist_ok=True)
            
            #current date time
            current_datetime = datetime.now().strftime('%d-%m-%Y_T%H%M%S')
            # Merge measurement + parameter data
            df = pd.concat([output_table, df_parameters], axis=1)
            # Reset index to start from 1 and add index column name
            df.index = df.index + 1
            df.index.name = "Index"
            #add zero padding to the cell number
            sampleid = f"{int(cell_number):02d}"
            
            #Determine run number by checking the exixting file name
            #prefix = f"{keyword}_{int(cell_number):02d}_run"
            #existing_files = [f for f in os.listdir(folder) if f.startswith(prefix)]
            #run_number = len(existing_files) + 1
            
            #construct the file name 
            #file_name = f"{keyword}_{sampleid}_run{run_number:02d}_{current_datetime}"
            file_name = f"{keyword}_{sampleid}_{current_datetime}"
            base_path = os.path.join(folder, file_name)
            df.to_csv(base_path + ".csv", index=True)
            print(f"File saved as: {base_path}.csv")
            
            self.make_graph_IV_1(output_table,cell_number,plot_type,save_path=base_path)
            return self._record_output(base_path + ".csv", keyword, output_table)
        except Exception as e:
            print(f" could  not save file: {e}")
            return None

    def make_graph(self,output_table,cell_number):
        """Plot current and voltage versus time on a twin-axis figure (non-blocking)."""
        try:
            print('start making graph')
            fig, ax1 = plt.subplots(figsize=(10,2)) # Plot current and voltage vs time.
            ax2 = ax1.twinx() # Plot double-y axis.
            ax1.plot(output_table["Time (s)"],output_table["Current (A)"], color='b', alpha=0.5) # Plot current vs time.
            ax2.plot(output_table["Time (s)"], output_table["Voltage (V)"], color='r', alpha=0.5) # Plot voltage vs time.
            #ax2.set_ylim(top = 4*voltage_level) 
            ax1.set_xlabel('Time (s)')
            ax1.set_ylabel('Current (A)', color = 'b')
            ax2.set_ylabel('Voltage (V)', color = 'r')
            plt.title(f'Cell {cell_number}')
            plt.show(block = False)
            plt.close('all')
            print('finish making graph')
        except:
            print('No graph to plot')
            pass   
    
    def make_graph_IV(self,output_table,cell_number):
        """Plot ``|current|`` vs voltage for each SET/RESET cycle.

        Cycles are colour-graded and drawn on a log current scale for the given cell.
        """
        try:
            data = output_table
            sampleid = cell_number
            cycle_max = data['Cycle'].max()
            cycle_min = data['Cycle'].min()

            # Define a custom colormap from dark yellow to dark green
            colors = [(1, 0.5, 0), (0.5, 0, 0.5)]#[(0.6, 0.6, 0), (0, 0.3, 0)]  # Dark yellow to dark green in RGB
            cmap_name = 'orange_to_purple'
            cmap = mcolors.LinearSegmentedColormap.from_list(cmap_name, colors)
            
            fig, ax = plt.subplots(figsize=(8,5))
            
            for cycle in range(cycle_min,cycle_max+1):
                # Plot the data
                color_index = cycle/cycle_max
                data_cyc = data[data['Cycle']==cycle]
                ax.plot(data_cyc['set_Voltage (V)'], abs(data_cyc['Current (A)']), 
                        label=cycle, 
                        color = cmap(color_index),
                        alpha =0.5)

            # Label the axes
            ax.set_xlabel('Voltage (V)')
            ax.set_ylabel('Current (A)')
            ax.set_title('Sample '+str(sampleid))
            # Add a legend outside of the plot to the right, with a smaller font size
            ax.legend(title='Cycle',loc='upper left', bbox_to_anchor=(1, 1), fontsize=6.8,ncol=3)
            
            plt.tight_layout()
            plt.yscale("log")
            plt.show()
        except:
            print('No graph to plot')
            pass

    def make_graph_IV_1(self,output_table,cell_number,plot_type,save_path=None):
        """Flexible plot helper.

        When ``plot_type == 'IV'`` draws a per-cycle current-vs-voltage plot; otherwise
        plots current/voltage versus time. Saved to ``save_path`` when provided.
        """
        try:
            sampleid = cell_number
            
            if plot_type == "IV":
                # extract cycle range
                cycle_max = output_table['Cycle'].max()
                cycle_min = output_table['Cycle'].min()
                # Define a custom colormap
                colors = [(1, 0.5, 0), (0.5, 0, 0.5)]#[(0.6, 0.6, 0), (0, 0.3, 0)]  # Dark yellow to dark green in RGB
                cmap_name = 'orange_to_purple'
                cmap = mcolors.LinearSegmentedColormap.from_list(cmap_name, colors)
                
                fig, ax = plt.subplots(figsize=(8,5))
                
                for cycle in range(cycle_min,cycle_max+1):
                    # Plot the data
                    color_index = cycle/cycle_max
                    data_cyc = output_table[output_table['Cycle'] == cycle]
                    ax.plot(data_cyc['set_Voltage (V)'], abs(data_cyc['Current (A)']), 
                            label=cycle, 
                            color = cmap(color_index),
                            alpha =0.5)
    
                # Label the axes
                ax.set_xlabel('Voltage (V)')
                ax.set_ylabel('Current (A)')
                plt.yscale("log")
                ax.set_title(f"Sample {sampleid}")
                # Add a legend outside of the plot to the right, with a smaller font size
                ax.legend(title='Cycle',loc='upper left', bbox_to_anchor=(1, 1), fontsize=8,ncol=3)
                plt.tight_layout()
                 
            elif plot_type == "x_time":
                fig, ax1 = plt.subplots(figsize=(10, 3))
    
                # Plot current
                ax1.plot(output_table["Time (s)"], output_table["Current (A)"], color="blue")
                ax1.set_xlabel("Time (s)")
                ax1.set_ylabel("Current (A)", color="blue")
                ax1.tick_params(axis="y", labelcolor="blue")
    
                # Overlay voltage on second axis
                ax2 = ax1.twinx()
                ax2.plot(output_table["Time (s)"], output_table["Voltage (V)"], color="red")
                ax2.set_ylabel("Voltage (V)", color="red")
                ax2.tick_params(axis="y", labelcolor="red")
                
                plt.title(f"Sample {sampleid}")
                #plt.tight_layout()
                
            # save the figure if save_path is given
            if save_path:
                fig.savefig(save_path + ".png", dpi=300, bbox_inches = "tight", pad_inches = 0.2)
                print(f"Plot saved as: {save_path}.png")
                plt.close(fig) 
                
            plt.show()
        except Exception as e:
            print(f"No graph to plot: {e}")
            pass

    @_measurement_result
    def Keysight_analog_pulse(self, cell_number: int) -> Dict[str, Any]:
        """Apply a train of identical read/write voltage pulses and record the current.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_analog_pulse.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure analog sweep for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_analog_pulse.csv')

        # Dictionary to store parameters
        parameters = {}

        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        # make a pulse train
        
        pulse_train, pulse_train_string, trigger_count, measure_delay = self.make_voltage_pulses(pulse_voltage,read_voltage,trigger_period,measure_delay_position,pulse_duration,read_duration,no_of_pulses)

        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        #Add column for set_voltage
        output_table['set_voltage (V)']= pd.Series(pulse_train)
        
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        
        
        #self.savefile(output_table, df_parameters, cell_number,'AnalogPulse')
        self.savefile_1(output_table,df_parameters,cell_number,'VoltagePulse',"x_time")

        #plot IV
        self.make_graph_IV_1(output_table,cell_number,plot_type = "x_time")    

    @_measurement_result
    def Keysight_Paired_Pulse_Facilitation(self, cell_number: int) -> Dict[str, Any]:
        """Paired-pulse facilitation (PPF): apply pulse pairs separated by each
        inter-pulse interval in ``delta_t`` and record the response.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Paired_Pulse_Facilitation.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure PPF for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Paired_Pulse_Facilitation.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)
        for t in delta_t:
            #print(t)
            # Define each segment based on the required length
            front_rest = np.full(int((rest_period/2)/trigger_period), 0, dtype=float)
            first_pulse = np.full(int(pulse_duration/trigger_period), pulse_voltage, dtype=float)
            delta_time1 = np.full(int(t/trigger_period), read_voltage, dtype=float)
            second_pulse = np.full(int(pulse_duration/trigger_period), pulse_voltage, dtype=float)
            delta_time2 = np.full(int(t/trigger_period), read_voltage, dtype=float)
            rear_rest = np.full(int((rest_period/2)/trigger_period), 0, dtype=float)
            
            # Concatenate all parts to form the pulse set for this iteration
            pulse_set = np.concatenate([front_rest, first_pulse, delta_time1, second_pulse, delta_time2, rear_rest])
            #print(pulse_set)
            #print('/n')
            # Append to the full pulse set
            full_pulse_set = np.concatenate([full_pulse_set, pulse_set])
            
        pulse_train = full_pulse_set
        print('Pulse_train:')
        print(pulse_train)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(output_table, df_parameters, cell_number,'PPF')

        #make a graph
        self.make_graph(output_table,cell_number)

    @_measurement_result
    def Keysight_Spike_Duration_DP(self, cell_number: int) -> Dict[str, Any]:
        """Spike-duration-dependent plasticity: sweep the write-pulse duration and
        record the resulting conductance change.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Spike_Duration_DP.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure SDDP for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Spike_Duration_DP.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)
        for pulse_duration in pulse_durations:
            #print(t)
            # Define each segment based on the required length
            front_rest = np.full(int((rest_period/2)/trigger_period), read_voltage, dtype=float)
            single_pulse = np.full(int(pulse_duration/trigger_period), pulse_voltage, dtype=float)
            rear_rest = np.full(int((rest_period/2)/trigger_period), read_voltage, dtype=float)
            
            # Concatenate all parts to form the pulse set for this iteration
            pulse_set = np.concatenate([front_rest, single_pulse, rear_rest])
            #print(pulse_set)
            #print('/n')
            # Append to the full pulse set
            full_pulse_set = np.concatenate([full_pulse_set, pulse_set])
            
        pulse_train = full_pulse_set
        print('Pulse_train:')
        print(pulse_train)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(output_table, df_parameters, cell_number,'SDDP')

        #make a graph
        self.make_graph(output_table,cell_number)

    @_measurement_result
    def Keysight_Spike_Voltage_DP(self, cell_number: int) -> Dict[str, Any]:
        """Spike-voltage-dependent plasticity: sweep the write-pulse voltage and
        record the resulting conductance change.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Spike_Voltage_DP.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure SVDP for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Spike_Voltage_DP.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)
        for pulse_voltage in pulse_voltages:
            #print(t)
            # Define each segment based on the required length
            front_rest = np.full(int((rest_period/2)/trigger_period), read_voltage, dtype=float)
            single_pulse = np.full(int(pulse_duration/trigger_period), pulse_voltage, dtype=float)
            rear_rest = np.full(int((rest_period/2)/trigger_period), read_voltage, dtype=float)
            
            # Concatenate all parts to form the pulse set for this iteration
            pulse_set = np.concatenate([front_rest, single_pulse, rear_rest])
            #print(pulse_set)
            #print('/n')
            # Append to the full pulse set
            full_pulse_set = np.concatenate([full_pulse_set, pulse_set])
            
        pulse_train = full_pulse_set
        print('Pulse_train:')
        print(pulse_train)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(output_table, df_parameters, cell_number,'SVDP')

        #make a graph
        self.make_graph(output_table,cell_number)
    
    @_measurement_result
    def Keysight_Digital_Endurance(self, cell_number: int) -> Dict[str, Any]:
        """Digital endurance / retention: repeatedly SET/RESET the device and track
        the read current over cycles. (Also exposed as ``Keysight_Digital_Retention``.)

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Digital_Retention.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure digital switching retention for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Digital_Retention.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)

        write_pulse = np.full(int(write_duration/trigger_period), write_voltage,dtype=float)
        read_pulse_1 = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
        erase_pulse = np.full(int(erase_duration/trigger_period), erase_voltage,dtype=float)
        read_pulse_2 = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)

        full_pulse_set = np.concatenate([write_pulse, read_pulse_1,erase_pulse,read_pulse_2])


        pulse_train = full_pulse_set
        print('Pulse_train:')
        print(pulse_train)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(output_table, df_parameters, cell_number,'RETENTION')

        #make a graph
        self.make_graph(output_table,cell_number)

    @_measurement_result
    def Keysight_Digital_Sweep(self, cell_number: int) -> Dict[str, Any]:
        """Digital I-V sweep: SET then RESET voltage sweeps over ``no_cycles``,
        recording the current per cycle.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Digital_Sweep.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds 'set_Voltage (V)', 'Current (A)', 'Cycle'.
                - ``result``: None.
        """
        print('Measure Digital J-V sweep for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Digital_Sweep.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a wave 
        full_set_reset_sweep = np.array([], dtype=float)
        

        #set process
        set_positive_sweep = np.arange(0, float(set_v_max) + float(volt_step), float(volt_step))
        set_negative_sweep = np.arange(float(set_v_max) - float(volt_step), 0 - float(volt_step), -1 * float(volt_step))
        #set_positive_sweep = np.arange(0,set_v_max+volt_step,volt_step)
        #set_negative_sweep = np.arange(set_v_max-volt_step,0-volt_step,-1*volt_step)


        #round it to 2 decimals
        set_positive_sweep = [np.round(x,decimals=2) for x in set_positive_sweep]
        set_negative_sweep = [np.round(x,decimals=2) for x in set_negative_sweep]
        #make one set sweep
        set_sweep = set_positive_sweep+set_negative_sweep

        #reset process
        reset_negative_sweep = np.arange(0, float(reset_v_min) - float(volt_step), -1 * float(volt_step))
        reset_positive_sweep = np.arange(float(reset_v_min), 0 + float(volt_step), float(volt_step))
        #reset_negative_sweep = np.arange(0,reset_v_min-volt_step,-1*volt_step)
        #reset_positive_sweep = np.arange(reset_v_min, 0+volt_step,volt_step)
        #round it to 2 decimals
        reset_negative_sweep = [np.round(x,decimals=2) for x in reset_negative_sweep]
        reset_positive_sweep = [np.round(x,decimals=2) for x in reset_positive_sweep]
        #make one set sweep
        reset_sweep = reset_negative_sweep+reset_positive_sweep

        #set reset full sweep
        full_set_reset_sweep = np.concatenate([set_sweep,reset_sweep])

        #conduct set and reset sweep in loop
        df_sweep_total = pd.DataFrame()

        for i in range(int(no_cycles)):
            print('Measuring sweep cycle: '+str(i+1))
            pulse_train = full_set_reset_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self.string_to_dataframe(output_string)
            #add Cycle column to the output_table
            output_table['Cycle']=i+1
            #add column set_Voltage (V)
            output_table['set_Voltage (V)']= pd.Series(full_set_reset_sweep)

            df_sweep_total = pd.concat([df_sweep_total,output_table], ignore_index = True)
        

        #adding parameters to the next rows, save it
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(df_sweep_total,df_parameters,cell_number,'DigiSweep')

        #plot IV
        self.make_graph_IV(df_sweep_total,cell_number)

    @_measurement_result
    def Keysight_Analog_Sweep(self, cell_number: int) -> Dict[str, Any]:
        """Analog I-V sweep: continuous SET/RESET voltage sweeps recording the analog
        current response.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Analog_Sweep.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds 'set_Voltage (V)', 'Current (A)', 'Cycle'.
                - ``result``: None.
        """
        print('Measure Analog I-V sweep for cell'+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Analog_Sweep.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a voltage wave 
        full_set_reset_sweep = np.array([], dtype=float)
        
        #set process
        set_positive_sweep = np.arange(0, float(set_v_max) + float(volt_step), float(volt_step))
        set_negative_sweep = np.arange(float(set_v_max) - float(volt_step), 0 - float(volt_step), -1 * float(volt_step))
        #set_positive_sweep = np.arange(0,set_v_max+volt_step,volt_step)
        #set_negative_sweep = np.arange(set_v_max-volt_step,0-volt_step,-1*volt_step)


        #round it to 2 decimals
        set_positive_sweep = [np.round(x,decimals=2) for x in set_positive_sweep]
        set_negative_sweep = [np.round(x,decimals=2) for x in set_negative_sweep]
        #make one set sweep
        set_sweep = set_positive_sweep+set_negative_sweep

        #reset process
        reset_negative_sweep = np.arange(0, float(reset_v_min) - float(volt_step), -1 * float(volt_step))
        reset_positive_sweep = np.arange(float(reset_v_min) + float(volt_step), 0 + float(volt_step), float(volt_step))
        #reset_negative_sweep = np.arange(0,reset_v_min-volt_step,-1*volt_step)
        #reset_positive_sweep = np.arange(reset_v_min, 0+volt_step,volt_step)
        #round it to 2 decimals
        reset_negative_sweep = [np.round(x,decimals=2) for x in reset_negative_sweep]
        reset_positive_sweep = [np.round(x,decimals=2) for x in reset_positive_sweep]
        #make one set sweep
        reset_sweep = reset_negative_sweep+reset_positive_sweep

        #set reset full sweep
        full_set_reset_sweep = np.concatenate([set_sweep,reset_sweep])

        #conduct first set sweeps and then reset sweeps in loop
        df_sweep_total = pd.DataFrame()

        #cycle for set
        df_set_sweep =pd.DataFrame()
        for i in range(int(no_cycles)):
            print('Measuring set sweep cycle: '+str(i+1))
            pulse_train = set_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self.string_to_dataframe(output_string)
            #add column set_Voltage (V)
            output_table['set_Voltage (V)']= pd.Series(set_sweep)
            #add Cycle column to the output_table
            output_table['Cycle']=i+1
            output_table['SetReset']='Set'

            df_set_sweep = pd.concat([df_set_sweep,output_table], ignore_index = True)
        
        #cycle for reset
        df_reset_sweep =pd.DataFrame()
        for i in range(int(no_cycles)):
            print('Measuring reset sweep cycle: '+str(i+1))
            pulse_train = reset_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self.string_to_dataframe(output_string)
            #add column set_Voltage (V)
            output_table['set_Voltage (V)']= pd.Series(reset_sweep)
            #add Cycle column to the output_table
            output_table['Cycle']=i+1
            output_table['SetReset']='Reset'

            df_reset_sweep = pd.concat([df_reset_sweep,output_table], ignore_index = True)
        
        df_sweep_total = pd.concat([df_set_sweep,df_reset_sweep], ignore_index=True)

        #adding parameters to the next rows, save it
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(df_sweep_total,df_parameters,cell_number,'AnaSweep')

        #plot IV
        self.make_graph_IV(df_sweep_total,cell_number)

    @_measurement_result
    def Keysight_set_reset_sweep(self, cell_number: int) -> Dict[str, Any]:
        """Configurable SET/RESET sweep honouring a ``mode`` parameter
        (``'loop'`` / ``'separate'`` / ``'set_only'`` / ``'reset_only'``).

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_set_reset_sweep.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds 'set_Voltage (V)', 'Current (A)', 'Cycle'.
                - ``result``: the combined sweep DataFrame ``df_sweep_total``.
        """
        print('Perform SET-RESET I-V sweep for cell'+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_set_reset_sweep.csv')
    
        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as infile:
            reader = csv.DictReader(infile)
            for row in reader:
                param_name = row['Parameter'].strip()
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value
    
        
        def make_sweeps():
            
            #set process
            positive_forward_sweep = np.arange(0, float(set_v_max) + float(volt_step), float(volt_step))
            positive_reverse_sweep = np.arange(float(set_v_max) - float(volt_step), 0 - float(volt_step), -1 * float(volt_step))
            #round it to 2 decimals
            positive_forward_sweep = [np.round(x,decimals=2) for x in positive_forward_sweep]
            positive_reverse_sweep = [np.round(x,decimals=2) for x in positive_reverse_sweep]
            #make one set sweep
            set_sweep = np.concatenate([positive_forward_sweep, positive_reverse_sweep])
        
            #reset process
            negative_forward_sweep = np.arange(0, float(reset_v_min) - float(volt_step), -1 * float(volt_step))
            negative_reverse_sweep = np.arange(float(reset_v_min) + float(volt_step), 0 + float(volt_step), float(volt_step))
            #round it to 2 decimals
            negative_forward_sweep = [np.round(x,decimals=2) for x in negative_forward_sweep]
            negative_reverse_sweep = [np.round(x,decimals=2) for x in negative_reverse_sweep]
            #make one set sweep
            reset_sweep = np.concatenate([negative_forward_sweep, negative_reverse_sweep])
        
            return set_sweep.tolist(), reset_sweep.tolist()
        
        set_sweep, reset_sweep = make_sweeps()
        
        #conduct first set sweeps and then reset sweeps in loop
        df_sweep_total = pd.DataFrame()
            
        def run_sweep (sweep, label, cycle_idx):
            """Helper function to execute a single sweep and process the data."""
            pulse_train = sweep
            pulse_train_string = ",".join(map(str, pulse_train))
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period * measure_delay_position)
    
            try:
                # Send pulses to instrument
                output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
  
                # Convert response into DataFrame
                output_table = self.string_to_dataframe(output_string)
            
                # Add metadata
                output_table["set_Voltage (V)"] = pd.Series(pulse_train, dtype=float)
                output_table["Cycle"] = cycle_idx + 1
                output_table["SetReset"] = label
        
                return output_table
        
            except Exception as e:
                print(f"Error during {label} sweep: {e}")
                return pd.DataFrame()
    
        # --- Execution modes ---
        
        if mode == "loop":
            for i in range(int(no_cycles)):
                print(f"Measuring SET sweep cycle: {i+1}")
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(set_sweep, "Set", i)], ignore_index=True)
    
                print(f"Measuring RESET sweep cycle: {i+1}")
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(reset_sweep, "Reset", i)], ignore_index=True)

        elif mode == "separate":
            # all SET cycles first
            for i in range(int(no_cycles)):
                print(f"Measuring SET sweep cycle: {i+1}")
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(set_sweep, "Set", i)], ignore_index=True)

            # then all RESET cycles
            for i in range(int(no_cycles)):
                print(f"Measuring RESET sweep cycle: {i+1}")
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(reset_sweep, "Reset", i)], ignore_index=True)

        elif mode == "set_only":
            for i in range(int(no_cycles)):
                print(f"Measuring SET sweep cycle: {i+1}")
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(set_sweep, "Set", i)], ignore_index=True)

        elif mode == "reset_only":
            for i in range(int(no_cycles)):
                print(f"Measuring RESET sweep cycle: {i+1}")
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(reset_sweep, "Reset", i)], ignore_index=True)

        #adding parameters to the next rows, save it
        df_parameters = pd.read_csv(file_parameters)
        self.savefile_1(df_sweep_total,df_parameters,cell_number,'set-reset_sweep',"IV")
    
        #plot IV
        self.make_graph_IV_1(df_sweep_total,cell_number,plot_type = "IV")    
    
        return df_sweep_total

    
    @_measurement_result
    def Keysight_Substrate_R(self, cell_number: int) -> Dict[str, Any]:
        """Measure substrate resistance via a small voltage sweep and a linear fit.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Substrate_R.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure Resistivity of scaffolds '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Substrate_R.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a wave 
        full_forward_reverse_sweep = np.array([], dtype=float)
        
        #forward bias
        forward_sweep = np.arange(v_min, v_max, volt_step)
        #reverse bias
        reverse_sweep = np.arange(v_max, v_min, -1 * volt_step)

        #round it to 2 decimals
        forward_sweep = [np.round(x,decimals=2) for x in forward_sweep]
        reverse_sweep = [np.round(x,decimals=2) for x in reverse_sweep]
        

        #full forward and reverse
        full_forward_reverse_sweep = np.concatenate([forward_sweep,reverse_sweep])

        #conduct forward and revetse sweep in loop
        df_sweep_total = pd.DataFrame()
    
        for i in range(int(no_cycles)):
            print('Measuring cycle: '+str(i+1))
            pulse_train = full_forward_reverse_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self.string_to_dataframe(output_string)
            
            #add Cycle column to the output_table
            output_table['Cycle']=i+1

            df_sweep_total = pd.concat([df_sweep_total,output_table], ignore_index = True)

        #adding parameters to the next rows, save it
        df_parameters = pd.read_csv(file_parameters)

        # Perform linear regression
        voltage = df_sweep_total['Voltage (V)']
        current = df_sweep_total['Current (A)']
        slope, intercept, r_value, p_value, std_err = linregress(voltage, current)

        # Save the slope as R
        R = 1/slope
        data = {'Resistance (Ohm)':[R]}
        df_R = pd.DataFrame(data)

        df_parameters_calcparam = pd.concat([df_parameters, df_R],axis = 1)
        self.savefile(df_sweep_total,df_parameters_calcparam,cell_number,'SubstR')

 
        # Plot the data points
        plt.scatter(voltage, current, label='Data points', color='blue')

        # Plot the fitting line
        plt.plot(voltage, intercept + slope * voltage, 'r', label=f'Fitting line (R = {R:.2e})')

        # Add labels and legend
        plt.title('Sample '+str(cell_number))
        plt.xlabel('Voltage (V)')
        plt.ylabel('Current (A)')
        plt.legend()

        # Show the plot
        plt.show()

    @_measurement_result
    def Keysight_JV_PV(self, cell_number: int) -> Dict[str, Any]:
        """Photovoltaic J-V measurement: forward and reverse voltage sweeps per cycle,
        converted to current density, with PV parameters extracted via
        :mod:`probot_drivers.analysis.pv_param`.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_JV_PV.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds 'Voltage (V)', 'Current (mA)', 'Current Density (mA/cm2)', 'Cycle' plus the PV
                  parameters (PCE, FF, Voc, Jsc, Rshunt, Rseries) per fwd/rev cycle.
                - ``result``: None.
        """
        print('Measure J-V of PV cell: '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_JV_PV.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #scan_are set. therefore we need to calculate the trigger period
        trigger_period = float(volt_step*1000/scan_rate)
        measure_delay = str(trigger_period*measure_delay_position)
        
        #forward bias
        forward_sweep = np.arange(v_min, v_max+volt_step, volt_step)
        #reverse bias
        reverse_sweep = np.arange(v_max, v_min-volt_step, -1 * volt_step)

        #round it to 2 decimals
        forward_sweep = [np.round(x,decimals=2) for x in forward_sweep]
        reverse_sweep = [np.round(x,decimals=2) for x in reverse_sweep]
        
    
        for i in range(int(no_cycles)):
            
            #---FORWARD----
            print('Measuring cycle '+str(i+1)+' forward')
            forward_pulse_train_string = ','.join(map(str,forward_sweep))
            trigger_count = str(int(len(forward_sweep)))
            #send pulse and get the output_string
            output_string_forward = self.send_pulse_train_to_keysight(compliance, forward_pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table_forward = self.string_to_dataframe(output_string_forward)#this contain Voltage (V), Current (A), Time (s). need to change to below
            #add Cycle column to the output_table
            output_table_forward['Cycle']=i+1

            #change the data parameters
            df_JV_cooked_fwd = pd.DataFrame({
                'Time (s)':output_table_forward['Time (s)'],
                'Voltage (V)': output_table_forward['Voltage (V)'],
                'Current (mA)': (-1)*output_table_forward['Current (A)']*1000, #change the sign upsidedown, change it to mA
                'Current Density (mA/cm2)': (-1)*output_table_forward['Current (A)']*1000/cell_area,
                #'Power (mW)': output_table_forward['Voltage (V)']*(-1)*output_table_forward['Current (A)']/1000,
                'Cycle': output_table_forward['Cycle']
            })

            #settings 
            df_settings = pd.read_csv(file_parameters) 
            #concat setting to the data
            df_data_setting_fwd = pd.concat([df_JV_cooked_fwd,df_settings],axis=1)
            #----calculate the PV parameters--
            df_PV_params_fwd = PV_calc.calculate_parameters(df=df_data_setting_fwd)
            

            #----NEED TO ADD CALCULATION OF J-V parameters--
            file_name_tail = 'JV_PV_fwd_cycle'+str(i+1)
            self.savefile(df_data_setting_fwd,df_PV_params_fwd,cell_number,file_name_tail)


            #---REVERSE
            print('Measuring cycle '+str(i+1)+' reverse')
            reverse_pulse_train_string = ','.join(map(str,reverse_sweep))
            trigger_count = str(int(len(reverse_sweep)))
            #send the pulse and get the output string
            output_string_reverse = self.send_pulse_train_to_keysight(compliance,reverse_pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change strings to dataframe
            output_table_reverse = self.string_to_dataframe(output_string_reverse)
            #add cycle column
            output_table_reverse['Cycle']=i+1
            #change the data format and parameters
            df_JV_cooked_rev = pd.DataFrame({
                'Time (s)':output_table_reverse['Time (s)'],
                'Voltage (V)': output_table_reverse['Voltage (V)'],
                'Current (mA)': (-1)*output_table_reverse['Current (A)']*1000, #change the sign upsidedown
                'Current Density (mA/cm2)': (-1)*output_table_reverse['Current (A)']*1000/cell_area,
                #'Power (mW)': output_table_reverse['Voltage (V)']*(-1)*output_table_forward['Current (A)']/1000,
                'Cycle': output_table_forward['Cycle']
            })
            
            #df setting 
            df_settings = pd.read_csv(file_parameters) 
            #concat setting to the data
            df_data_setting_rev = pd.concat([df_JV_cooked_rev,df_settings],axis=1)
            #----calculate the PV parameters--
            df_PV_params_rev = PV_calc.calculate_parameters(df=df_data_setting_rev)
            

            file_name_tail = 'JV_PV_rev_cycle'+str(i+1)
            self.savefile(df_data_setting_rev,df_PV_params_rev,cell_number,file_name_tail)



            #----PLOT forward and reserve per cycle
            plt.plot(df_JV_cooked_fwd['Voltage (V)'], df_JV_cooked_fwd['Current Density (mA/cm2)'], label='fwd', color='blue',  linestyle='-', marker='o', markersize=3)
            plt.plot(df_JV_cooked_rev['Voltage (V)'], df_JV_cooked_rev['Current Density (mA/cm2)'], label='rev', color='red', linestyle='-', marker='o', markersize=3)

            # Add solid lines at y = 0 and x = 0
            plt.axhline(0, color='black', linestyle='-', linewidth=1)
            plt.axvline(0, color='black', linestyle='-', linewidth=1)

            # Add labels and legend
            plt.title('Sample '+str(cell_number)+' Cycle '+str(i+1))
            plt.xlabel('Voltage (V)')
            plt.ylabel('Current Density (mA/cm2)')
            plt.legend(loc='upper right')

            # Display the calculated_params and PV_param_values for df_PV_params_fwd
            params_text_fwd = '\n'.join([f"{row['PV_params']}: {row['PV_param_values']:.2f}" for _, row in df_PV_params_fwd.iterrows()])
            plt.gcf().text(0.92, 0.6, params_text_fwd, fontsize=10, verticalalignment='center', color='blue')

            # Display the calculated_params and PV_param_values for df_PV_params_rev
            params_text_rev = '\n'.join([f"{row['PV_params']}: {row['PV_param_values']:.2f}" for _, row in df_PV_params_rev.iterrows()])
            plt.gcf().text(0.92, 0.3, params_text_rev, fontsize=10, verticalalignment='center', color='red')

            # Show the plot
            plt.show()

            # Show the plot
            plt.show()

    def send_pulse_train_to_keysight_light_pulse(self, compliance, pulse_train_string,trigger_count,trigger_period,measure_delay,
                                                 front_rest_duration, light_intensity, read_duration, light_on_duration, light_off_duration):
        
        """Run a voltage-list sweep while driving the Pico light.

        Same as :meth:`send_pulse_train_to_keysight` but additionally pulses the light
        during acquisition, for light-synchronized (optoelectronic) measurements.
        """
        print('SENDING COMMANDS TO INSTRUMENT')
        try:
            # ********** INITIALIZE SMU **********
            self.smu.timeout = 10000000  # 10000s
            self.smu.write_termination = '\n' # To define end of command.
            self.smu.read_termination = '\n' # To define end of command.
            self.smu.write('*CLS') # Clears the command queue.
            self.smu.write('*RST') # Resets the volatile memory.


                # ********** CONFIGURE MEASUREMENT **********

                # All the commands are for channel 1 (front). Please make sure thatthe connections are made to channel 1.

            self.smu.write(":TRAC:FEED SENS") # The buffer stores measured data.
            self.smu.write(":TRAC:FEED:CONT NEXT") # Make the buffer editable.
            self.smu.write(":TRAC:TST:FORM ABS") # Format of stored timestamps.
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR") # Reading operation stores time, voltage, and current to the buffer.
            self.smu.write(":SOUR:FUNC:MODE VOLT") # Set the source to supply voltage.
            self.smu.write(":SOUR:VOLT:MODE LIST") # Supply voltage as a list.
            self.smu.write(f":SENS:CURR:PROT {str(compliance/1000)}") # Set compliance current.
            self.smu.write(f":LIST:VOLT {pulse_train_string}") # Send the voltage list that was previously generated.
            self.smu.write(f":TRIG:COUN {trigger_count}") # Set the number of triggers.
            self.smu.write(":TRIG:SOUR TIMER") # Set the source of commands trigger as time. The trigger duration depends on time. 
            self.smu.write(f":TRIG:TIM {str(trigger_period)}") # Set the duration of trigger.
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}") # Set the measure delay. Preferably, the measurement is performed at the centre of the trigger.

                # ********** RUN MEASUREMENT ********** 
            print('INNITIATE MEASURREMENT')
            self.smu.write(":OUTP ON") # Turns SMU output on.
            self.smu.write(":INIT") # Initiate measurement.
            ##create light pulses
            #turn off at front rest
            self.pico_instrument.light_off()
            time.sleep(front_rest_duration)
            #conduct on/off cycle
            # ********** light pulse **********
            print('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.light_pulse(light_intensity=light_intensity, 
                                        read_duration=read_duration, 
                                        light_on_duration=light_on_duration, 
                                        light_off_duration=light_off_duration)

            # ********** READ BUFFER **********
            print('READING BUFFER')
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            print(self.smu.query("SYST:ERR?")) # Check error buffer. If there was any error in the execution of SCPI commands. 
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        return output_string
    
    @_measurement_result
    def Keysight_Light_Pulse(self, cell_number: int) -> Dict[str, Any]:

        """Apply a voltage pulse train synchronized with Pico light pulses and record
        the optoelectronic response.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Light_Pulse.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure analog sweep for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Light_Pulse.csv')

        # Dictionary to store parameters
        parameters = {}

        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)

        front_rest = np.full(int(front_rest_duration/trigger_period), 0, dtype=float)
        read_pulse = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
            
        # Append to the full pulse set
        full_pulse_set = np.concatenate([front_rest, read_pulse])
            
        pulse_train = full_pulse_set
        print('Pulse_train:')
        print(pulse_train)
    
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight_light_pulse(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay, front_rest_duration, light_intensity, read_duration, light_on_duration, light_off_duration)
        """
                #create light pulses
                #turn off at front rest
                self.pico_instrument.light_off()
                time.sleep(front_rest_duration)
                #conduct on/off cycle
                self.pico_instrument.light_pulse(light_intensity=light_intensity, 
                                                read_duration=read_duration, 
                                                light_on_duration=light_on_duration, 
                                                light_off_duration=light_off_duration)
        """      
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(output_table, df_parameters, cell_number,'LightPulse')

        #make a graph
        self.make_graph(output_table,cell_number)

    @_measurement_result
    def Keysight_Voltage_Steady(self, cell_number: int) -> Dict[str, Any]:
        """Hold a list of steady voltages and record the current over time
        (constant-voltage stress / retention).

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_voltage_steady.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Measure voltage steady state '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_voltage_steady.csv')

        # Dictionary to store parameters
        parameters = {}
        with open(file_parameters, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                param_name = row['Parameter']
                param_value = row['Value'].strip()
                # Use ast.literal_eval to convert string to Python literal if possible
                # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                try:
                    param_value = ast.literal_eval(param_value)
                except (SyntaxError, ValueError):
                    # If it's not a literal (e.g., a normal string), keep it as a string
                    pass
                parameters[param_name] = param_value
                #print(param_value)
        # Create variables in the global namespace
        for p_name, p_value in parameters.items():
            globals()[p_name] = p_value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)
        for pulse_voltage in pulse_voltages: #pulse_voltages is a list
            
            # Define each voltage segment based on the required duration
            single_pulse = np.full(int(voltage_duration/trigger_period), pulse_voltage, dtype=float)

            # Append to the full pulse set
            full_pulse_set = np.concatenate([full_pulse_set, single_pulse])
            
        pulse_train = full_pulse_set
        #print('Pulse_train:')
        #print(pulse_train)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        #Add column for set_voltage
        output_table['set_voltage (V)']= pd.Series(pulse_train)
        
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile_1(output_table, df_parameters, cell_number,'VoltageSteady', "x_time")
        self.make_graph_IV_1(output_table, cell_number, plot_type="x_time") 
        
    @_measurement_result
    def Keysight_Voltage_list(self, cell_number: int) -> Dict[str, Any]:
            """Apply an arbitrary voltage list loaded from CSV and record the response.

            Args:
                cell_number: 1-based cell index, used in the saved file name(s).
                    Measurement settings are read from ``parameter_Keysight_voltage_list.csv``
                    (columns ``Parameter, Value``).

            Returns:
                Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                    - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                    - ``result``: None.
            """
            print('Measure Current against voltage list input '+str(cell_number))
            file_parameters = self._param_file('parameter_Keysight_voltage_list.csv')

            # Dictionary to store parameters
            parameters = {}
            with open(file_parameters, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    param_name = row['Parameter']
                    param_value = row['Value'].strip()
                    # Use ast.literal_eval to convert string to Python literal if possible
                    # This will convert strings like "[0.02,0.04,0.06]" into a Python list
                    try:
                        param_value = ast.literal_eval(param_value)
                    except (SyntaxError, ValueError):
                        # If it's not a literal (e.g., a normal string), keep it as a string
                        pass
                    parameters[param_name] = param_value
                    #print(param_value)
            # Create variables in the global namespace
            for p_name, p_value in parameters.items():
                globals()[p_name] = p_value
            
            voltage_df = pd.read_csv(csv_path)
            pulse_train = voltage_df["voltage_list"].to_numpy(dtype=float)

            
            print(pulse_train)
            print(pulse_train.shape)
            print(type(pulse_train[0]))
            
            pulse_train_string = ','.join(map(str,pulse_train))
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)


            #send pulse and get the output_sting
            output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self.string_to_dataframe(output_string)
            #Add column for set_voltage
            output_table['set_voltage (V)']= pd.Series(pulse_train)
            
            #save file, adding parameters to the next rows
            df_parameters = pd.read_csv(file_parameters)
            self.savefile_1(output_table, df_parameters, cell_number,'Voltage_list', "x_time")
            self.make_graph_IV_1(output_table, cell_number, plot_type="x_time") 
        
    def Pot_Dep_Calculation(self,df, cell_number, keyword):
        """Post-process a potentiation/depression run.

        Extracts conductance, separates the potentiation/depression cycles, fits the
        traces and appends a results summary (delegates to
        :mod:`probot_drivers.analysis.ht_potdep`).

        Args:
            df: the raw measurement DataFrame.
            cell_number: 1-based cell index.
            keyword: file-name keyword for the saved summary.
        """
        try:
            #folder to save
            folder = self._data_path('1_reservoir')
            
            #current date time
            current_datetime = datetime.now().strftime('%Y%m%d_%H%M%S')

            #add conductance column
            df['Conductance (S)'] = df['Current (A)']/df['Voltage (V)']

            #when Voltage is less then +- 0.001, make the conductance 0
            df['Conductance (S)'] = df['Conductance (S)'].where(abs(df['Voltage (V)']) > 0.001, 0)

            #make new column Cycle. 
            total_length = len(df)
            rest_length = int(df.loc[0,'Value']/df.loc[8,'Value'])
            pulses_length = total_length - rest_length
            pulse_length = pulses_length/df.loc[7,'Value']
            df['Cycle']=((df.index-rest_length)//pulse_length)+1

            #print('debug 2')
            df.to_csv('debug.csv')
            #the main fitting is done in the HTPD
            df_output_parameters_fitting = self._htpd().main(df,cell_number)

            #print('debug 7')

            #save the file
            sampleid = cell_number
            if (sampleid < 10):
                sampleid = "0"+str(sampleid)

            file_name = f'{keyword}_{sampleid}_{current_datetime}.csv'

            file_path = os.path.join(folder, file_name)
            df_output_parameters_fitting.to_csv(file_path, index=False)

            #print('debug 8')
            print("File saved as: " + file_name)

            return df_output_parameters_fitting
        except:
            print('no file to save at pot_dep_calculation')
            pass        

    @_measurement_result
    def Keysight_Potent_Depress(self, cell_number: int) -> Dict[str, Any]:
        """Potentiation/depression: apply repeated write then erase pulse trains over
        cycles and record the conductance change (synaptic weight update).

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Potent_Depress.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Conduct potentiation and depression cycle for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Potent_Depress.csv')

        # Dictionary to store parameters
        parameters = {}
        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)

        ##---RESET at the beginning of pulses by giving 0V for a certain duration (reset_period
        reset_voltage = 0
        reset_pulse = np.full(int(reset_period/trigger_period), reset_voltage, dtype=float)
        full_pulse_set = np.concatenate([full_pulse_set, reset_pulse])

        ##---POTENTIATION AND DEPRESSION pulses for cycle_write_erase cycles ----
        for cycle in range(cycle_write_erase):  #pulse_voltages is a list

            ##----POTENTIATION----
            full_write_read_pulses = np.array([], dtype=float) #initialize the read_write_pulse
            for pulse in range(pulse_no):
                # make write pulses
                write_pulse = np.full(int(pulse_duration/trigger_period), write_voltage, dtype=float)
                read_pulse = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
                write_read_pulse = np.concatenate([write_pulse, read_pulse])
                full_write_read_pulses = np.concatenate([full_write_read_pulses, write_read_pulse]) #this is the full write and read pulses for pulse_no

            ##----DEPRESSION----
            full_erase_read_pulses = np.array([], dtype=float) #initialize the read_write_pulse
            for pulse in range(pulse_no):
                # make write pulses
                erase_pulse = np.full(int(pulse_duration/trigger_period), erase_voltage, dtype=float)
                read_pulse = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
                erase_read_pulse = np.concatenate([erase_pulse, read_pulse])
                full_erase_read_pulses = np.concatenate([full_erase_read_pulses, erase_read_pulse])
            
            #concatenate potentiation and depression
            full_pulse_set = np.concatenate([full_pulse_set, full_write_read_pulses, full_erase_read_pulses])


        pulse_train = full_pulse_set
        #print('Pulse_train:')
        #print(pulse_train)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        
        # adding parameters to the next rows, save it
        df_parameters = pd.read_csv(file_parameters)
        #add cell_number to the df_parameters bottom row
        df_parameters = pd.concat([df_parameters, pd.DataFrame({'Parameter':'cell_number','Value':[cell_number]})], ignore_index=True)
        #combine output_table and parameters
        df_output_table_parameters = pd.concat([output_table, df_parameters], axis=1)


        #make a measurement overall graph
        try:
            print('start making graph')
            fig, ax1 = plt.subplots(figsize=(10,2)) # Plot current and voltage vs time.
            ax2 = ax1.twinx() # Plot double-y axis.
            ax1.plot(output_table["Time (s)"],output_table["Current (A)"], color='b', alpha=0.5) # Plot current vs time.
            ax2.plot(output_table["Time (s)"], output_table["Voltage (V)"], color='r', alpha=0.5) # Plot voltage vs time.
            #ax2.set_ylim(top = 4*voltage_level) 
            ax1.set_xlabel('Time (s)')
            ax1.set_ylabel('Current (A)', color = 'b')
            ax2.set_ylabel('Voltage (V)', color = 'r')
            plt.title(f'Overall graph for cell {cell_number}')
            plt.show(block = False)
            plt.close('all')
            print('finish making graph')
        except:
            print('No graph to plot')
            pass 
        
       
        #Fit the potentiation and depression, also save the file
        df_output_parameters_fitting = self.Pot_Dep_Calculation(df_output_table_parameters, cell_number, 'PotDep')

        #return df_output_parameters_fitting

    
    @_measurement_result
    def Keysight_Potent_Depress_2(self, cell_number: int) -> Dict[str, Any]:
        #here the read pulse is only pulse in between 0 V
        #the length of the waiting time = t_pulse_to_read + t_read + t_read_to_pulse

        """Potentiation/depression with explicit pulse-to-read and pulse-to-pulse
        timing control.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Potent_Depress_2.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: the fitting-parameters DataFrame ``df_output_parameters_fitting``.
        """
        print('Conduct potentiation and depression cycle for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Potent_Depress_2.csv')

        # Dictionary to store parameters
        parameters = {}
        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        #make a wave 
        full_pulse_set = np.array([], dtype=float)

        ##---RESET at the beginning of pulses by giving 0V for a certain duration (reset_period
        reset_voltage = 0
        reset_pulse = np.full(int(reset_period/trigger_period), reset_voltage, dtype=float)
        full_pulse_set = np.concatenate([full_pulse_set, reset_pulse])

        ##---POTENTIATION AND DEPRESSION pulses for cycle_write_erase cycles ----
        for cycle in range(cycle_write_erase):  #pulse_voltages is a list

            #wait_voltage = 0.01

            ##----POTENTIATION----
            full_write_read_pulses = np.array([], dtype=float) #initialize the read_write_pulse
            for pulse in range(pulse_no):
                # make write pulses
                write_pulse = np.full(int(pulse_duration/trigger_period), write_voltage, dtype=float)
                wait_front = np.full(int(t_pulse_to_read/trigger_period), wait_voltage, dtype=float)
                read_pulse = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
                wait_rear = np.full(int((t_pulse_to_pulse-t_pulse_to_read-read_duration)/trigger_period), wait_voltage, dtype=float)
                
                write_read_pulse = np.concatenate([write_pulse, wait_front,read_pulse,wait_rear])
                full_write_read_pulses = np.concatenate([full_write_read_pulses, write_read_pulse]) #this is the full write and read pulses for pulse_no

            ##----DEPRESSION----
            full_erase_read_pulses = np.array([], dtype=float) #initialize the read_write_pulse
            for pulse in range(pulse_no):
                # make write pulses
                erase_pulse = np.full(int(pulse_duration/trigger_period), erase_voltage, dtype=float)
                wait_front = np.full(int(t_pulse_to_read/trigger_period), wait_voltage, dtype=float)
                read_pulse = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
                wait_rear = np.full(int((t_pulse_to_pulse-t_pulse_to_read-read_duration)/trigger_period), wait_voltage, dtype=float)
                
                erase_read_pulse = np.concatenate([erase_pulse, wait_front, read_pulse, wait_rear])
                full_erase_read_pulses = np.concatenate([full_erase_read_pulses, erase_read_pulse])
            
            #concatenate potentiation and depression
            full_pulse_set = np.concatenate([full_pulse_set, full_write_read_pulses, full_erase_read_pulses])


        pulse_train = full_pulse_set
        #print('Pulse_train:')
        #print(pulse_train)
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self.send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        print("output_table from SMU acquired")
        
        # adding parameters to the next rows, save it
        df_parameters = pd.read_csv(file_parameters)
        #add cell_number to the df_parameters bottom row
        df_parameters = pd.concat([df_parameters, pd.DataFrame({'Parameter':'cell_number','Value':[cell_number]})], ignore_index=True)
        #combine output_table and parameters
        df_output_table_parameters = pd.concat([output_table, df_parameters], axis=1)


        #make a measurement overall graph
        try:
            print('start making overall graph')
            fig, ax1 = plt.subplots(figsize=(10,2)) # Plot current and voltage vs time.
            ax2 = ax1.twinx() # Plot double-y axis.
            ax1.plot(output_table["Time (s)"],output_table["Current (A)"], color='b', alpha=0.5) # Plot current vs time.
            ax2.plot(output_table["Time (s)"], output_table["Voltage (V)"], color='r', alpha=0.5) # Plot voltage vs time.
            #ax2.set_ylim(top = 4*voltage_level) 
            ax1.set_xlabel('Time (s)')
            ax1.set_ylabel('Current (A)', color = 'b')
            ax2.set_ylabel('Voltage (V)', color = 'r')
            plt.title(f'Overall graph for cell {cell_number}')
            plt.show(block = False)
            plt.close('all')
            print('finish making overall graph')
        except:
            print('No graph to plot')
            pass 
        
        try:
            print('start making read graph')
            output_table_read = output_table[(output_table['Voltage (V)']>df_output_table_parameters.loc[5,'Value']-0.05) & (output_table['Voltage (V)']<df_output_table_parameters.loc[5,'Value']+0.05)]
            fig, ax = plt.subplots(figsize=(10,2)) # Plot current and voltage vs time.
            ax.plot(output_table_read["Time (s)"],(output_table_read["Current (A)"]/output_table_read["Voltage (V)"]),'o',markersize = 2, color='b', alpha=0.5) # Plot current vs time.
           
            ax.set_xlabel('Time (s)')
            ax.set_ylabel('Conductance (S)')
            plt.title(f'Read condunctance for cell {cell_number}')
            plt.show(block = False)
            plt.close('all')
            print('finish making read graph')
        except:
            print('No graph to plot')
            pass 
        
        #print('debug 1')
        #Fit the potentiation and depression, also save the file

        df_output_parameters_fitting = self.Pot_Dep_Calculation(df_output_table_parameters, cell_number, 'PotDep')

        return df_output_parameters_fitting
    
    @_measurement_result
    def Keysight_HT_PotDep(self, cell_number: int) -> Dict[str, Any]:
        #################
        #initial sampling
        #################
        #this function is to do initial sampling with 5 datapoints
        #the 5 data points are taken from 3 variables [V_write, t_write, t_read] its pre-saved in inital_parameters.csv made by LHS
        #return the df of  delta_v, ave_Pot_Gain, ave_Dep_Gain, ave_Pot_Loss, ave_Dep_Loss
        
        #import initial parameters
        """High-throughput potentiation/depression tuning: iterate over an initial
        parameter sweep (LHS) and Bayesian optimization (:func:`analysis.ht_potdep.main_BO`)
        to tune the write/erase pulse parameters, rewriting the parameter CSV each
        iteration. Requires the ``analysis`` extra (torch/botorch).

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Potent_Depress_2.csv and initial_parameters.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds no files saved by this routine.
                - ``result``: None.
        """
        df_initial_parameters = pd.read_csv(self._param_file('initial_parameters.csv'))

        print("--Start innitial sampling--")
        #for each parameter set, rewrite the setting in the parameter_Keysioght_Potent_Depress.csv
        for i in range(len(df_initial_parameters)):
            time.sleep(60) #wait for 5 minutes
            df_pot_dep_param = pd.read_csv(self._param_file('parameter_Keysight_Potent_Depress_2.csv'))
            df_pot_dep_param.at[1, 'Value'] = df_initial_parameters.at[i, 'V_write'] #write_voltage
            df_pot_dep_param.at[2, 'Value'] = -1*(df_initial_parameters.at[i, 'V_write']) #erase_voltage
            df_pot_dep_param.at[3, 'Value'] = df_initial_parameters.at[i, 't_write'] #pulse_duration
            df_pot_dep_param.at[12, 'Value'] = df_initial_parameters.at[i, 't_pulse_to_pulse']
            #df_pot_dep_param.at[6, 'Value'] = df_initial_parameters.at[i, 't_re'] #read_duration
            #df_pot_dep_param.at[8, 'Value'] = df_initial_parameters.at[i, 't_re'] #trigger_period make it the same with read duration
            #save the new parameters back to this file
            df_pot_dep_param.to_csv(self._param_file('parameter_Keysight_Potent_Depress_2.csv'), index=False)

            #conduct potentiation and depression cycle, get the df_output_parameters_fitting
            df_output_parameters_fitting = self.Keysight_Potent_Depress_2(cell_number)

            #make df_results_summary and save it 
            self._htpd().make_results_summary(df_output_parameters_fitting)
        print("--Innitial sampling finished--")
        
        print('do analog sweeping')
        self.Keysight_Analog_Sweep(cell_number)
        
        
        
        
        ###############
        # BO run code below
        ##############
       
        campaign = 5 #number of iteration campaign
        print("--Start BO sampling. Total campaign = "+str(campaign))
        for i in range(campaign):
            time.sleep(60)
            self._htpd().main_BO(cell_number)

            #conduct potentiation and depression cycle, get the df_output_parameters_fitting
            df_output_parameters_fitting = self.Keysight_Potent_Depress_2(cell_number)

            #make df_results_summary and save it 
            self._htpd().make_results_summary(df_output_parameters_fitting)
        print("--BO sampling finished--")
        
    
    @_measurement_result
    def Keysight_Voc_decay(self, cell_number: int) -> Dict[str, Any]:
        #this function is to measure the voc decay with light on/off
        #the light pulse is done with pico
        #here the read pulse is only pulse in between 0 V
        #the length of the waiting time = t_pulse_to_read + t_read + t_read_to_pulse

        """Open-circuit voltage (Voc) decay: cycle the light on/off and record the Voc
        transient.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Voc_decay.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Conduct Voc rise and decay measurement for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Voc_decay.csv')
        
        #conduct light soaking
        #self.pico_instrument.light_on()
        #print('conduct light soaking for 5 minutes')
        #time.sleep(60*5)
        #self.pico_instrument.light_off()
        
        
        # Dictionary to store parameters
        parameters = {}
        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        #make a wave
        full_on_off_pulses = np.array([], dtype=float) #initialize full on off pulses
        for cycle in range(on_off_cycles):  #pulse_voltages is a list
            # make on off pulses
            on_pulse = np.full(int(light_on_duration/trigger_period), source_current, dtype=float)
            off_pulse = np.full(int(light_off_duration/trigger_period), source_current, dtype=float)
            
            on_off_pulse = np.concatenate([on_pulse, off_pulse])
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_off_pulse]) #this is the full on off pulse

        pulse_train_string = ','.join(map(str,full_on_off_pulses))
        #calculate trigger count
        trigger_count = str(int(len(full_on_off_pulses)))
        #send to instrument
        print('SENDING COMMANDS TO INSTRUMENT')
        #print(on_off_pulse)
        
        try:
            # ********** INITIALIZE SMU **********
        
            self.smu.timeout = 10000000  # 10000s
            self.smu.write_termination = '\n' # To define end of command.
            self.smu.read_termination = '\n' # To define end of command.

            self.smu.write("*RST") # Reset instrument to default
            self.smu.write("*CLS")
            
            self.smu.write(":TRAC:CLE")  # Clear buffer
            self.smu.write(":TRAC:FEED SENS")  # Store measured data in buffer
            self.smu.write(":TRAC:FEED:CONT NEXT")  # Make buffer editable
            self.smu.write(":TRAC:TST:FORM ABS")  # Absolute timestamp format
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")  # Elements to store
            
            self.smu.write(":SOUR:FUNC:MODE CURR")  # Current source mode
            self.smu.write(":SOUR:CURR:MODE LIST")  # List mode
            #self.smu.write(":SOUR:FUNC:TRIG:CONT ON") #Auto advance to the next source point
            self.smu.write(":SENS:FUNCtion 'CURRent','VOLTage'")
            self.smu.write(f":SENS:VOLT:PROT {compliance_voltage}")  # Voltage compliance
            
            self.smu.write(f":LIST:CURR {pulse_train_string}")  # Current list values
            #self.smu.write(":SENS:REM ON") #4-wire measurement mode
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON") #it was 0.2 for cells with low Voc, 2 for higher voc
            
            self.smu.write(f":TRIG:COUN {trigger_count}")  # Trigger count
            self.smu.write(":TRIG:SOUR TIM")  # Timer trigger source
            self.smu.write(f":TRIG:TIM {trigger_period}")  # Trigger period
            measure_delay = str(trigger_period*measure_delay_position)
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")  # sets the dwell time or waiting time before the measurement
            
            self.smu.write(":OUTP ON")  # Turn on output
            self.smu.write(":INIT")  # Initiate measurements
            

            # ********** light pulse **********
            print('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_light_pulse(light_intensity=light_intensity, 
                                        on_off_cycles=on_off_cycles, 
                                        light_on_duration=light_on_duration, 
                                        light_off_duration=light_off_duration)

            # ********** READ BUFFER **********
            print('READING BUFFER')
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            print(self.smu.query(":SYST:ERR?")) # Check error buffer. If there was any error in the execution of SCPI commands. 
            self.smu.write("*RST")
            #smu.close() # Close SMU object.
        
        except Exception as e:
            print("ERROR:", e)
            print(self.smu.query(":SYST:ERR?"))
            output_string = 'NONE'
             
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        print("output_table from SMU acquired")
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(output_table, df_parameters, cell_number,'Voc_decay')


        ## make a graph
        try:
            print('start making graph')
            fig, ax1 = plt.subplots(figsize=(10,2)) # Plot current and voltage vs time.
            ax2 = ax1.twinx() # Plot double-y axis.
            ax1.plot(output_table["Time (s)"],output_table["Current (A)"], color='b', alpha=0.5) # Plot current vs time.
            ax2.plot(output_table["Time (s)"], output_table["Voltage (V)"], color='r', alpha=0.5) # Plot voltage vs time.
            #ax2.set_ylim(top = 4*voltage_level) 
            ax1.set_xlabel('Time (s)')
            ax1.set_ylabel('Current (A)', color = 'b')
            ax2.set_ylabel('Voltage (V)', color = 'r')
            plt.title(f'Overall graph for cell {cell_number}')
            plt.show(block = False)
            plt.close('all')
            print('finish making graph')
        except:
            print('No graph to plot')
            pass 


    @_measurement_result
    def Keysight_Voc_profile(self, cell_number: int) -> Dict[str, Any]:
        #this function is to measure the voc decay with light on/off
        #the light pulse is done with pico

        """Voc profile: idle, then light on/off cycles, then a read period, recording
        the Voc transient.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Voc_profile.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Conduct Voc rise and decay measurement for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Voc_profile.csv')
        
        #conduct light soaking
        #self.pico_instrument.light_on()
        #print('conduct light soaking for 5 minutes')
        #time.sleep(60*5)
        #self.pico_instrument.light_off()
        
        # Dictionary to store parameters
        parameters = {}
        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        #make a light pulse train with wait time before the pulses
        pre_exposure_time = np.full(int(wait_time/trigger_period), source_current, dtype=float)
        full_on_off_pulses = np.array([], dtype=float) #initialize full on_off pulses
        for cycle in range(on_off_cycles):  #pulse_voltages is a list
            # make on off pulses
            on_pulse = np.full(int(light_on_duration/trigger_period), source_current, dtype=float)
            off_pulse = np.full(int(light_off_duration/trigger_period), source_current, dtype=float)
            
            on_off_pulse = np.concatenate([on_pulse, off_pulse])
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_off_pulse])
        
        post_exposure_time = np.full(int(read_time/trigger_period), source_current, dtype=float)
        #add pre exposure time at the beginning
        full_on_off_pulses = np.concatenate([pre_exposure_time, full_on_off_pulses, post_exposure_time])
        pulse_train_string = ','.join(map(str,full_on_off_pulses))
        #calculate trigger count
        trigger_count = str(int(len(full_on_off_pulses)))
        #send to instrument
        print('SENDING COMMANDS TO INSTRUMENT')
        #print(on_off_pulse)
        
        try:
            # ********** INITIALIZE SMU **********
        
            self.smu.timeout = 10000000  # 10000s
            self.smu.write_termination = '\n' # To define end of command.
            self.smu.read_termination = '\n' # To define end of command.

            self.smu.write("*RST") # Reset instrument to default
            self.smu.write("*CLS")
            
            self.smu.write(":TRAC:CLE")  # Clear buffer
            self.smu.write(":TRAC:FEED SENS")  # Store measured data in buffer
            self.smu.write(":TRAC:FEED:CONT NEXT")  # Make buffer editable
            self.smu.write(":TRAC:TST:FORM ABS")  # Absolute timestamp format
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")  # Elements to store
            
            self.smu.write(":SOUR:FUNC:MODE CURR")  # Current source mode
            self.smu.write(":SOUR:CURR:MODE LIST")  # List mode
            #self.smu.write(":SOUR:FUNC:TRIG:CONT ON") #Auto advance to the next source point
            self.smu.write(":SENS:FUNCtion 'CURRent','VOLTage'")
            self.smu.write(f":SENS:VOLT:PROT {compliance_voltage}")  # Voltage compliance
            
            self.smu.write(f":LIST:CURR {pulse_train_string}")  # Current list values
            #self.smu.write(":SENS:REM ON") #4-wire measurement mode
            self.smu.write(f":SENS:VOLT:NPLC {NPLC_value}") #NPLC value
            self.smu.write(f":SENS:CURR:NPLC {NPLC_value}") #NPLC value
            #self.smu.write(":SENS:VOLT:RANG:AUTO ON")
            #self.smu.write(":SENS:CURR:RANG:AUTO ON")
            self.smu.write(":SENS:VOLT:RANG:AUTO OFF") # first set the autorange function OFF
            self.smu.write(f":SENS:VOLT:RANG {volt_sense_range}") #it was 0.2 for cells with low Voc, 2 for higher voc
            self.smu.write(":SENS:CURR:RANG:AUTO OFF") # first set the autorange function OFF
            self.smu.write(f":SENS:CURR:RANG {curr_sense_range}") #sets the range for current sense function
            
            self.smu.write(f":TRIG:COUN {trigger_count}")  # Trigger count
            self.smu.write(":TRIG:SOUR TIM")  # Timer trigger source
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")  # Trigger period
            measure_delay = str(trigger_period*measure_delay_position)
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")  # sets the dwell time or waiting time before the measurement
            
            self.smu.write(":OUTP ON")  # Turn on output
            self.smu.write(":INIT")  # Initiate measurements

            # ********** light pulse **********
            print('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_profile_light_pulse(light_intensity=light_intensity,
                                                idle_time=wait_time,
                                                on_off_cycles=on_off_cycles, 
                                                light_on_duration=light_on_duration, 
                                                light_off_duration=light_off_duration,
                                                read_period=read_time)

            # ********** READ BUFFER **********
            print('READING BUFFER')
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            print(self.smu.query(":SYST:ERR?")) # Check error buffer. If there was any error in the execution of SCPI commands. 
            self.smu.write("*RST")
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        print("output_table from SMU acquired")
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile_1(output_table, df_parameters, cell_number,'Voc_profile', "x_time")
        self.make_graph_IV_1(output_table, cell_number, plot_type="x_time")

    @_measurement_result
    def Keysight_Jsc_profile(self, cell_number: int) -> Dict[str, Any]:
        #this function is to measure the voc decay with light on/off
        #the light pulse is done with pico

        """Short-circuit current (Jsc) profile under light on/off cycling.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Jsc_profile.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Conduct Jsc rise and decay measurement for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Jsc_profile.csv')
        
        #conduct light soaking
        #self.pico_instrument.light_on()
        #print('conduct light soaking for 5 minutes')
        #time.sleep(60*5)
        #self.pico_instrument.light_off()
        
        # Dictionary to store parameters
        parameters = {}
        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        #make a light pulse train with wait time before the pulses
        pre_exposure_time = np.full(int(wait_time/trigger_period), source_voltage, dtype=float)
        full_on_off_pulses = np.array([], dtype=float) #initialize full on_off pulses
        for cycle in range(on_off_cycles):  #pulse_voltages is a list
            # make on off pulses
            on_pulse = np.full(int(light_on_duration/trigger_period), source_voltage, dtype=float)
            off_pulse = np.full(int(light_off_duration/trigger_period), source_voltage, dtype=float)
            
            on_off_pulse = np.concatenate([on_pulse, off_pulse])
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_off_pulse])
        
        post_exposure_time = np.full(int(read_time/trigger_period), source_voltage, dtype=float)
        #add pre exposure time at the beginning
        full_on_off_pulses = np.concatenate([pre_exposure_time, full_on_off_pulses, post_exposure_time])
        pulse_train_string = ','.join(map(str,full_on_off_pulses))
        #calculate trigger count
        trigger_count = str(int(len(full_on_off_pulses)))
        #send to instrument
        print('SENDING COMMANDS TO INSTRUMENT')
        #print(on_off_pulse)
        
        try:
            # ********** INITIALIZE SMU **********
        
            self.smu.timeout = 10000000  # 10000s
            self.smu.write_termination = '\n' # To define end of command.
            self.smu.read_termination = '\n' # To define end of command.

            self.smu.write("*RST") # Reset instrument to default
            self.smu.write("*CLS")
            
            self.smu.write(":TRAC:CLE")  # Clear buffer
            self.smu.write(":TRAC:FEED SENS")  # Store measured data in buffer
            self.smu.write(":TRAC:FEED:CONT NEXT")  # Make buffer editable
            self.smu.write(":TRAC:TST:FORM ABS")  # Absolute timestamp format
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")  # Elements to store
            
            self.smu.write(":SOUR:FUNC:MODE VOLT")  # Current source mode
            self.smu.write(":SOUR:VOLT:MODE LIST")  # List mode
            #self.smu.write(":SOUR:FUNC:TRIG:CONT ON") #Auto advance to the next source point
            self.smu.write(":SENS:FUNCtion 'CURRent','VOLTage'")
            self.smu.write(f":SENS:CURR:PROT {str(compliance_current)}")  # Voltage compliance
            
            self.smu.write(f":LIST:VOLT {pulse_train_string}")  # Current list values
            #self.smu.write(":SENS:REM ON") #4-wire measurement mode
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON") #it was 0.2 for cells with low Voc, 2 for higher voc
            
            self.smu.write(f":TRIG:COUN {trigger_count}")  # Trigger count
            self.smu.write(":TRIG:SOUR TIM")  # Timer trigger source
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")  # Trigger period
            measure_delay = str(trigger_period*measure_delay_position)
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")  # sets the dwell time or waiting time before the measurement
            
            self.smu.write(":OUTP ON")  # Turn on output
            self.smu.write(":INIT")  # Initiate measurements

            # ********** light pulse **********
            print('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_profile_light_pulse(light_intensity=light_intensity,
                                                idle_time=wait_time,
                                                on_off_cycles=on_off_cycles, 
                                                light_on_duration=light_on_duration, 
                                                light_off_duration=light_off_duration,
                                                read_period=read_time)

            # ********** READ BUFFER **********
            print('READING BUFFER')
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            print(self.smu.query(":SYST:ERR?")) # Check error buffer. If there was any error in the execution of SCPI commands. 
            self.smu.write("*RST")
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        print("output_table from SMU acquired")
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile_1(output_table, df_parameters, cell_number,'Jsc_profile', "x_time")
        self.make_graph_IV_1(output_table, cell_number, plot_type="x_time")

   
    @_measurement_result
    def Keysight_Voc_decay_indiv_soaking(self, cell_number: int) -> Dict[str, Any]:
        #this function is to measure the voc decay with light on/off
        #the light pulse is done with pico
                #here the read pulse is only pulse in between 0 V
        #the length of the waiting time = t_pulse_to_read + t_read + t_read_to_pulse

        """Voc decay with multi-level light soaking before the on/off cycles.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Voc_decay_indiv_soaking.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Conduct Voc rise and decay measurement for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Voc_decay_indiv_soaking.csv')
        
        #conduct light soaking
        #self.pico_instrument.light_on()
        #print('conduct light soaking for 5 minutes')
        #time.sleep(60*5)
        #self.pico_instrument.light_off()
        
        # Dictionary to store parameters
        parameters = {}
        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value

        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value

        #make a wave
        full_on_off_pulses = np.array([], dtype=float) #initialize full on off pulses
        soaking_pulse = np.full(int(soaking_time/trigger_period), source_current, dtype=float)
        for cycle in range(on_off_cycles):  #pulse_voltages is a list
            # make on off pulses
    
            off_pulse1 = np.full(int(light_off_duration1/trigger_period), source_current, dtype=float)
            on_pulse1 = np.full(int(light_on1/trigger_period), source_current, dtype=float)
            
            off_pulse2 = np.full(int(light_off_duration2/trigger_period), source_current, dtype=float)
            on_pulse2 = np.full(int(light_on2/trigger_period), source_current, dtype=float)
            
            off_pulse3 = np.full(int(light_off_duration3/trigger_period), source_current, dtype=float)
            on_pulse3 = np.full(int(light_on3/trigger_period), source_current, dtype=float)
            
            off_pulse4 = np.full(int(light_off_duration4/trigger_period), source_current, dtype=float)
            on_pulse4 = np.full(int(light_on4/trigger_period), source_current, dtype=float)
            
            off_pulse5 = np.full(int(light_off_duration5/trigger_period), source_current, dtype=float)
            on_pulse5 = np.full(int(light_on5/trigger_period), source_current, dtype=float)
            
            
            
            on_off_pulse = np.concatenate([off_pulse1,on_pulse1,
                                           off_pulse2,on_pulse2,
                                           off_pulse3,on_pulse3,
                                           off_pulse4,on_pulse4,
                                           off_pulse5,on_pulse5 ])
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_off_pulse]) #this is the full on off pulse
        full_soak_on_off_pulses = np.concatenate([soaking_pulse, full_on_off_pulses])
        #calculate trigger count
        trigger_count = str(int(len(full_soak_on_off_pulses)))
        #send to instrument
        print('SENDING COMMANDS TO INSTRUMENT')
        #print(on_off_pulse)
        
        try:
            # ********** INITIALIZE SMU **********
        
            self.smu.timeout = 10000000  # 10000s
            self.smu.write_termination = '\n' # To define end of command.
            self.smu.read_termination = '\n' # To define end of command.
            
            self.smu.write("*RST")  # Reset instrument to default
            self.smu.write(":TRAC:CLE")  # Clear buffer
            self.smu.write(f":TRAC:POIN {trigger_count}")  # Set buffer size
            self.smu.write(":TRAC:FEED SENS")  # Store measured data in buffer
            self.smu.write(":TRAC:FEED:CONT NEXT")  # Make buffer editable
            self.smu.write(":TRAC:TST:FORM ABS")  # Absolute timestamp format
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")  # Elements to store
            
            self.smu.write(":SOUR:FUNC:MODE CURR")  # Current source mode
            self.smu.write(":SOUR:CURR:MODE LIST")  # List mode
            self.smu.write(f":SENS:VOLT:PROT {compliance}")  # Voltage compliance
            
            self.smu.write(f":LIST:CURR {full_soak_on_off_pulses}")  # Current list values
            self.smu.write("SENS:VOLT:RANG AUTO") #it was 0.2 for cells with low Voc, 2 for higher voc
            #self.smu.write(":LIST:STEP AUTO")  # Auto-advance through list
            #self.smu.write(":LIST:DWELL FIXED")  # Fixed dwell time
            
            self.smu.write(f":TRIG:COUN {trigger_count}")  # Trigger count
            self.smu.write(":TRIG:SOUR TIMER")  # Timer trigger source
            self.smu.write(f":TRIG:TIM {trigger_period}")  # Trigger period
            #self.smu.write(f":TRIG:ACQ:DEL {str(measure_delay)}")  # Only if needed
            
            self.smu.write(":OUTP ON")  # Turn on output
            self.smu.write(":INIT")  # Initiate measurements
 


            # ********** light pulse **********
            print('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_light_pulse_soak(light_intensity=light_intensity, 
                                        on_off_cycles=on_off_cycles, 
                                        soaking_time = soaking_time,
                                        light_off_duration1=light_off_duration1,
                                        light_on1=light_on1, 
                                        light_off_duration2=light_off_duration2,
                                        light_on2=light_on2, 
                                        light_off_duration3=light_off_duration3,
                                        light_on3=light_on3, 
                                        light_off_duration4=light_off_duration4,
                                        light_on4=light_on4, 
                                        light_off_duration5=light_off_duration5,
                                        light_on5=light_on5, 
                                        )

            # ********** READ BUFFER **********
            print('READING BUFFER')
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            print(self.smu.query("SYST:ERR?")) # Check error buffer. If there was any error in the execution of SCPI commands. 
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        print("output_table from SMU acquired")
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile(output_table, df_parameters, cell_number,'Voc_decay_indiv_soaking')


        ## make a graph
        try:
            print('start making graph')
            fig, ax1 = plt.subplots(figsize=(10,2)) # Plot current and voltage vs time.
            ax2 = ax1.twinx() # Plot double-y axis.
            ax1.plot(output_table["Time (s)"],output_table["Current (A)"], color='b', alpha=0.5) # Plot current vs time.
            ax2.plot(output_table["Time (s)"], output_table["Voltage (V)"], color='r', alpha=0.5) # Plot voltage vs time.
            #ax2.set_ylim(top = 4*voltage_level) 
            ax1.set_xlabel('Time (s)')
            ax1.set_ylabel('Current (A)', color = 'b')
            ax2.set_ylabel('Voltage (V)', color = 'r')
            plt.title(f'Overall graph for cell {cell_number}')
            plt.show(block = False)
            plt.close('all')
            print('finish making graph')
        except:
            print('No graph to plot')
            pass 

    @_measurement_result
    def Keysight_Voc_decay_ON_OFF_Variation(self, cell_number: int) -> Dict[str, Any]:
        # this program is used to measure the impact of varying light soaking time as light pulse on Voc
        """Voc decay with variable light on/off durations per cycle.

        Args:
            cell_number: 1-based cell index, used in the saved file name(s).
                Measurement settings are read from ``parameter_Keysight_Voc_decay_ON_OFF_Variation.csv``
                (columns ``Parameter, Value``).

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`):
                - ``outputs``: one record per saved file; each ``data`` holds current/voltage/time columns (e.g. 'Voltage (V)', 'Current (A)', 'Time (s)').
                - ``result``: None.
        """
        print('Conduct Voc rise and decay measurement under varying light pulse for cell '+str(cell_number))
        file_parameters = self._param_file('parameter_Keysight_Voc_decay_ON_OFF_Variation.csv')
        
        # Dictionary to store parameters
        parameters = {}
        # Read the measurement parameters CSV file
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)  # Skip the header row
            for rows in reader:
                #skip empty rows
                if not rows or len(rows)<2:
                    continue
                key = rows[0].strip()
                value = rows[1].strip()
                # Convert value to float or int if possible
                try:
                    #attempt to convert to float first
                    value = float(value)
                    #if the float is an integer, then change to integer
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass  # Keep as string if conversion fails
                parameters[key] = value
    
        # Assign variables dynamically
        for key, value in parameters.items():
            globals()[key] = value
    
        #design of current signals for the waveform
        pre_exposure_pulse = np.full(int(wait_time/trigger_period), source_current, dtype=float)
        full_on_off_pulses = np.array([], dtype=float) #initialize full on off pulses
        for cycle in range(on_off_cycles):
            # make on off pulses
            on_pulse1 = np.full(int(light_on1/trigger_period), source_current, dtype=float)
            off_pulse1 = np.full(int(light_off_duration1/trigger_period), source_current, dtype=float)
            
            on_pulse2 = np.full(int(light_on2/trigger_period), source_current, dtype=float)
            off_pulse2 = np.full(int(light_off_duration2/trigger_period), source_current, dtype=float)
            
            on_pulse3 = np.full(int(light_on3/trigger_period), source_current, dtype=float)
            off_pulse3 = np.full(int(light_off_duration3/trigger_period), source_current, dtype=float)
            
            on_pulse4 = np.full(int(light_on4/trigger_period), source_current, dtype=float)
            off_pulse4 = np.full(int(light_off_duration4/trigger_period), source_current, dtype=float)
            
            on_pulse5 = np.full(int(light_on5/trigger_period), source_current, dtype=float)
            off_pulse5 = np.full(int(light_off_duration5/trigger_period), source_current, dtype=float)
           
            on_off_pulse = np.concatenate([on_pulse1,off_pulse1,
                                           on_pulse2,off_pulse2,
                                           on_pulse3,off_pulse3,
                                           on_pulse4,off_pulse4,
                                           on_pulse5,off_pulse5])
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_off_pulse]) #this is the full on off pulse
            wait_time_full_on_off_pulses = np.concatenate([pre_exposure_pulse, full_on_off_pulses])
        pulse_train = wait_time_full_on_off_pulses
        pulse_train_string = ','.join(map(str,pulse_train))
        #calculate trigger count
        trigger_count = str(int(len(wait_time_full_on_off_pulses)))
        #send to instrument
        if self.smu is None:
            print("Error: SMU is not connected. Aborting measurement.")
            return None
        
        print('SENDING COMMANDS TO INSTRUMENT')
        
        try:
            # ********** INITIALIZE SMU **********
        
            self.smu.timeout = 10000000  # 10000s
            self.smu.write_termination = '\n' # To define end of command.
            self.smu.read_termination = '\n' # To define end of command.
            self.smu.write("*RST")  # Reset instrument to default
            self.smu.write("*CLS") # Clears the command queue.
            self.smu.query(":SYST:ERR?")
            self.smu.write(":TRAC:CLE")  # Clear buffer
            self.smu.write(":TRAC:FEED SENS")  # Store measured data in buffer
            self.smu.write(":TRAC:FEED:CONT NEXT")  # Make buffer editable
            self.smu.write(":TRAC:TST:FORM ABS")  # Absolute timestamp format
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")  # Elements to store
            self.smu.write(":SOUR:FUNC:MODE CURR")  # Current source mode
            self.smu.write(":SOUR:CURR:MODE LIST")  # List mode
            self.smu.write(f":LIST:CURR {pulse_train_string}")  # Current list values
            self.smu.write(":SENS:FUNC 'CURR', 'VOLT'")
            self.smu.write(f":SENS:VOLT:PROT {str(voltage_compliance)}")  # Voltage compliance
            self.smu.write(":SENS:REM ON") #4-wire measurement mode
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON")
            self.smu.write(f":TRIG:COUN {trigger_count}")  # Trigger count
            self.smu.write(":TRIG:SOUR TIM")  # Timer trigger source
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")  # Trigger period
            measure_delay = trigger_period * measure_delay_position
            self.smu.write(f":TRIG:ACQ:DEL {str(measure_delay)}")  # sets the dwell time or waiting time before the measurement
            
            self.smu.write(":OUTP ON")  # Turn on output
            self.smu.write(":INIT")  # Initiate measurements
    
            # ********** light pulse **********
            print('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.light_pulse_ON_OFF_variation(light_intensity=light_intensity,on_off_cycles=on_off_cycles,idle_time=wait_time,
                                                            light_on1=light_on1,light_off_duration1=light_off_duration1, 
                                                            light_on2=light_on2,light_off_duration2=light_off_duration2,
                                                            light_on3=light_on3,light_off_duration3=light_off_duration3,
                                                            light_on4=light_on4,light_off_duration4=light_off_duration4,
                                                            light_on5=light_on5,light_off_duration5=light_off_duration5)
    
            # ********** READ BUFFER **********
            print('READING BUFFER')
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            #output_string = self.smu.query(":TRAC:DATA? 1,10")
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            print(self.smu.query(":SYST:ERR?")) # Check error buffer. If there was any error in the execution of SCPI commands. 
            #self.smu.close() # Close SMU object.
            
        except Exception as e:
            print(f"Error communicating with SMU: {e}")
            output_string = 'NONE'
    
        #change the output_string to output_table (DF)
        output_table = self.string_to_dataframe(output_string)
        print("output_table from SMU acquired")
        #save file, adding parameters to the next rows
        df_parameters = pd.read_csv(file_parameters)
        self.savefile_1(output_table, df_parameters, cell_number,'Light_ON_OFF_Variation',"x_time")
    
        self.make_graph_IV_1(output_table, cell_number, plot_type="x_time")
              
    @_measurement_result
    def Keysight_Time_Gap(self, value: int = None) -> Dict[str, Any]:
        
        """Idle / wait step used to insert delays into a measurement queue.

        Sleeps for the ``sleep`` seconds defined in ``parameter_Keysight_Time_Gap.csv``.

        Args:
            value: unused; present so the queue can call it with the common
                ``(cell_number)`` signature.

        Returns:
            Dict[str, Any]: standard measurement envelope (see :func:`_measurement_result`);
                ``outputs`` is empty and ``result`` is ``None``.
        """
        file_parameters = self._param_file('parameter_Keysight_Time_Gap.csv')
    
        parameters = {}
    
        with open(file_parameters, mode='r') as infile:
            reader = csv.reader(infile)
            next(reader)
    
            for rows in reader:
                if not rows or len(rows) < 2:
                    continue
    
                key = rows[0].strip()
                value = rows[1].strip()
    
                try:
                    value = float(value)
                    if value.is_integer():
                        value = int(value)
                except ValueError:
                    pass
    
                parameters[key] = value
    
        if "sleep" in parameters: 
            sleep_time = parameters["sleep"]
        else: 
            sleep_time = 0
    
        print(f"Sleeping for {sleep_time} seconds...")
        time.sleep(sleep_time)


MeasurementProbot = ProbotMeasurement
