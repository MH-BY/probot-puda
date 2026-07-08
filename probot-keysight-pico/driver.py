"""Self-contained driver for the ``probot-keysight-pico`` edge.

This one module bundles everything the edge needs, in dependency order:

* :class:`KeysightProbot` - the Keysight SMU PyVISA transport (opens the session).
* :class:`PicoProbot` - the Pico G2V light controller.
* :class:`KeysightPicoProbotMachine` - the PUDA machine that composes the two and
  defines every ``Keysight_*`` measurement command directly on its class body.

``main.py`` imports :class:`KeysightPicoProbotMachine` from here (``from driver
import ...``), mirroring the ViPSA one-folder-per-edge layout.

The Keysight SMU and the Pico light live in **one** machine because several
measurements drive the light inline, during the SMU acquisition, with sub-second
timing (``Keysight_Light_Pulse``, ``Keysight_Voc_decay``, ``Keysight_Voc_profile``,
``Keysight_Jsc_profile``, ``Keysight_Voc_decay_indiv_soaking``,
``Keysight_Voc_decay_ON_OFF_Variation``). The stage is a *separate* edge.

IMPORTANT (PUDA): PUDA exposes only the public methods **defined directly on this
class** as machine commands - it does NOT expose inherited methods. So every
``Keysight_*`` measurement is defined on this class body (not in a mixin/base
class). SCPI/data/save/plot helpers are ``_``-prefixed so they are not exposed.
The measurement routines use ``self.smu`` (the raw PyVISA resource, via a property
over the SMU sub-driver) and ``self.pico_instrument`` (the light).
"""

from __future__ import annotations

import csv
import os
import ast
import time
import functools
from datetime import datetime
from typing import Any, Dict
import logging

import numpy as np
import pandas as pd
from scipy.stats import linregress

logger = logging.getLogger(__name__)


# ===========================================================================
# SMU transport sub-driver — owns the Keysight PyVISA session.
# The measurement routines below reach the raw resource via ``self.smu``.
# ===========================================================================
class KeysightProbot:
    """Own the PyVISA session for the probot Keysight source-measure unit."""

    instrument_family = "keysight_smu_probot"

    def __init__(self, address: str | None = None, device_no: int = 0) -> None:
        """Store connection config (does not connect).

        Args:
            address: VISA resource string (e.g. ``"USB0::0x0957::...::INSTR"``).
                When ``None``, :meth:`startup` falls back to the ``device_no``-th
                resource returned by the VISA resource manager.
            device_no: Index into ``list_resources()`` used only when ``address``
                is not given.
        """
        self.address = address
        self.device_no = device_no
        self.rm = None
        self.smu = None  # raw pyvisa resource; consumed by the measurement routines

    @property
    def is_connected(self) -> bool:
        """Return True once :meth:`startup` has opened the VISA session."""
        return self.smu is not None

    def startup(self) -> bool:
        """Open the VISA session to the SMU."""
        if self.is_connected:
            return True
        try:
            import pyvisa

            self.rm = pyvisa.ResourceManager()
            address = self.address
            if address is None:
                resources = list(self.rm.list_resources())
                if not resources:
                    raise RuntimeError("No VISA resources found")
                address = resources[self.device_no]
            self.address = address
            self.smu = self.rm.open_resource(address)
            logger.info("Connected to Keysight SMU at %s", address)
            return True
        except Exception:
            logger.exception("Failed to connect to Keysight SMU (address=%s)", self.address)
            self.smu = None
            return False

    def identify(self) -> str:
        """Return the SMU ``*IDN?`` string."""
        return self.smu.query("*IDN?")

    def shutdown(self) -> bool:
        """Turn the output off (best effort) and close the VISA session."""
        try:
            if self.is_connected:
                try:
                    self.smu.write(":OUTP OFF")
                except Exception:
                    logger.exception("Error turning SMU output off during shutdown")
                self.smu.close()
        finally:
            self.smu = None
        return True


# ===========================================================================
# Pico G2V light sub-driver — controlled inline by several measurements.
# ===========================================================================
class PicoProbot:
    """Control the probot Pico G2V LED over Ethernet.

    The constructor only stores configuration; call :meth:`startup` to open the
    connection. Global intensity is on a 0-100 scale.
    """

    def __init__(self, ip: str | None = None, device_id: str | None = None) -> None:
        """Store connection config (does not connect).

        Args:
            ip: IP address of the Pico controller (typically link-local,
                e.g. ``"169.254.x.x"``).
            device_id: Pico device serial id (the controller's hardware id).
        """
        self.ip = ip
        self.device_id = device_id
        self.pico = None

    @property
    def is_connected(self) -> bool:
        """Return True once :meth:`startup` has opened the device."""
        return self.pico is not None

    def startup(self) -> bool:
        """Open the connection to the Pico controller and turn the light off.

        Returns:
            True on success, False if the device could not be reached.
        """
        if self.is_connected:
            return True
        try:
            from g2vpico import G2VPico

            self.pico = G2VPico(self.ip, self.device_id)
            self.light_off()
            logger.info("Connected to Pico G2V at %s (%s)", self.ip, self.device_id)
            return True
        except Exception:
            logger.exception("Failed to connect to Pico G2V at %s", self.ip)
            self.pico = None
            return False

    def shutdown(self) -> bool:
        """Turn the light off and drop the device handle."""
        try:
            if self.is_connected:
                self.light_off()
        except Exception:
            logger.exception("Error while turning Pico light off during shutdown")
        finally:
            self.pico = None
        return True

    # ------------------------------------------------------------------
    # Light primitives (ported verbatim from pico.py)
    # ------------------------------------------------------------------

    def light_on(self):
        self.pico.set_global_intensity(100)

    def light_off(self):
        self.pico.set_global_intensity(0)

    def light_pulse(self, light_intensity, read_duration, light_on_duration, light_off_duration):
        no_of_cycle = int(read_duration / (light_on_duration + light_off_duration))
        for i in range(no_of_cycle):
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on_duration)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration)

    def voc_light_pulse(self, light_intensity, on_off_cycles, light_on_duration, light_off_duration):
        for i in range(on_off_cycles):
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on_duration)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration)

    def voc_profile_light_pulse(self, light_intensity, idle_time, on_off_cycles, light_on_duration, light_off_duration, read_period):
        self.pico.set_global_intensity(0)
        time.sleep(idle_time)

        for i in range(on_off_cycles):
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on_duration)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration)

        self.pico.set_global_intensity(0)
        time.sleep(read_period)

    def voc_light_pulse_soak(self, light_intensity, on_off_cycles, soaking_time,
                             light_off_duration1,
                             light_on1,
                             light_off_duration2,
                             light_on2,
                             light_off_duration3,
                             light_on3,
                             light_off_duration4,
                             light_on4,
                             light_off_duration5,
                             light_on5):
        # start with soaking
        self.pico.set_global_intensity(light_intensity)
        time.sleep(soaking_time)
        # then on/off
        for i in range(on_off_cycles):
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration1)
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on1)

            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration2)
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on2)

            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration3)
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on3)

            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration4)
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on4)

            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration5)
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on5)

    def light_pulse_ON_OFF_variation(self, light_intensity, on_off_cycles, idle_time,
                                     light_on1, light_off_duration1,
                                     light_on2, light_off_duration2,
                                     light_on3, light_off_duration3,
                                     light_on4, light_off_duration4,
                                     light_on5, light_off_duration5):
        self.pico.set_global_intensity(0)
        time.sleep(idle_time)
        for i in range(on_off_cycles):
            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on1)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration1)

            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on2)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration2)

            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on3)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration3)

            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on4)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration4)

            self.pico.set_global_intensity(light_intensity)
            time.sleep(light_on5)
            self.pico.set_global_intensity(0)
            time.sleep(light_off_duration5)


MEASUREMENT_NAMES = [
    'Keysight_Voc_decay_ON_OFF_Variation', 'Keysight_Voc_decay_indiv_soaking',
    'Keysight_Voc_decay', 'Keysight_Potent_Depress_2',
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


_DEFAULT_DATA_DIR = os.path.join("Data", "Keysight")


class KeysightPicoProbotMachine:
    """The probot SMU+light machine: Keysight SMU + Pico light + all measurements."""

    instrument_family = "probot_keysight_pico"

    def __init__(
        self,
        smu_address: str | None = None,
        smu_device_no: int = 0,
        pico_ip: str | None = None,
        pico_id: str | None = None,
        data_dir: str | None = None,
    ) -> None:
        """Wire up the SMU + light sub-controllers (does not connect).

        Measurement settings arrive as method kwargs (with defaults), so there is no
        parameter directory to configure - only ``data_dir`` for where results are
        written.
        """
        self._smu = KeysightProbot(address=smu_address, device_no=smu_device_no)
        self.light = PicoProbot(ip=pico_ip, device_id=pico_id)
        # Alias expected by the measurement routines.
        self.pico_instrument = self.light

        self._data_dir = data_dir or os.environ.get("PROBOT_DATA_DIR") or _DEFAULT_DATA_DIR

        logger.info(
            "KeysightPicoProbotMachine initialised (smu_address=%s, pico_ip=%s, data_dir=%s)",
            smu_address, pico_ip, self._data_dir,
        )

    @property
    def smu(self):
        """Raw PyVISA resource (consumed by the measurement routines)."""
        return self._smu.smu

    # ------------------------------------------------------------------
    # Lifecycle (PUDA commands)
    # ------------------------------------------------------------------

    def startup(self) -> bool:
        """Connect the SMU and the light. Returns True if both connected."""
        logger.info("Starting up SMU + light")
        ok_smu = self._smu.startup()
        ok_light = self.light.startup()
        if not (ok_smu and ok_light):
            logger.warning("SMU+light startup partial (smu=%s, light=%s)", ok_smu, ok_light)
        return ok_smu and ok_light

    def shutdown(self) -> bool:
        """Shut down the SMU and the light. Used by ``puda machine shutdown``."""
        return all([self._smu.shutdown(), self.light.shutdown()])

    def home(self) -> bool:
        """No homing for the SMU/light. Used by ``puda machine home``."""
        return True

    def reset(self) -> bool:
        """Software-reset: reconnect the SMU and light. Used by ``puda machine reset``."""
        self.shutdown()
        return self.startup()

    def get_position(self) -> dict:
        """Telemetry: the SMU/light machine has no spatial position."""
        return {}

    def is_connected(self) -> bool:
        """Telemetry: True when both the SMU and the light are connected."""
        return self._smu.is_connected and self.light.is_connected

    def identify(self) -> str:
        """Return the SMU ``*IDN?`` identification string."""
        return self._smu.identify()

    def measurement_list(self) -> list:
        """Return the available measurement command names."""
        return measurement_list()

    def light_on(self):
        """Turn the Pico light fully on (100%)."""
        return self.light.light_on()

    def light_off(self):
        """Turn the Pico light off (0%)."""
        return self.light.light_off()


    def _data_path(self, *parts):
        """Return (creating if needed) a data output directory under the data root."""
        d = os.path.join(self._data_dir, *parts)
        os.makedirs(d, exist_ok=True)
        return d


    def _params_df(self, parameters: dict) -> "pd.DataFrame":
        """Build the ``Parameter, Value`` settings DataFrame saved alongside data.

        Replaces the original ``pd.read_csv(parameter_*.csv)`` now that settings
        come in as method arguments; the two-column layout matches what the
        original CSV-based flow produced for :meth:`_savefile` / :meth:`_savefile_1`.
        """
        return pd.DataFrame({
            "Parameter": list(parameters.keys()),
            "Value": list(parameters.values()),
        })

    def _make_voltage_pulses(self,pulse_voltage,read_voltage,trigger_period,measure_delay_position,pulse_duration,read_duration,no_of_pulses):
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
    
    
    def _send_pulse_train_to_keysight(self, compliance, pulse_train_string,trigger_count,trigger_period,measure_delay):
        
        """Run one voltage-list sweep on the SMU and return the raw buffer string.

        Resets and configures the SMU as a voltage-list source that measures current and
        voltage with the given ``compliance`` (mA), loads ``pulse_train_string``, runs
        ``trigger_count`` timer-triggered points spaced by ``trigger_period`` seconds
        (measuring ``measure_delay`` into each point), then fetches the buffer.

        Returns:
            str: comma-separated ``time,voltage,current`` triples, or ``'NONE'`` on error.
        """
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
            self.smu.write(":OUTP ON") # Turns SMU output on.
            self.smu.write(":INIT") # Initiate measurement.
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            #self.smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        return output_string


    
    def _string_to_dataframe(self,output_string):# change the output data (string) to dataframe
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

            return output_table
        except:
            pass
    
    def _savefile(self,output_table,df_parameters,cell_number,keyword):
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
            return file_path
        except:
            return None

    def _savefile_1(self,output_table,df_parameters,cell_number,keyword,plot_type):
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
            
            return base_path + ".csv"
        except Exception as e:
            return None

    


    def Keysight_analog_pulse(
        self,
        cell_number: int,
        pulse_voltage: float = 0.3,
        read_voltage: float = 0.2,
        trigger_period: float = 0.1,
        measure_delay_position: float = 0.5,
        pulse_duration: float = 1.0,
        read_duration: float = 60.0,
        no_of_pulses: int = 5,
        compliance: float = 1.0,
    ) -> list[dict]:
        """Apply a train of identical read/write voltage pulses and record the current.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            pulse_voltage: pulse voltage (V). Default 0.3.
            read_voltage: read voltage (V). Default 0.2.
            trigger_period: trigger period (s). Default 0.1.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.
            pulse_duration: pulse duration (s). Default 1.0.
            read_duration: read duration (s). Default 60.0.
            no_of_pulses: no of pulses (count). Default 5.
            compliance: compliance (mA). Default 1.0.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"pulse_voltage": pulse_voltage, "read_voltage": read_voltage, "trigger_period": trigger_period, "measure_delay_position": measure_delay_position, "pulse_duration": pulse_duration, "read_duration": read_duration, "no_of_pulses": no_of_pulses, "compliance": compliance}

        # make a pulse train
        
        pulse_train, pulse_train_string, trigger_count, measure_delay = self._make_voltage_pulses(pulse_voltage,read_voltage,trigger_period,measure_delay_position,pulse_duration,read_duration,no_of_pulses)

        #send pulse and get the output_sting
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #Add column for set_voltage
        output_table['set_voltage (V)']= pd.Series(pulse_train)
        
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        
        
        #self._savefile(output_table, df_parameters, cell_number,'AnalogPulse')
        self._savefile_1(output_table,df_parameters,cell_number,'VoltagePulse',"x_time")
        return output_table.to_dict(orient="records")

        #plot IV

    def Keysight_Paired_Pulse_Facilitation(
        self,
        cell_number: int,
        pulse_voltage: float = 0.6,
        read_voltage: float = 0.1,
        trigger_period: float = 0.05,
        pulse_duration: float = 0.1,
        delta_t: list|None = None,
        rest_period: float = 1.0,
        compliance: float = 1.0,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Paired-pulse facilitation (PPF): apply pulse pairs separated by each
        inter-pulse interval in ``delta_t`` and record the response.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            pulse_voltage: pulse voltage (V). Default 0.6.
            read_voltage: read voltage (V). Default 0.1.
            trigger_period: trigger period (s). Default 0.05.
            pulse_duration: pulse duration (s). Default 0.1.
            delta_t: delta t. Default None.
            rest_period: rest period (s). Default 1.0.
            compliance: compliance (mA). Default 1.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        if delta_t is None:
            delta_t = [0.6]
        parameters = {"pulse_voltage": pulse_voltage, "read_voltage": read_voltage, "trigger_period": trigger_period, "pulse_duration": pulse_duration, "delta_t": delta_t, "rest_period": rest_period, "compliance": compliance, "measure_delay_position": measure_delay_position}

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
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile(output_table, df_parameters, cell_number,'PPF')
        return output_table.to_dict(orient="records")

        #make a graph

    def Keysight_Spike_Duration_DP(
        self,
        cell_number: int,
        pulse_voltage: float = 2.0,
        read_voltage: float = 0.1,
        trigger_period: float = 0.02,
        pulse_durations: list|None = None,
        rest_period: float = 5.0,
        compliance: float = 100.0,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Spike-duration-dependent plasticity: sweep the write-pulse duration and
        record the resulting conductance change.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            pulse_voltage: pulse voltage (V). Default 2.0.
            read_voltage: read voltage (V). Default 0.1.
            trigger_period: trigger period (s). Default 0.02.
            pulse_durations: pulse durations (s). Default None.
            rest_period: rest period (s). Default 5.0.
            compliance: compliance (mA). Default 100.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        if pulse_durations is None:
            pulse_durations = [0.02, 0.04, 0.06, 0.08, 0.1, 0.12, 0.14, 0.16, 0.18, 0.2, 0.3, 0.4, 0.5, 1]
        parameters = {"pulse_voltage": pulse_voltage, "read_voltage": read_voltage, "trigger_period": trigger_period, "pulse_durations": pulse_durations, "rest_period": rest_period, "compliance": compliance, "measure_delay_position": measure_delay_position}

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
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile(output_table, df_parameters, cell_number,'SDDP')
        return output_table.to_dict(orient="records")

        #make a graph

    def Keysight_Spike_Voltage_DP(
        self,
        cell_number: int,
        pulse_voltages: list|None = None,
        read_voltage: float = 0.1,
        trigger_period: float = 0.02,
        pulse_duration: float = 0.5,
        rest_period: float = 5.0,
        compliance: float = 100.0,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Spike-voltage-dependent plasticity: sweep the write-pulse voltage and
        record the resulting conductance change.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            pulse_voltages: pulse voltages (V). Default None.
            read_voltage: read voltage (V). Default 0.1.
            trigger_period: trigger period (s). Default 0.02.
            pulse_duration: pulse duration (s). Default 0.5.
            rest_period: rest period (s). Default 5.0.
            compliance: compliance (mA). Default 100.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        if pulse_voltages is None:
            pulse_voltages = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
        parameters = {"pulse_voltages": pulse_voltages, "read_voltage": read_voltage, "trigger_period": trigger_period, "pulse_duration": pulse_duration, "rest_period": rest_period, "compliance": compliance, "measure_delay_position": measure_delay_position}

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
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile(output_table, df_parameters, cell_number,'SVDP')
        return output_table.to_dict(orient="records")

        #make a graph
    
    def Keysight_Digital_Endurance(
        self,
        cell_number: int,
        write_voltage: float = 2.0,
        erase_voltage: float = -2.0,
        read_voltage: float = 0.7,
        trigger_period: float = 0.05,
        write_duration: float = 10.0,
        erase_duration: float = 10.0,
        read_duration: float = 30.0,
        measure_delay_position: float = 0.5,
        compliance: float = 100.0,
    ) -> list[dict]:
        """Digital endurance / retention: repeatedly SET/RESET the device and track
        the read current over cycles. (Also exposed as ``Keysight_Digital_Retention``.)

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            write_voltage: write voltage (V). Default 2.0.
            erase_voltage: erase voltage (V). Default -2.0.
            read_voltage: read voltage (V). Default 0.7.
            trigger_period: trigger period (s). Default 0.05.
            write_duration: write duration (s). Default 10.0.
            erase_duration: erase duration (V). Default 10.0.
            read_duration: read duration (s). Default 30.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.
            compliance: compliance (mA). Default 100.0.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"write_voltage": write_voltage, "erase_voltage": erase_voltage, "read_voltage": read_voltage, "trigger_period": trigger_period, "write_duration": write_duration, "erase_duration": erase_duration, "read_duration": read_duration, "measure_delay_position": measure_delay_position, "compliance": compliance}

        #make a wave 
        full_pulse_set = np.array([], dtype=float)

        write_pulse = np.full(int(write_duration/trigger_period), write_voltage,dtype=float)
        read_pulse_1 = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
        erase_pulse = np.full(int(erase_duration/trigger_period), erase_voltage,dtype=float)
        read_pulse_2 = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)

        full_pulse_set = np.concatenate([write_pulse, read_pulse_1,erase_pulse,read_pulse_2])


        pulse_train = full_pulse_set
        
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile(output_table, df_parameters, cell_number,'RETENTION')
        return output_table.to_dict(orient="records")

        #make a graph

    def Keysight_Digital_Sweep(
        self,
        cell_number: int,
        set_v_max: float = 1.2,
        reset_v_min: float = -0.01,
        volt_step: float = 0.05,
        compliance: float = 100.0,
        trigger_period: float = 0.005,
        no_cycles: int = 10,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Digital I-V sweep: SET then RESET voltage sweeps over ``no_cycles``,
        recording the current per cycle.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            set_v_max: set v max (V). Default 1.2.
            reset_v_min: reset v min (V). Default -0.01.
            volt_step: volt step (V). Default 0.05.
            compliance: compliance (mA). Default 100.0.
            trigger_period: trigger period (s). Default 0.005.
            no_cycles: no cycles (count). Default 10.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"set_v_max": set_v_max, "reset_v_min": reset_v_min, "volt_step": volt_step, "compliance": compliance, "trigger_period": trigger_period, "no_cycles": no_cycles, "measure_delay_position": measure_delay_position}

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
            pulse_train = full_set_reset_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self._string_to_dataframe(output_string)
            #add Cycle column to the output_table
            output_table['Cycle']=i+1
            #add column set_Voltage (V)
            output_table['set_Voltage (V)']= pd.Series(full_set_reset_sweep)

            df_sweep_total = pd.concat([df_sweep_total,output_table], ignore_index = True)
        

        #adding parameters to the next rows, save it
        df_parameters = self._params_df(parameters)
        self._savefile(df_sweep_total,df_parameters,cell_number,'DigiSweep')
        return df_sweep_total.to_dict(orient="records")

        #plot IV

    def Keysight_Analog_Sweep(
        self,
        cell_number: int,
        set_v_max: float = 0.5,
        reset_v_min: float = -0.5,
        volt_step: float = 0.02,
        compliance: float = 100.0,
        trigger_period: float = 0.05,
        no_cycles: int = 20,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Analog I-V sweep: continuous SET/RESET voltage sweeps recording the analog
        current response.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            set_v_max: set v max (V). Default 0.5.
            reset_v_min: reset v min (V). Default -0.5.
            volt_step: volt step (V). Default 0.02.
            compliance: compliance (mA). Default 100.0.
            trigger_period: trigger period (s). Default 0.05.
            no_cycles: no cycles (count). Default 20.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"set_v_max": set_v_max, "reset_v_min": reset_v_min, "volt_step": volt_step, "compliance": compliance, "trigger_period": trigger_period, "no_cycles": no_cycles, "measure_delay_position": measure_delay_position}

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
            pulse_train = set_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self._string_to_dataframe(output_string)
            #add column set_Voltage (V)
            output_table['set_Voltage (V)']= pd.Series(set_sweep)
            #add Cycle column to the output_table
            output_table['Cycle']=i+1
            output_table['SetReset']='Set'

            df_set_sweep = pd.concat([df_set_sweep,output_table], ignore_index = True)
        
        #cycle for reset
        df_reset_sweep =pd.DataFrame()
        for i in range(int(no_cycles)):
            pulse_train = reset_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self._string_to_dataframe(output_string)
            #add column set_Voltage (V)
            output_table['set_Voltage (V)']= pd.Series(reset_sweep)
            #add Cycle column to the output_table
            output_table['Cycle']=i+1
            output_table['SetReset']='Reset'

            df_reset_sweep = pd.concat([df_reset_sweep,output_table], ignore_index = True)
        
        df_sweep_total = pd.concat([df_set_sweep,df_reset_sweep], ignore_index=True)

        #adding parameters to the next rows, save it
        df_parameters = self._params_df(parameters)
        self._savefile(df_sweep_total,df_parameters,cell_number,'AnaSweep')
        return df_sweep_total.to_dict(orient="records")

        #plot IV

    def Keysight_set_reset_sweep(
        self,
        cell_number: int,
        set_v_max: float = 1.0,
        reset_v_min: float = 0.0,
        volt_step: float = 0.01,
        no_cycles: int = 1,
        mode: str = 'set_only',
        compliance: float = 1.0,
        trigger_period: float = 0.1,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Configurable SET/RESET sweep honouring a ``mode`` parameter
        (``'loop'`` / ``'separate'`` / ``'set_only'`` / ``'reset_only'``).

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            set_v_max: set v max (V). Default 1.0.
            reset_v_min: reset v min (V). Default 0.0.
            volt_step: volt step (V). Default 0.01.
            no_cycles: no cycles (count). Default 1.
            mode: mode. Default 'set_only'.
            compliance: compliance (mA). Default 1.0.
            trigger_period: trigger period (s). Default 0.1.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"set_v_max": set_v_max, "reset_v_min": reset_v_min, "volt_step": volt_step, "no_cycles": no_cycles, "mode": mode, "compliance": compliance, "trigger_period": trigger_period, "measure_delay_position": measure_delay_position}
    
        
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
                output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
  
                # Convert response into DataFrame
                output_table = self._string_to_dataframe(output_string)
            
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
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(set_sweep, "Set", i)], ignore_index=True)
    
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(reset_sweep, "Reset", i)], ignore_index=True)

        elif mode == "separate":
            # all SET cycles first
            for i in range(int(no_cycles)):
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(set_sweep, "Set", i)], ignore_index=True)

            # then all RESET cycles
            for i in range(int(no_cycles)):
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(reset_sweep, "Reset", i)], ignore_index=True)

        elif mode == "set_only":
            for i in range(int(no_cycles)):
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(set_sweep, "Set", i)], ignore_index=True)

        elif mode == "reset_only":
            for i in range(int(no_cycles)):
                df_sweep_total = pd.concat([df_sweep_total, run_sweep(reset_sweep, "Reset", i)], ignore_index=True)

        #adding parameters to the next rows, save it
        df_parameters = self._params_df(parameters)
        self._savefile_1(df_sweep_total,df_parameters,cell_number,'set-reset_sweep',"IV")
        return df_sweep_total.to_dict(orient="records")
    
        #plot IV
    
        return df_sweep_total

    
    def Keysight_Substrate_R(
        self,
        cell_number: int,
        v_max: float = 1.0,
        v_min: float = -1.0,
        volt_step: float = 0.1,
        compliance: float = 10.0,
        trigger_period: float = 0.1,
        no_cycles: int = 1,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Measure substrate resistance via a small voltage sweep and a linear fit.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            v_max: v max (V). Default 1.0.
            v_min: v min (V). Default -1.0.
            volt_step: volt step (V). Default 0.1.
            compliance: compliance (mA). Default 10.0.
            trigger_period: trigger period (s). Default 0.1.
            no_cycles: no cycles (count). Default 1.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"v_max": v_max, "v_min": v_min, "volt_step": volt_step, "compliance": compliance, "trigger_period": trigger_period, "no_cycles": no_cycles, "measure_delay_position": measure_delay_position}

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
            pulse_train = full_forward_reverse_sweep
            pulse_train_string = ','.join(map(str,pulse_train))
            
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)

            #send pulse and get the output_string
            output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self._string_to_dataframe(output_string)
            
            #add Cycle column to the output_table
            output_table['Cycle']=i+1

            df_sweep_total = pd.concat([df_sweep_total,output_table], ignore_index = True)

        #adding parameters to the next rows, save it
        df_parameters = self._params_df(parameters)

        # Perform linear regression
        voltage = df_sweep_total['Voltage (V)']
        current = df_sweep_total['Current (A)']
        slope, intercept, r_value, p_value, std_err = linregress(voltage, current)

        # Save the slope as R
        R = 1/slope
        data = {'Resistance (Ohm)':[R]}
        df_R = pd.DataFrame(data)

        df_parameters_calcparam = pd.concat([df_parameters, df_R],axis = 1)
        self._savefile(df_sweep_total,df_parameters_calcparam,cell_number,'SubstR')
        return df_sweep_total.to_dict(orient="records")

 
        # Plot the data points

        # Plot the fitting line

        # Add labels and legend

        # Show the plot

    def Keysight_JV_PV(
        self,
        cell_number: int,
        v_min: float = -0.2,
        v_max: float = 1.0,
        volt_step: float = 0.01,
        compliance: float = 100.0,
        scan_rate: float = 500.0,
        cell_area: float = 0.09,
        irr: float = 1.0,
        no_cycles: int = 1,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Photovoltaic J-V measurement: forward and reverse voltage sweeps per cycle,
        converted to current density. Returns raw sweeps (PV-parameter
        extraction is done agent-side).

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            v_min: v min (V). Default -0.2.
            v_max: v max (V). Default 1.0.
            volt_step: volt step (V). Default 0.01.
            compliance: compliance (mA). Default 100.0.
            scan_rate: scan rate (mV/s). Default 500.0.
            cell_area: cell area (cm^2). Default 0.09.
            irr: irr. Default 1.0.
            no_cycles: no cycles (count). Default 1.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        Returns:
            list[dict]: one record per measured point, with keys ``Time (s)``,
            ``Voltage (V)``, ``Current (mA)``, ``Current Density (mA/cm2)``,
            ``Cycle`` and ``Direction`` (``"forward"``/``"reverse"``). Also saved
            to CSV as a side effect for the local lab workflow.
        """
        parameters = {"v_min": v_min, "v_max": v_max, "volt_step": volt_step, "compliance": compliance, "scan_rate": scan_rate, "cell_area": cell_area, "irr": irr, "no_cycles": no_cycles, "measure_delay_position": measure_delay_position}

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

        records = []
        for i in range(int(no_cycles)):
            
            #---FORWARD----
            forward_pulse_train_string = ','.join(map(str,forward_sweep))
            trigger_count = str(int(len(forward_sweep)))
            #send pulse and get the output_string
            output_string_forward = self._send_pulse_train_to_keysight(compliance, forward_pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table_forward = self._string_to_dataframe(output_string_forward)#this contain Voltage (V), Current (A), Time (s). need to change to below
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
            df_settings = self._params_df(parameters) 
            #concat setting to the data
            df_data_setting_fwd = pd.concat([df_JV_cooked_fwd,df_settings],axis=1)
            #----calculate the PV parameters--
            

            #----NEED TO ADD CALCULATION OF J-V parameters--
            file_name_tail = 'JV_PV_fwd_cycle'+str(i+1)
            self._savefile(df_data_setting_fwd, pd.DataFrame(), cell_number, file_name_tail)
            records += df_JV_cooked_fwd.assign(Direction="forward").to_dict(orient="records")


            #---REVERSE
            reverse_pulse_train_string = ','.join(map(str,reverse_sweep))
            trigger_count = str(int(len(reverse_sweep)))
            #send the pulse and get the output string
            output_string_reverse = self._send_pulse_train_to_keysight(compliance,reverse_pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change strings to dataframe
            output_table_reverse = self._string_to_dataframe(output_string_reverse)
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
            df_settings = self._params_df(parameters) 
            #concat setting to the data
            df_data_setting_rev = pd.concat([df_JV_cooked_rev,df_settings],axis=1)
            #----calculate the PV parameters--
            

            file_name_tail = 'JV_PV_rev_cycle'+str(i+1)
            self._savefile(df_data_setting_rev, pd.DataFrame(), cell_number, file_name_tail)
            records += df_JV_cooked_rev.assign(Direction="reverse").to_dict(orient="records")

        return records



            #----PLOT forward and reserve per cycle

            # Add solid lines at y = 0 and x = 0

            # Add labels and legend

            # Display the calculated_params and PV_param_values for df_PV_params_fwd

            # Display the calculated_params and PV_param_values for df_PV_params_rev

            # Show the plot

            # Show the plot

    def _send_pulse_train_to_keysight_light_pulse(self, compliance, pulse_train_string,trigger_count,trigger_period,measure_delay,
                                                 front_rest_duration, light_intensity, read_duration, light_on_duration, light_off_duration):
        
        """Run a voltage-list sweep while driving the Pico light.

        Same as :meth:`send_pulse_train_to_keysight` but additionally pulses the light
        during acquisition, for light-synchronized (optoelectronic) measurements.
        """
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
            self.smu.write(":OUTP ON") # Turns SMU output on.
            self.smu.write(":INIT") # Initiate measurement.
            ##create light pulses
            #turn off at front rest
            self.pico_instrument.light_off()
            time.sleep(front_rest_duration)
            #conduct on/off cycle
            # ********** light pulse **********
            self.pico_instrument.light_pulse(light_intensity=light_intensity, 
                                        read_duration=read_duration, 
                                        light_on_duration=light_on_duration, 
                                        light_off_duration=light_off_duration)

            # ********** READ BUFFER **********
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        return output_string
    
    def Keysight_Light_Pulse(
        self,
        cell_number: int,
        read_voltage: float = 0.5,
        trigger_period: float = 0.1,
        front_rest_duration: float = 2.0,
        read_duration: float = 10.0,
        compliance: float = 10.0,
        measure_delay_position: float = 0.5,
        light_intensity: float = 20.0,
        light_on_duration: float = 1.0,
        light_off_duration: float = 1.0,
    ) -> list[dict]:
        """Apply a voltage pulse train synchronized with Pico light pulses and record
        the optoelectronic response.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            read_voltage: read voltage (V). Default 0.5.
            trigger_period: trigger period (s). Default 0.1.
            front_rest_duration: front rest duration (s). Default 2.0.
            read_duration: read duration (s). Default 10.0.
            compliance: compliance (mA). Default 10.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.
            light_intensity: light intensity (%). Default 20.0.
            light_on_duration: light on duration (s). Default 1.0.
            light_off_duration: light off duration (s). Default 1.0.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"read_voltage": read_voltage, "trigger_period": trigger_period, "front_rest_duration": front_rest_duration, "read_duration": read_duration, "compliance": compliance, "measure_delay_position": measure_delay_position, "light_intensity": light_intensity, "light_on_duration": light_on_duration, "light_off_duration": light_off_duration}

        #make a wave 
        full_pulse_set = np.array([], dtype=float)

        front_rest = np.full(int(front_rest_duration/trigger_period), 0, dtype=float)
        read_pulse = np.full(int(read_duration/trigger_period), read_voltage, dtype=float)
            
        # Append to the full pulse set
        full_pulse_set = np.concatenate([front_rest, read_pulse])
            
        pulse_train = full_pulse_set
    
        pulse_train_string = ','.join(map(str,pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period*measure_delay_position)


        #send pulse and get the output_sting
        output_string = self._send_pulse_train_to_keysight_light_pulse(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay, front_rest_duration, light_intensity, read_duration, light_on_duration, light_off_duration)
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
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile(output_table, df_parameters, cell_number,'LightPulse')
        return output_table.to_dict(orient="records")

        #make a graph

    def Keysight_Voltage_Steady(
        self,
        cell_number: int,
        pulse_voltages: list|None = None,
        trigger_period: float = 0.1,
        voltage_duration: float = 20.0,
        compliance: float = 0.001,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Hold a list of steady voltages and record the current over time
        (constant-voltage stress / retention).

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            pulse_voltages: pulse voltages (V). Default None.
            trigger_period: trigger period (s). Default 0.1.
            voltage_duration: voltage duration (V). Default 20.0.
            compliance: compliance (mA). Default 0.001.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        if pulse_voltages is None:
            pulse_voltages = [0]
        parameters = {"pulse_voltages": pulse_voltages, "trigger_period": trigger_period, "voltage_duration": voltage_duration, "compliance": compliance, "measure_delay_position": measure_delay_position}

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
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #Add column for set_voltage
        output_table['set_voltage (V)']= pd.Series(pulse_train)
        
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile_1(output_table, df_parameters, cell_number,'VoltageSteady', "x_time")
        return output_table.to_dict(orient="records")
        
    def Keysight_Voltage_list(
        self,
        cell_number: int,
        csv_path: str = 'C:\\Users\\AMDM\\Desktop\\test.csv',
        trigger_period: float = 0.1,
        compliance: float = 0.001,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
            """Apply an arbitrary voltage list loaded from CSV and record the response.

            Args:
                cell_number: 1-based cell index (also labels the saved data files).
                csv_path: path to the voltage-list CSV (default is a Windows example path).
                trigger_period: trigger period (s). Default 0.1.
                compliance: compliance (mA). Default 0.001.
                measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

            Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
            contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
            ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
            saved data.

            

            Returns:
                list[dict]: one record per measured point (keys such as ``Time (s)``,
                ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
                as a side effect for the local lab workflow.
            """
            parameters = {"csv_path": csv_path, "trigger_period": trigger_period, "compliance": compliance, "measure_delay_position": measure_delay_position}
            
            voltage_df = pd.read_csv(csv_path)
            pulse_train = voltage_df["voltage_list"].to_numpy(dtype=float)

            
            
            pulse_train_string = ','.join(map(str,pulse_train))
            trigger_count = str(int(len(pulse_train)))
            measure_delay = str(trigger_period*measure_delay_position)


            #send pulse and get the output_sting
            output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
            #change the output_string to output_table (DF)
            output_table = self._string_to_dataframe(output_string)
            #Add column for set_voltage
            output_table['set_voltage (V)']= pd.Series(pulse_train)
            
            #save file, adding parameters to the next rows
            df_parameters = self._params_df(parameters)
            self._savefile_1(output_table, df_parameters, cell_number,'Voltage_list', "x_time")
            return output_table.to_dict(orient="records")
        

    def Keysight_Potent_Depress(
        self,
        cell_number: int,
        reset_period: float = 5.0,
        write_voltage: float = 1.94,
        erase_voltage: float = -1.94,
        pulse_duration: float = 0.16,
        pulse_no: int = 50,
        read_voltage: float = 1.0,
        read_duration: float = 0.16,
        cycle_write_erase: int = 10,
        trigger_period: float = 0.16,
        measure_delay_position: float = 0.5,
        compliance: float = 100.0,
    ) -> list[dict]:
        """Potentiation/depression: apply repeated write then erase pulse trains over
        cycles and record the conductance change (synaptic weight update).

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            reset_period: reset period (s). Default 5.0.
            write_voltage: write voltage (V). Default 1.94.
            erase_voltage: erase voltage (V). Default -1.94.
            pulse_duration: pulse duration (s). Default 0.16.
            pulse_no: pulse no (count). Default 50.
            read_voltage: read voltage (V). Default 1.0.
            read_duration: read duration (s). Default 0.16.
            cycle_write_erase: cycle write erase (V). Default 10.
            trigger_period: trigger period (s). Default 0.16.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.
            compliance: compliance (mA). Default 100.0.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"reset_period": reset_period, "write_voltage": write_voltage, "erase_voltage": erase_voltage, "pulse_duration": pulse_duration, "pulse_no": pulse_no, "read_voltage": read_voltage, "read_duration": read_duration, "cycle_write_erase": cycle_write_erase, "trigger_period": trigger_period, "measure_delay_position": measure_delay_position, "compliance": compliance}

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
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        
        # adding parameters to the next rows, save it
        df_parameters = self._params_df(parameters)
        #add cell_number to the df_parameters bottom row
        df_parameters = pd.concat([df_parameters, pd.DataFrame({'Parameter':'cell_number','Value':[cell_number]})], ignore_index=True)
        #combine output_table and parameters
        df_output_table_parameters = pd.concat([output_table, df_parameters], axis=1)


        #make a measurement overall graph
        try:
            pass
            #ax2.set_ylim(top = 4*voltage_level) 
        except:
            pass 
        
       
        #Fit the potentiation and depression, also save the file
        # Fitting/analysis is done agent-side; save the raw data here.
        self._savefile(df_output_table_parameters, pd.DataFrame(), cell_number, 'PotDep')
        return df_output_table_parameters.to_dict(orient="records")


    
    def Keysight_Potent_Depress_2(
        self,
        cell_number: int,
        reset_period: float = 0.0,
        write_voltage: float = 0.5,
        erase_voltage: float = -0.5,
        pulse_duration: float = 0.05,
        pulse_no: int = 50,
        read_voltage: float = 0.1,
        read_duration: float = 0.05,
        cycle_write_erase: int = 3,
        trigger_period: float = 0.05,
        measure_delay_position: float = 0.5,
        compliance: float = 100.0,
        t_pulse_to_read: float = 0.2,
        t_pulse_to_pulse: float = 0.5,
        wait_voltage: float = 0.0,
    ) -> list[dict]:
        """Potentiation/depression with explicit pulse-to-read and pulse-to-pulse
        timing control.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            reset_period: reset period (s). Default 0.0.
            write_voltage: write voltage (V). Default 0.5.
            erase_voltage: erase voltage (V). Default -0.5.
            pulse_duration: pulse duration (s). Default 0.05.
            pulse_no: pulse no (count). Default 50.
            read_voltage: read voltage (V). Default 0.1.
            read_duration: read duration (s). Default 0.05.
            cycle_write_erase: cycle write erase (V). Default 3.
            trigger_period: trigger period (s). Default 0.05.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.
            compliance: compliance (mA). Default 100.0.
            t_pulse_to_read: t pulse to read. Default 0.2.
            t_pulse_to_pulse: t pulse to pulse. Default 0.5.
            wait_voltage: wait voltage (V). Default 0.0.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"reset_period": reset_period, "write_voltage": write_voltage, "erase_voltage": erase_voltage, "pulse_duration": pulse_duration, "pulse_no": pulse_no, "read_voltage": read_voltage, "read_duration": read_duration, "cycle_write_erase": cycle_write_erase, "trigger_period": trigger_period, "measure_delay_position": measure_delay_position, "compliance": compliance, "t_pulse_to_read": t_pulse_to_read, "t_pulse_to_pulse": t_pulse_to_pulse, "wait_voltage": wait_voltage}

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
        output_string = self._send_pulse_train_to_keysight(compliance, pulse_train_string,trigger_count,trigger_period,measure_delay)
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        
        # adding parameters to the next rows, save it
        df_parameters = self._params_df(parameters)
        #add cell_number to the df_parameters bottom row
        df_parameters = pd.concat([df_parameters, pd.DataFrame({'Parameter':'cell_number','Value':[cell_number]})], ignore_index=True)
        #combine output_table and parameters
        df_output_table_parameters = pd.concat([output_table, df_parameters], axis=1)


        #make a measurement overall graph
        try:
            pass
            #ax2.set_ylim(top = 4*voltage_level) 
        except:
            pass 
        
        try:
            output_table_read = output_table[(output_table['Voltage (V)']>df_output_table_parameters.loc[5,'Value']-0.05) & (output_table['Voltage (V)']<df_output_table_parameters.loc[5,'Value']+0.05)]
           
        except:
            pass 
        
        #print('debug 1')
        #Fit the potentiation and depression, also save the file

        # Fitting/analysis is done agent-side; save the raw data here.
        self._savefile(df_output_table_parameters, pd.DataFrame(), cell_number, 'PotDep')
        return df_output_table_parameters.to_dict(orient="records")

    
        
    
    def Keysight_Voc_decay(
        self,
        cell_number: int,
        trigger_period: float = 0.1,
        light_intensity: float = 50.0,
        light_on_duration: float = 5.0,
        light_off_duration: float = 10.0,
        on_off_cycles: int = 1,
        source_current: float = 0.0,
        compliance_voltage: float = 1.0,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Open-circuit voltage (Voc) decay: cycle the light on/off and record the Voc
        transient.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            trigger_period: trigger period (s). Default 0.1.
            light_intensity: light intensity (%). Default 50.0.
            light_on_duration: light on duration (s). Default 5.0.
            light_off_duration: light off duration (s). Default 10.0.
            on_off_cycles: on off cycles (count). Default 1.
            source_current: source current. Default 0.0.
            compliance_voltage: compliance voltage (mA). Default 1.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"trigger_period": trigger_period, "light_intensity": light_intensity, "light_on_duration": light_on_duration, "light_off_duration": light_off_duration, "on_off_cycles": on_off_cycles, "source_current": source_current, "compliance_voltage": compliance_voltage, "measure_delay_position": measure_delay_position}

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
            self.pico_instrument.voc_light_pulse(light_intensity=light_intensity, 
                                        on_off_cycles=on_off_cycles, 
                                        light_on_duration=light_on_duration, 
                                        light_off_duration=light_off_duration)

            # ********** READ BUFFER **********
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            self.smu.write("*RST")
            #smu.close() # Close SMU object.
        
        except Exception as e:
            output_string = 'NONE'
             
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile(output_table, df_parameters, cell_number,'Voc_decay')
        return output_table.to_dict(orient="records")


        ## make a graph
        try:
            pass
            #ax2.set_ylim(top = 4*voltage_level) 
        except:
            pass 


    def Keysight_Voc_profile(
        self,
        cell_number: int,
        trigger_period: float = 0.1,
        light_intensity: float = 6.0,
        wait_time: float = 2.0,
        light_on_duration: float = 10.0,
        light_off_duration: float = 60.0,
        on_off_cycles: int = 1,
        read_time: float = 0.0,
        source_current: float = 0.0,
        compliance_voltage: float = 1.0,
        measure_delay_position: float = 0.5,
        NPLC_value: float = 0.01,
        volt_sense_range: float = 2.0,
        curr_sense_range: float = 1e-07,
    ) -> list[dict]:
        """Voc profile: idle, then light on/off cycles, then a read period, recording
        the Voc transient.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            trigger_period: trigger period (s). Default 0.1.
            light_intensity: light intensity (%). Default 6.0.
            wait_time: wait time (s). Default 2.0.
            light_on_duration: light on duration (s). Default 10.0.
            light_off_duration: light off duration (s). Default 60.0.
            on_off_cycles: on off cycles (count). Default 1.
            read_time: read time (s). Default 0.0.
            source_current: source current. Default 0.0.
            compliance_voltage: compliance voltage (mA). Default 1.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.
            NPLC_value: NPLC value. Default 0.01.
            volt_sense_range: volt sense range. Default 2.0.
            curr_sense_range: curr sense range. Default 1e-07.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"trigger_period": trigger_period, "light_intensity": light_intensity, "wait_time": wait_time, "light_on_duration": light_on_duration, "light_off_duration": light_off_duration, "on_off_cycles": on_off_cycles, "read_time": read_time, "source_current": source_current, "compliance_voltage": compliance_voltage, "measure_delay_position": measure_delay_position, "NPLC_value": NPLC_value, "volt_sense_range": volt_sense_range, "curr_sense_range": curr_sense_range}

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
            self.pico_instrument.voc_profile_light_pulse(light_intensity=light_intensity,
                                                idle_time=wait_time,
                                                on_off_cycles=on_off_cycles, 
                                                light_on_duration=light_on_duration, 
                                                light_off_duration=light_off_duration,
                                                read_period=read_time)

            # ********** READ BUFFER **********
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            self.smu.write("*RST")
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile_1(output_table, df_parameters, cell_number,'Voc_profile', "x_time")
        return output_table.to_dict(orient="records")

    def Keysight_Jsc_profile(
        self,
        cell_number: int,
        trigger_period: float = 0.1,
        light_intensity: float = 10.0,
        wait_time: float = 1.0,
        light_on_duration: float = 5.0,
        light_off_duration: float = 5.0,
        on_off_cycles: int = 1,
        read_time: float = 5.0,
        source_voltage: float = 0.0,
        compliance_current: float = 0.001,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Short-circuit current (Jsc) profile under light on/off cycling.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            trigger_period: trigger period (s). Default 0.1.
            light_intensity: light intensity (%). Default 10.0.
            wait_time: wait time (s). Default 1.0.
            light_on_duration: light on duration (s). Default 5.0.
            light_off_duration: light off duration (s). Default 5.0.
            on_off_cycles: on off cycles (count). Default 1.
            read_time: read time (s). Default 5.0.
            source_voltage: source voltage (V). Default 0.0.
            compliance_current: compliance current (mA). Default 0.001.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"trigger_period": trigger_period, "light_intensity": light_intensity, "wait_time": wait_time, "light_on_duration": light_on_duration, "light_off_duration": light_off_duration, "on_off_cycles": on_off_cycles, "read_time": read_time, "source_voltage": source_voltage, "compliance_current": compliance_current, "measure_delay_position": measure_delay_position}

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
            self.pico_instrument.voc_profile_light_pulse(light_intensity=light_intensity,
                                                idle_time=wait_time,
                                                on_off_cycles=on_off_cycles, 
                                                light_on_duration=light_on_duration, 
                                                light_off_duration=light_off_duration,
                                                read_period=read_time)

            # ********** READ BUFFER **********
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            self.smu.write("*RST")
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile_1(output_table, df_parameters, cell_number,'Jsc_profile', "x_time")
        return output_table.to_dict(orient="records")

   
    def Keysight_Voc_decay_indiv_soaking(
        self,
        cell_number: int,
        trigger_period: float = 0.1,
        light_intensity: float = 100.0,
        light_off_duration1: float = 1.0,
        light_on1: float = 1.0,
        light_off_duration2: float = 1.0,
        light_on2: float = 1.0,
        light_off_duration3: float = 1.0,
        light_on3: float = 1.0,
        light_off_duration4: float = 1.0,
        light_on4: float = 1.0,
        light_off_duration5: float = 1.0,
        light_on5: float = 1.0,
        on_off_cycles: int = 1,
        source_current: float = 0.0,
        compliance: float = 2.0,
        measure_delay_position: float = 0.5,
        soaking_time: float = 0.0,
    ) -> list[dict]:
        """Voc decay with multi-level light soaking before the on/off cycles.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            trigger_period: trigger period (s). Default 0.1.
            light_intensity: light intensity (%). Default 100.0.
            light_off_duration1: light off duration1 (s). Default 1.0.
            light_on1: light on1 (s). Default 1.0.
            light_off_duration2: light off duration2 (s). Default 1.0.
            light_on2: light on2 (s). Default 1.0.
            light_off_duration3: light off duration3 (s). Default 1.0.
            light_on3: light on3 (s). Default 1.0.
            light_off_duration4: light off duration4 (s). Default 1.0.
            light_on4: light on4 (s). Default 1.0.
            light_off_duration5: light off duration5 (s). Default 1.0.
            light_on5: light on5 (s). Default 1.0.
            on_off_cycles: on off cycles (count). Default 1.
            source_current: source current. Default 0.0.
            compliance: compliance (mA). Default 2.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.
            soaking_time: soaking time (s). Default 0.0.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"trigger_period": trigger_period, "light_intensity": light_intensity, "light_off_duration1": light_off_duration1, "light_on1": light_on1, "light_off_duration2": light_off_duration2, "light_on2": light_on2, "light_off_duration3": light_off_duration3, "light_on3": light_on3, "light_off_duration4": light_off_duration4, "light_on4": light_on4, "light_off_duration5": light_off_duration5, "light_on5": light_on5, "on_off_cycles": on_off_cycles, "source_current": source_current, "compliance": compliance, "measure_delay_position": measure_delay_position, "soaking_time": soaking_time}

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
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            #smu.close() # Close SMU object.
        
        except:
            output_string = 'NONE'

        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile(output_table, df_parameters, cell_number,'Voc_decay_indiv_soaking')
        return output_table.to_dict(orient="records")


        ## make a graph
        try:
            pass
            #ax2.set_ylim(top = 4*voltage_level) 
        except:
            pass 

    def Keysight_Voc_decay_ON_OFF_Variation(
        self,
        cell_number: int,
        trigger_period: float = 0.01,
        light_intensity: float = 10.0,
        wait_time: float = 0.5,
        light_off_duration1: float = 0.1,
        light_on1: float = 2.0,
        light_off_duration2: float = 5.0,
        light_on2: float = 0.0,
        light_off_duration3: float = 0.0,
        light_on3: float = 0.0,
        light_off_duration4: float = 0.0,
        light_on4: float = 0.0,
        light_off_duration5: float = 0.0,
        light_on5: float = 0.0,
        on_off_cycles: int = 2,
        source_current: float = 0.0,
        voltage_compliance: float = 1.0,
        measure_delay_position: float = 0.5,
    ) -> list[dict]:
        """Voc decay with variable light on/off durations per cycle.

        Args:
            cell_number: 1-based cell index (also labels the saved data files).
            trigger_period: trigger period (s). Default 0.01.
            light_intensity: light intensity (%). Default 10.0.
            wait_time: wait time (s). Default 0.5.
            light_off_duration1: light off duration1 (s). Default 0.1.
            light_on1: light on1 (s). Default 2.0.
            light_off_duration2: light off duration2 (s). Default 5.0.
            light_on2: light on2 (s). Default 0.0.
            light_off_duration3: light off duration3 (s). Default 0.0.
            light_on3: light on3 (s). Default 0.0.
            light_off_duration4: light off duration4 (s). Default 0.0.
            light_on4: light on4 (s). Default 0.0.
            light_off_duration5: light off duration5 (s). Default 0.0.
            light_on5: light on5 (s). Default 0.0.
            on_off_cycles: on off cycles (count). Default 2.
            source_current: source current. Default 0.0.
            voltage_compliance: voltage compliance (mA). Default 1.0.
            measure_delay_position: measure delay position (fraction of trigger period). Default 0.5.

        Sequencing (PUDA): runs on the ``probot-keysight-pico`` machine. Position and
        contact the cell first via the ``stage-probot`` machine (``move_to_cell`` then
        ``probe``), and ``unprobe`` once this returns; ``cell_number`` only labels the
        saved data.

        

        Returns:
            list[dict]: one record per measured point (keys such as ``Time (s)``,
            ``Voltage (V)``, ``Current (A)``). The raw data is also written to CSV
            as a side effect for the local lab workflow.
        """
        parameters = {"trigger_period": trigger_period, "light_intensity": light_intensity, "wait_time": wait_time, "light_off_duration1": light_off_duration1, "light_on1": light_on1, "light_off_duration2": light_off_duration2, "light_on2": light_on2, "light_off_duration3": light_off_duration3, "light_on3": light_on3, "light_off_duration4": light_off_duration4, "light_on4": light_on4, "light_off_duration5": light_off_duration5, "light_on5": light_on5, "on_off_cycles": on_off_cycles, "source_current": source_current, "voltage_compliance": voltage_compliance, "measure_delay_position": measure_delay_position}
    
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
            return None
        
        
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
            self.pico_instrument.light_pulse_ON_OFF_variation(light_intensity=light_intensity,on_off_cycles=on_off_cycles,idle_time=wait_time,
                                                            light_on1=light_on1,light_off_duration1=light_off_duration1, 
                                                            light_on2=light_on2,light_off_duration2=light_off_duration2,
                                                            light_on3=light_on3,light_off_duration3=light_off_duration3,
                                                            light_on4=light_on4,light_off_duration4=light_off_duration4,
                                                            light_on5=light_on5,light_off_duration5=light_off_duration5)
    
            # ********** READ BUFFER **********
            self.smu.query("*OPC?") # Checks and waits for the SMU to complete the measurement.
            self.smu.write(":OUTP OFF") # Turns SMU output off.
            #output_string = self.smu.query(":TRAC:DATA? 1,10")
            output_string = self.smu.query(":FETC:ARR?") # Read the buffer as a comma separated string.
            #self.smu.close() # Close SMU object.
            
        except Exception as e:
            output_string = 'NONE'
    
        #change the output_string to output_table (DF)
        output_table = self._string_to_dataframe(output_string)
        #save file, adding parameters to the next rows
        df_parameters = self._params_df(parameters)
        self._savefile_1(output_table, df_parameters, cell_number,'Light_ON_OFF_Variation',"x_time")
        return output_table.to_dict(orient="records")
    
              
    def Keysight_Time_Gap(
        self,
        value: int = None,
        sleep: float = 60,
    ) -> dict:
        """Idle / wait step used to insert delays into a measurement queue.

        Args:
            value: unused; present so the queue can call it with the common
                ``(cell_number)`` signature.
            sleep: seconds to wait. Default 60.

        Returns:
            dict: ``{"slept_s": <seconds>}``.
        """
        time.sleep(sleep)
        return {"slept_s": sleep}


# Alias: measurement_list() advertises ``Keysight_Digital_Retention`` while the
# implementation is ``Keysight_Digital_Endurance`` (reads the Retention CSV).
KeysightPicoProbotMachine.Keysight_Digital_Retention = KeysightPicoProbotMachine.Keysight_Digital_Endurance
