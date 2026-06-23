"""Keysight SMU + Pico light machine driver for the ``probot-smu-keysight`` edge service.

Self-contained module combining the VISA transport, the ~22 measurement routines, and
the PUDA machine interface.  Import and instantiate :class:`SMUKeysightDriver` directly
from this file — no ``probot_drivers`` sub-module needed for the driver itself.

The Keysight SMU and the Pico G2V light source are **co-located** here because several
measurements drive the light inline with sub-second timing.  The stage runs as a
separate edge.

Public interface (PUDA primitives / AI-callable)
------------------------------------------------
* Lifecycle  – :meth:`startup`, :meth:`shutdown`, :meth:`home`, :meth:`get_position`
* Introspect – :meth:`identify`, :meth:`measurement_list`
* Light      – :meth:`light_on`, :meth:`light_off`
* Keysight_* – all ~22 measurement primitives (see :attr:`MEASUREMENT_NAMES`)

All measurement primitives accept their settings as typed keyword arguments with
sensible defaults, so PUDA (or an AI recipe) can call them with explicit overrides
without touching any config file.  The raw DataFrame is returned as a dict so PUDA
records it as structured data.

Private helpers (prefixed ``_``)
---------------------------------
SCPI helpers and data-conversion methods are private (``_``-prefixed) and not
reflected as PUDA primitives.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from scipy.stats import linregress

from probot_drivers.analysis import pv_param as PV_calc
from probot_drivers import PicoProbot

logger = logging.getLogger(__name__)


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


class SMUKeysightDriver:
    """Keysight SMU + Pico G2V light driver (probot-smu-keysight edge).

    Combines the VISA transport, measurement routines, and PUDA machine interface
    into a single class.  The constructor stores configuration only; call
    :meth:`startup` to open connections.

    Public methods are reflected as PUDA primitives.  Internal helpers are
    ``_``-prefixed and not visible to the orchestrator or AI recipes.
    """

    instrument_family = "probot_smu_keysight"

    def __init__(
        self,
        smu_address: str | None = None,
        smu_device_no: int = 0,
        pico_ip: str | None = None,
        pico_id: str | None = None,
    ) -> None:
        """Store connection config (does not connect).

        Args:
            smu_address: VISA resource string (e.g. ``"USB0::0x0957::...::INSTR"``).
                When ``None``, :meth:`startup` falls back to ``smu_device_no``.
            smu_device_no: index into ``list_resources()`` used when ``smu_address``
                is not given.
            pico_ip: IP address of the Pico G2V controller (link-local, e.g.
                ``"169.254.x.x"``).
            pico_id: Pico device serial id.
        """
        self._smu_address = smu_address
        self._smu_device_no = smu_device_no
        self._rm = None
        self.smu = None

        self._light = PicoProbot(ip=pico_ip, device_id=pico_id)
        self.pico_instrument = self._light

        logger.info(
            "SMUKeysightDriver initialised (smu_address=%s, pico_ip=%s)",
            smu_address, pico_ip,
        )

    # ══════════════════════════════════════════════════════════════════════
    # PUBLIC INTERFACE — PUDA primitives / AI-callable
    # ══════════════════════════════════════════════════════════════════════

    def startup(self) -> bool:
        """Connect to the Keysight SMU and the Pico light.

        Returns:
            True when both connections succeed.
        """
        logger.info("Starting up SMU + Pico light")
        ok_smu = self._smu_startup()
        ok_light = self._light.startup()
        if ok_smu and ok_light:
            logger.info("SMU + light startup complete")
        else:
            logger.warning("Startup partial (smu=%s, light=%s)", ok_smu, ok_light)
        return ok_smu and ok_light

    def shutdown(self) -> bool:
        """Turn the SMU output off, close the VISA session, and turn the light off.

        Returns:
            True (best effort).
        """
        return all([self._smu_shutdown(), self._light.shutdown()])

    def home(self) -> bool:
        """No homing for the SMU/light machine.

        Returns:
            True (always; required PUDA lifecycle method).
        """
        return True

    def get_position(self) -> dict:
        """The SMU/light machine has no spatial position.

        Returns:
            Empty dict.
        """
        return {}

    def identify(self) -> str:
        """Return the Keysight SMU ``*IDN?`` string.

        Returns:
            str: instrument identification string.
        """
        return self.smu.query("*IDN?")

    def measurement_list(self) -> list:
        """Return the ordered list of available Keysight measurement primitives.

        Returns:
            list[str]: measurement method names in canonical order.
        """
        return list(MEASUREMENT_NAMES)

    def light_on(self) -> None:
        """Turn the Pico G2V light to full intensity (100 %)."""
        self._light.light_on()

    def light_off(self) -> None:
        """Turn the Pico G2V light off (0 % intensity)."""
        self._light.light_off()

    # ══════════════════════════════════════════════════════════════════════
    # PRIVATE — VISA transport
    # ══════════════════════════════════════════════════════════════════════

    def _smu_startup(self) -> bool:
        """Open the VISA session to the Keysight SMU."""
        if self.smu is not None:
            return True
        try:
            import pyvisa
            self._rm = pyvisa.ResourceManager()
            address = self._smu_address
            if address is None:
                resources = list(self._rm.list_resources())
                if not resources:
                    raise RuntimeError("No VISA resources found")
                address = resources[self._smu_device_no]
            self._smu_address = address
            self.smu = self._rm.open_resource(address)
            logger.info("Connected to Keysight SMU at %s", address)
            return True
        except Exception:
            logger.exception("Failed to connect to Keysight SMU (address=%s)", self._smu_address)
            self.smu = None
            return False

    def _smu_shutdown(self) -> bool:
        """Turn the SMU output off (best effort) and close the VISA session."""
        try:
            if self.smu is not None:
                try:
                    self.smu.write(":OUTP OFF")
                except Exception:
                    logger.exception("Error turning SMU output off during shutdown")
                self.smu.close()
        finally:
            self.smu = None
        return True

    # ══════════════════════════════════════════════════════════════════════
    # PRIVATE — SCPI / data helpers
    # ══════════════════════════════════════════════════════════════════════

    def _htpd(self):
        """Lazily import the HT_PotDep analysis module."""
        from probot_drivers.analysis import ht_potdep as HTPD
        return HTPD

    def _make_voltage_pulses(self, pulse_voltage, read_voltage, trigger_period,
                             measure_delay_position, pulse_duration, read_duration,
                             no_of_pulses):
        """Build a repeated read+write voltage pulse train.

        Returns:
            tuple: ``(pulse_train, pulse_train_string, trigger_count, measure_delay)``
        """
        pulse_single = np.empty(int((read_duration + pulse_duration) / trigger_period), dtype=float)
        for i in range(int(read_duration / trigger_period)):
            pulse_single[i] = read_voltage
        for i in range(int(read_duration / trigger_period),
                       int((read_duration + pulse_duration) / trigger_period)):
            pulse_single[i] = pulse_voltage

        pulse_train = np.array(pulse_single)
        for i in range(1, no_of_pulses):
            pulse_train = np.append(pulse_train, pulse_single)

        pulse_train_string = ','.join(map(str, pulse_train))
        trigger_count = str(int(no_of_pulses * (read_duration + pulse_duration) / trigger_period))
        measure_delay = str(trigger_period * measure_delay_position)
        return pulse_train, pulse_train_string, trigger_count, measure_delay

    def _send_pulse_train_to_keysight(self, compliance, pulse_train_string,
                                      trigger_count, trigger_period, measure_delay):
        """Run one voltage-list sweep on the SMU and return the raw buffer string.

        Returns:
            str: comma-separated ``time,voltage,current`` triples, or ``'NONE'`` on error.
        """
        logger.debug('SENDING COMMANDS TO INSTRUMENT')
        try:
            self.smu.timeout = 10000000
            self.smu.write_termination = '\n'
            self.smu.read_termination = '\n'
            self.smu.write("*RST")
            self.smu.write("*CLS")
            self.smu.write(":TRAC:CLE")
            self.smu.write(":TRAC:FEED SENS")
            self.smu.write(":TRAC:FEED:CONT NEXT")
            self.smu.write(":TRAC:TST:FORM ABS")
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")
            self.smu.write(":SOUR:FUNC:MODE VOLT")
            self.smu.write(":SOUR:VOLT:MODE LIST")
            self.smu.write(":SENS:FUNCtion 'CURR', 'VOLT'")
            self.smu.write(f":SENS:CURR:PROT {str(compliance / 1000)}")
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON")
            self.smu.write(f":LIST:VOLT {pulse_train_string}")
            self.smu.write(f":TRIG:COUN {trigger_count}")
            self.smu.write(":TRIG:SOUR TIM")
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")
            self.smu.write(":OUTP ON")
            self.smu.write(":INIT")
            self.smu.query("*OPC?")
            self.smu.write(":OUTP OFF")
            output_string = self.smu.query(":FETC:ARR?")
            logger.debug("%s", self.smu.query("SYST:ERR?"))
        except Exception:
            output_string = 'NONE'
        return output_string

    def _send_pulse_train_to_keysight_light_pulse(self, compliance, pulse_train_string,
                                                   trigger_count, trigger_period,
                                                   measure_delay, front_rest_duration,
                                                   light_intensity, read_duration,
                                                   light_on_duration, light_off_duration):
        """Run a voltage-list sweep while driving the Pico light.

        Returns:
            str: comma-separated ``time,voltage,current`` triples, or ``'NONE'`` on error.
        """
        logger.debug('SENDING COMMANDS TO INSTRUMENT')
        try:
            self.smu.timeout = 10000000
            self.smu.write_termination = '\n'
            self.smu.read_termination = '\n'
            self.smu.write('*CLS')
            self.smu.write('*RST')
            self.smu.write(":TRAC:FEED SENS")
            self.smu.write(":TRAC:FEED:CONT NEXT")
            self.smu.write(":TRAC:TST:FORM ABS")
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")
            self.smu.write(":SOUR:FUNC:MODE VOLT")
            self.smu.write(":SOUR:VOLT:MODE LIST")
            self.smu.write(f":SENS:CURR:PROT {str(compliance / 1000)}")
            self.smu.write(f":LIST:VOLT {pulse_train_string}")
            self.smu.write(f":TRIG:COUN {trigger_count}")
            self.smu.write(":TRIG:SOUR TIMER")
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")
            self.smu.write(":OUTP ON")
            self.smu.write(":INIT")
            self.pico_instrument.light_off()
            time.sleep(front_rest_duration)
            logger.debug('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.light_pulse(
                light_intensity=light_intensity,
                read_duration=read_duration,
                light_on_duration=light_on_duration,
                light_off_duration=light_off_duration,
            )
            logger.debug('READING BUFFER')
            self.smu.query("*OPC?")
            self.smu.write(":OUTP OFF")
            output_string = self.smu.query(":FETC:ARR?")
            logger.debug("%s", self.smu.query("SYST:ERR?"))
        except Exception:
            output_string = 'NONE'
        return output_string

    def _string_to_dataframe(self, output_string):
        """Parse the SMU buffer string into a DataFrame.

        Args:
            output_string: comma-separated ``time,voltage,current`` values.

        Returns:
            pandas.DataFrame with columns ``['Voltage (V)', 'Current (A)', 'Time (s)']``,
            or an empty DataFrame on parse failure.
        """
        try:
            output_array = np.array(output_string.split(','), float)
            split_columns = np.reshape(output_array, (int(output_array.size / 3), 3))
            return pd.DataFrame(split_columns, columns=['Voltage (V)', 'Current (A)', 'Time (s)'])
        except Exception:
            logger.debug('No output_string to convert to df')
            return pd.DataFrame(columns=['Voltage (V)', 'Current (A)', 'Time (s)'])

    def _df_to_dict(self, df: pd.DataFrame) -> Dict[str, list]:
        """Serialise a DataFrame to a plain ``{column: [values]}`` dict for PUDA."""
        try:
            return df.to_dict(orient='list')
        except Exception:
            return {}

    def _Pot_Dep_Calculation(self, df: pd.DataFrame, cell_number: int,
                             reset_period: float, trigger_period: float,
                             pulse_no: int) -> pd.DataFrame | None:
        """Post-process a potentiation/depression run via the HT_PotDep analysis module.

        Args:
            df: raw measurement DataFrame (columns: Voltage (V), Current (A), Time (s)).
            cell_number: 1-based cell index.
            reset_period: seconds of reset pulse at the start of the waveform.
            trigger_period: seconds per sample point.
            pulse_no: number of write/erase pulses per cycle.

        Returns:
            DataFrame of fitted potentiation/depression parameters, or None on failure.
        """
        try:
            df = df.copy()
            df['Conductance (S)'] = df['Current (A)'] / df['Voltage (V)']
            df['Conductance (S)'] = df['Conductance (S)'].where(
                abs(df['Voltage (V)']) > 0.001, 0)
            rest_length = int(reset_period / trigger_period)
            pulses_length = len(df) - rest_length
            pulse_length = pulses_length / pulse_no
            df['Cycle'] = ((df.index - rest_length) // pulse_length) + 1
            return self._htpd().main(df, cell_number)
        except Exception:
            logger.exception('Pot_Dep_Calculation failed')
            return None

    # ══════════════════════════════════════════════════════════════════════
    # MEASUREMENTS — all params are typed kwargs; data is returned as dict
    # ══════════════════════════════════════════════════════════════════════

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
    ) -> Dict[str, Any]:
        """Apply a train of identical read/write voltage pulses and record the current.

        Args:
            cell_number: 1-based cell index.
            pulse_voltage: write pulse amplitude in V. Default 0.3.
            read_voltage: read pulse amplitude in V. Default 0.2.
            trigger_period: seconds per sample point. Default 0.1.
            measure_delay_position: fractional delay within trigger period. Default 0.5.
            pulse_duration: write pulse width in s. Default 1.0.
            read_duration: read pulse width in s. Default 60.0.
            no_of_pulses: number of write/read pairs. Default 5.
            compliance: current compliance in mA. Default 1.0.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``,
            ``set_voltage`` (all as lists).
        """
        logger.info('Measure analog sweep for cell %s', cell_number)
        pulse_train, pulse_train_string, trigger_count, measure_delay = self._make_voltage_pulses(
            pulse_voltage, read_voltage, trigger_period, measure_delay_position,
            pulse_duration, read_duration, no_of_pulses)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
            "set_voltage": pulse_train.tolist(),
        }

    def Keysight_Paired_Pulse_Facilitation(
        self,
        cell_number: int,
        pulse_voltage: float = 0.6,
        read_voltage: float = 0.1,
        trigger_period: float = 0.05,
        pulse_duration: float = 0.1,
        delta_t: List[float] = None,
        rest_period: float = 1.0,
        compliance: float = 1.0,
        measure_delay_position: float = 0.5,
    ) -> Dict[str, Any]:
        """Paired-pulse facilitation (PPF): apply pulse pairs separated by each
        inter-pulse interval in ``delta_t`` and record the response.

        Args:
            cell_number: 1-based cell index.
            pulse_voltage: write pulse amplitude in V. Default 0.6.
            read_voltage: read pulse amplitude in V. Default 0.1.
            trigger_period: seconds per sample point. Default 0.05.
            pulse_duration: pulse width in s. Default 0.1.
            delta_t: list of inter-pulse intervals in s. Default [0.6].
            rest_period: rest period between pairs in s. Default 1.0.
            compliance: current compliance in mA. Default 1.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        if delta_t is None:
            delta_t = [0.6]
        logger.info('Measure PPF for cell %s', cell_number)
        full_pulse_set = np.array([], dtype=float)
        for t in delta_t:
            front_rest = np.full(int((rest_period / 2) / trigger_period), 0, dtype=float)
            first_pulse = np.full(int(pulse_duration / trigger_period), pulse_voltage, dtype=float)
            delta_time1 = np.full(int(t / trigger_period), read_voltage, dtype=float)
            second_pulse = np.full(int(pulse_duration / trigger_period), pulse_voltage, dtype=float)
            delta_time2 = np.full(int(t / trigger_period), read_voltage, dtype=float)
            rear_rest = np.full(int((rest_period / 2) / trigger_period), 0, dtype=float)
            pulse_set = np.concatenate([front_rest, first_pulse, delta_time1,
                                        second_pulse, delta_time2, rear_rest])
            full_pulse_set = np.concatenate([full_pulse_set, pulse_set])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

    def Keysight_Spike_Duration_DP(
        self,
        cell_number: int,
        pulse_voltage: float = 2.0,
        read_voltage: float = 0.1,
        trigger_period: float = 0.02,
        pulse_durations: List[float] = None,
        rest_period: float = 5.0,
        compliance: float = 100.0,
        measure_delay_position: float = 0.5,
    ) -> Dict[str, Any]:
        """Spike-duration-dependent plasticity: sweep the write-pulse duration.

        Args:
            cell_number: 1-based cell index.
            pulse_voltage: write pulse amplitude in V. Default 2.0.
            read_voltage: read voltage in V. Default 0.1.
            trigger_period: seconds per sample point. Default 0.02.
            pulse_durations: list of write-pulse widths in s.
                Default [0.02, 0.04, ..., 1.0].
            rest_period: rest period between pulses in s. Default 5.0.
            compliance: current compliance in mA. Default 100.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        if pulse_durations is None:
            pulse_durations = [0.02, 0.04, 0.06, 0.08, 0.1, 0.12, 0.14, 0.16,
                               0.18, 0.2, 0.3, 0.4, 0.5, 1.0]
        logger.info('Measure SDDP for cell %s', cell_number)
        full_pulse_set = np.array([], dtype=float)
        for pd_ in pulse_durations:
            front_rest = np.full(int((rest_period / 2) / trigger_period), read_voltage, dtype=float)
            single_pulse = np.full(int(pd_ / trigger_period), pulse_voltage, dtype=float)
            rear_rest = np.full(int((rest_period / 2) / trigger_period), read_voltage, dtype=float)
            full_pulse_set = np.concatenate([full_pulse_set, front_rest, single_pulse, rear_rest])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

    def Keysight_Spike_Voltage_DP(
        self,
        cell_number: int,
        pulse_voltages: List[float] = None,
        read_voltage: float = 0.1,
        trigger_period: float = 0.02,
        pulse_duration: float = 0.5,
        rest_period: float = 5.0,
        compliance: float = 100.0,
        measure_delay_position: float = 0.5,
    ) -> Dict[str, Any]:
        """Spike-voltage-dependent plasticity: sweep the write-pulse voltage.

        Args:
            cell_number: 1-based cell index.
            pulse_voltages: list of write-pulse amplitudes in V.
                Default [0.1, 0.2, ..., 2.0].
            read_voltage: read voltage in V. Default 0.1.
            trigger_period: seconds per sample point. Default 0.02.
            pulse_duration: pulse width in s. Default 0.5.
            rest_period: rest period between pulses in s. Default 5.0.
            compliance: current compliance in mA. Default 100.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        if pulse_voltages is None:
            pulse_voltages = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9,
                              1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
        logger.info('Measure SVDP for cell %s', cell_number)
        full_pulse_set = np.array([], dtype=float)
        for pv in pulse_voltages:
            front_rest = np.full(int((rest_period / 2) / trigger_period), read_voltage, dtype=float)
            single_pulse = np.full(int(pulse_duration / trigger_period), pv, dtype=float)
            rear_rest = np.full(int((rest_period / 2) / trigger_period), read_voltage, dtype=float)
            full_pulse_set = np.concatenate([full_pulse_set, front_rest, single_pulse, rear_rest])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

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
    ) -> Dict[str, Any]:
        """Digital endurance / retention: repeatedly SET/RESET the device.
        Also exposed as ``Keysight_Digital_Retention``.

        Args:
            cell_number: 1-based cell index.
            write_voltage: SET pulse amplitude in V. Default 2.0.
            erase_voltage: RESET pulse amplitude in V. Default -2.0.
            read_voltage: read pulse amplitude in V. Default 0.7.
            trigger_period: seconds per sample point. Default 0.05.
            write_duration: SET pulse width in s. Default 10.0.
            erase_duration: RESET pulse width in s. Default 10.0.
            read_duration: read pulse width in s. Default 30.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.
            compliance: current compliance in mA. Default 100.0.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        logger.info('Measure digital switching retention for cell %s', cell_number)
        write_pulse = np.full(int(write_duration / trigger_period), write_voltage, dtype=float)
        read_pulse_1 = np.full(int(read_duration / trigger_period), read_voltage, dtype=float)
        erase_pulse = np.full(int(erase_duration / trigger_period), erase_voltage, dtype=float)
        read_pulse_2 = np.full(int(read_duration / trigger_period), read_voltage, dtype=float)
        full_pulse_set = np.concatenate([write_pulse, read_pulse_1, erase_pulse, read_pulse_2])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

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
    ) -> Dict[str, Any]:
        """Digital I-V sweep: SET then RESET voltage sweeps over ``no_cycles``.

        Args:
            cell_number: 1-based cell index.
            set_v_max: maximum SET voltage in V. Default 1.2.
            reset_v_min: minimum RESET voltage in V. Default -0.01.
            volt_step: voltage step in V. Default 0.05.
            compliance: current compliance in mA. Default 100.0.
            trigger_period: seconds per sample point. Default 0.005.
            no_cycles: number of SET/RESET cycles. Default 10.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``cycles`` (list of per-cycle dicts with
            ``cycle``, ``time``, ``voltage``, ``current``, ``set_voltage``).
        """
        logger.info('Measure Digital J-V sweep for cell %s', cell_number)
        set_positive_sweep = [np.round(x, 2) for x in np.arange(0, float(set_v_max) + float(volt_step), float(volt_step))]
        set_negative_sweep = [np.round(x, 2) for x in np.arange(float(set_v_max) - float(volt_step), 0 - float(volt_step), -float(volt_step))]
        reset_negative_sweep = [np.round(x, 2) for x in np.arange(0, float(reset_v_min) - float(volt_step), -float(volt_step))]
        reset_positive_sweep = [np.round(x, 2) for x in np.arange(float(reset_v_min), 0 + float(volt_step), float(volt_step))]
        full_set_reset_sweep = np.concatenate([set_positive_sweep + set_negative_sweep,
                                               reset_negative_sweep + reset_positive_sweep])
        cycles = []
        for i in range(int(no_cycles)):
            logger.info('Measuring sweep cycle: %s', i + 1)
            pulse_train_string = ','.join(map(str, full_set_reset_sweep))
            trigger_count = str(int(len(full_set_reset_sweep)))
            measure_delay = str(trigger_period * measure_delay_position)
            output_string = self._send_pulse_train_to_keysight(
                compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
            df = self._string_to_dataframe(output_string)
            cycles.append({
                "cycle": i + 1,
                "time": df['Time (s)'].tolist(),
                "voltage": df['Voltage (V)'].tolist(),
                "current": df['Current (A)'].tolist(),
                "set_voltage": full_set_reset_sweep.tolist(),
            })
        return {"cell_number": cell_number, "cycles": cycles}

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
    ) -> Dict[str, Any]:
        """Analog I-V sweep: continuous SET/RESET voltage sweeps.

        Args:
            cell_number: 1-based cell index.
            set_v_max: maximum SET voltage in V. Default 0.5.
            reset_v_min: minimum RESET voltage in V. Default -0.5.
            volt_step: voltage step in V. Default 0.02.
            compliance: current compliance in mA. Default 100.0.
            trigger_period: seconds per sample point. Default 0.05.
            no_cycles: number of SET/RESET cycles each. Default 20.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``set_cycles``, ``reset_cycles`` (each a
            list of per-cycle dicts with ``cycle``, ``time``, ``voltage``, ``current``,
            ``set_voltage``).
        """
        logger.info('Measure Analog I-V sweep for cell %s', cell_number)
        set_positive_sweep = [np.round(x, 2) for x in np.arange(0, float(set_v_max) + float(volt_step), float(volt_step))]
        set_negative_sweep = [np.round(x, 2) for x in np.arange(float(set_v_max) - float(volt_step), 0 - float(volt_step), -float(volt_step))]
        reset_negative_sweep = [np.round(x, 2) for x in np.arange(0, float(reset_v_min) - float(volt_step), -float(volt_step))]
        reset_positive_sweep = [np.round(x, 2) for x in np.arange(float(reset_v_min) + float(volt_step), 0 + float(volt_step), float(volt_step))]
        set_sweep = set_positive_sweep + set_negative_sweep
        reset_sweep = reset_negative_sweep + reset_positive_sweep

        def run_sweep_cycles(sweep, label):
            results = []
            for i in range(int(no_cycles)):
                logger.info('Measuring %s sweep cycle: %s', label, i + 1)
                pulse_train_string = ','.join(map(str, sweep))
                trigger_count = str(int(len(sweep)))
                measure_delay = str(trigger_period * measure_delay_position)
                output_string = self._send_pulse_train_to_keysight(
                    compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
                df = self._string_to_dataframe(output_string)
                results.append({
                    "cycle": i + 1,
                    "time": df['Time (s)'].tolist(),
                    "voltage": df['Voltage (V)'].tolist(),
                    "current": df['Current (A)'].tolist(),
                    "set_voltage": list(sweep),
                })
            return results

        return {
            "cell_number": cell_number,
            "set_cycles": run_sweep_cycles(set_sweep, "SET"),
            "reset_cycles": run_sweep_cycles(reset_sweep, "RESET"),
        }

    def Keysight_set_reset_sweep(
        self,
        cell_number: int,
        set_v_max: float = 1.0,
        reset_v_min: float = 0.0,
        volt_step: float = 0.01,
        no_cycles: int = 1,
        mode: str = "set_only",
        compliance: float = 1.0,
        trigger_period: float = 0.1,
        measure_delay_position: float = 0.5,
    ) -> Dict[str, Any]:
        """Configurable SET/RESET sweep with ``mode`` selecting the sweep sequence.

        Args:
            cell_number: 1-based cell index.
            set_v_max: maximum SET voltage in V. Default 1.0.
            reset_v_min: minimum RESET voltage in V. Default 0.0.
            volt_step: voltage step in V. Default 0.01.
            no_cycles: number of cycles. Default 1.
            mode: one of ``'loop'``, ``'separate'``, ``'set_only'``, ``'reset_only'``.
                Default ``'set_only'``.
            compliance: current compliance in mA. Default 1.0.
            trigger_period: seconds per sample point. Default 0.1.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``cycles`` (list of per-sweep dicts with
            ``cycle``, ``direction``, ``time``, ``voltage``, ``current``, ``set_voltage``).
        """
        logger.info('Perform SET-RESET I-V sweep for cell %s', cell_number)
        positive_forward = [np.round(x, 2) for x in np.arange(0, float(set_v_max) + float(volt_step), float(volt_step))]
        positive_reverse = [np.round(x, 2) for x in np.arange(float(set_v_max) - float(volt_step), 0 - float(volt_step), -float(volt_step))]
        set_sweep = np.concatenate([positive_forward, positive_reverse])
        negative_forward = [np.round(x, 2) for x in np.arange(0, float(reset_v_min) - float(volt_step), -float(volt_step))]
        negative_reverse = [np.round(x, 2) for x in np.arange(float(reset_v_min) + float(volt_step), 0 + float(volt_step), float(volt_step))]
        reset_sweep = np.concatenate([negative_forward, negative_reverse])

        def run_sweep(sweep, label, cycle_idx):
            pulse_train_string = ','.join(map(str, sweep))
            trigger_count = str(int(len(sweep)))
            measure_delay = str(trigger_period * measure_delay_position)
            try:
                output_string = self._send_pulse_train_to_keysight(
                    compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
                df = self._string_to_dataframe(output_string)
                return {
                    "cycle": cycle_idx + 1,
                    "direction": label,
                    "time": df['Time (s)'].tolist(),
                    "voltage": df['Voltage (V)'].tolist(),
                    "current": df['Current (A)'].tolist(),
                    "set_voltage": sweep.tolist(),
                }
            except Exception as e:
                logger.error("Error during %s sweep: %s", label, e)
                return None

        cycles = []
        if mode == "loop":
            for i in range(int(no_cycles)):
                r = run_sweep(set_sweep, "Set", i)
                if r: cycles.append(r)
                r = run_sweep(reset_sweep, "Reset", i)
                if r: cycles.append(r)
        elif mode == "separate":
            for i in range(int(no_cycles)):
                r = run_sweep(set_sweep, "Set", i)
                if r: cycles.append(r)
            for i in range(int(no_cycles)):
                r = run_sweep(reset_sweep, "Reset", i)
                if r: cycles.append(r)
        elif mode == "set_only":
            for i in range(int(no_cycles)):
                r = run_sweep(set_sweep, "Set", i)
                if r: cycles.append(r)
        elif mode == "reset_only":
            for i in range(int(no_cycles)):
                r = run_sweep(reset_sweep, "Reset", i)
                if r: cycles.append(r)

        return {"cell_number": cell_number, "cycles": cycles}

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
    ) -> Dict[str, Any]:
        """Measure substrate resistance via a small voltage sweep and a linear fit.

        Args:
            cell_number: 1-based cell index.
            v_max: sweep upper limit in V. Default 1.0.
            v_min: sweep lower limit in V. Default -1.0.
            volt_step: voltage step in V. Default 0.1.
            compliance: current compliance in mA. Default 10.0.
            trigger_period: seconds per sample point. Default 0.1.
            no_cycles: number of forward/reverse sweep cycles. Default 1.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``resistance_ohm``, ``time``,
            ``voltage``, ``current``, ``cycle``.
        """
        logger.info('Measure Resistivity of scaffolds %s', cell_number)
        forward_sweep = [np.round(x, 2) for x in np.arange(v_min, v_max, volt_step)]
        reverse_sweep = [np.round(x, 2) for x in np.arange(v_max, v_min, -volt_step)]
        full_sweep = np.concatenate([forward_sweep, reverse_sweep])

        df_total = pd.DataFrame()
        for i in range(int(no_cycles)):
            logger.info('Measuring cycle: %s', i + 1)
            pulse_train_string = ','.join(map(str, full_sweep))
            trigger_count = str(int(len(full_sweep)))
            measure_delay = str(trigger_period * measure_delay_position)
            output_string = self._send_pulse_train_to_keysight(
                compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
            df = self._string_to_dataframe(output_string)
            df['Cycle'] = i + 1
            df_total = pd.concat([df_total, df], ignore_index=True)

        slope, intercept, r_value, p_value, std_err = linregress(
            df_total['Voltage (V)'], df_total['Current (A)'])
        resistance = 1.0 / slope if slope != 0 else float('inf')
        return {
            "cell_number": cell_number,
            "resistance_ohm": resistance,
            "time": df_total['Time (s)'].tolist(),
            "voltage": df_total['Voltage (V)'].tolist(),
            "current": df_total['Current (A)'].tolist(),
            "cycle": df_total['Cycle'].tolist(),
        }

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
    ) -> Dict[str, Any]:
        """Photovoltaic J-V measurement: forward and reverse voltage sweeps per cycle.

        Args:
            cell_number: 1-based cell index.
            v_min: sweep start voltage in V. Default -0.2.
            v_max: sweep end voltage in V. Default 1.0.
            volt_step: voltage step in V. Default 0.01.
            compliance: current compliance in mA. Default 100.0.
            scan_rate: scan rate in mV/s. Default 500.0.
            cell_area: cell area in cm². Default 0.09.
            irr: irradiance normalisation factor. Default 1.0.
            no_cycles: number of fwd/rev cycle pairs. Default 1.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with key ``cell_number`` and ``cycles`` (list of per-cycle dicts with
            ``cycle``, ``forward`` and ``reverse``, each containing ``voltage``,
            ``current_mA``, ``current_density_mA_cm2``, and ``pv_params``).
        """
        logger.info('Measure J-V of PV cell: %s', cell_number)
        trigger_period = float(volt_step * 1000 / scan_rate)
        measure_delay = str(trigger_period * measure_delay_position)

        forward_sweep = [np.round(x, 2) for x in np.arange(v_min, v_max + volt_step, volt_step)]
        reverse_sweep = [np.round(x, 2) for x in np.arange(v_max, v_min - volt_step, -volt_step)]

        cycles = []
        for i in range(int(no_cycles)):
            logger.info('Measuring cycle %s forward', i + 1)
            fwd_string = ','.join(map(str, forward_sweep))
            output_string_fwd = self._send_pulse_train_to_keysight(
                compliance, fwd_string, str(int(len(forward_sweep))), trigger_period, measure_delay)
            df_fwd = self._string_to_dataframe(output_string_fwd)
            j_fwd = (-1) * df_fwd['Current (A)'] * 1000 / cell_area

            logger.info('Measuring cycle %s reverse', i + 1)
            rev_string = ','.join(map(str, reverse_sweep))
            output_string_rev = self._send_pulse_train_to_keysight(
                compliance, rev_string, str(int(len(reverse_sweep))), trigger_period, measure_delay)
            df_rev = self._string_to_dataframe(output_string_rev)
            j_rev = (-1) * df_rev['Current (A)'] * 1000 / cell_area

            df_fwd_jv = pd.DataFrame({
                'Voltage (V)': df_fwd['Voltage (V)'],
                'Current (mA)': (-1) * df_fwd['Current (A)'] * 1000,
                'Current Density (mA/cm2)': j_fwd,
                'Cycle': i + 1,
            })
            df_rev_jv = pd.DataFrame({
                'Voltage (V)': df_rev['Voltage (V)'],
                'Current (mA)': (-1) * df_rev['Current (A)'] * 1000,
                'Current Density (mA/cm2)': j_rev,
                'Cycle': i + 1,
            })
            pv_params_fwd = PV_calc.calculate_parameters(df=df_fwd_jv)
            pv_params_rev = PV_calc.calculate_parameters(df=df_rev_jv)

            cycles.append({
                "cycle": i + 1,
                "forward": {
                    "voltage": df_fwd_jv['Voltage (V)'].tolist(),
                    "current_mA": df_fwd_jv['Current (mA)'].tolist(),
                    "current_density_mA_cm2": df_fwd_jv['Current Density (mA/cm2)'].tolist(),
                    "pv_params": pv_params_fwd.to_dict(orient='list') if pv_params_fwd is not None else {},
                },
                "reverse": {
                    "voltage": df_rev_jv['Voltage (V)'].tolist(),
                    "current_mA": df_rev_jv['Current (mA)'].tolist(),
                    "current_density_mA_cm2": df_rev_jv['Current Density (mA/cm2)'].tolist(),
                    "pv_params": pv_params_rev.to_dict(orient='list') if pv_params_rev is not None else {},
                },
            })
        return {"cell_number": cell_number, "cycles": cycles}

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
    ) -> Dict[str, Any]:
        """Apply a voltage pulse synchronized with Pico light pulses.

        Args:
            cell_number: 1-based cell index.
            read_voltage: read voltage in V. Default 0.5.
            trigger_period: seconds per sample point. Default 0.1.
            front_rest_duration: dark-rest duration before the light pulse in s. Default 2.0.
            read_duration: total acquisition window in s. Default 10.0.
            compliance: current compliance in mA. Default 10.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.
            light_intensity: Pico light intensity (0–100 %). Default 20.0.
            light_on_duration: light-on duration in s. Default 1.0.
            light_off_duration: light-off duration in s. Default 1.0.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        logger.info('Measure light pulse for cell %s', cell_number)
        front_rest = np.full(int(front_rest_duration / trigger_period), 0, dtype=float)
        read_pulse = np.full(int(read_duration / trigger_period), read_voltage, dtype=float)
        full_pulse_set = np.concatenate([front_rest, read_pulse])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight_light_pulse(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay,
            front_rest_duration, light_intensity, read_duration,
            light_on_duration, light_off_duration)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

    def Keysight_Voltage_Steady(
        self,
        cell_number: int,
        pulse_voltages: List[float] = None,
        trigger_period: float = 0.1,
        voltage_duration: float = 20.0,
        compliance: float = 0.001,
        measure_delay_position: float = 0.5,
    ) -> Dict[str, Any]:
        """Hold a list of steady voltages and record the current over time.

        Args:
            cell_number: 1-based cell index.
            pulse_voltages: list of steady-state voltages in V. Default [0].
            trigger_period: seconds per sample point. Default 0.1.
            voltage_duration: duration per voltage step in s. Default 20.0.
            compliance: current compliance in mA. Default 0.001.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``,
            ``set_voltage``.
        """
        if pulse_voltages is None:
            pulse_voltages = [0.0]
        logger.info('Measure voltage steady state %s', cell_number)
        full_pulse_set = np.array([], dtype=float)
        for pv in pulse_voltages:
            single_pulse = np.full(int(voltage_duration / trigger_period), pv, dtype=float)
            full_pulse_set = np.concatenate([full_pulse_set, single_pulse])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
            "set_voltage": full_pulse_set.tolist(),
        }

    def Keysight_Voltage_list(
        self,
        cell_number: int,
        csv_path: str = "",
        trigger_period: float = 0.1,
        compliance: float = 0.001,
        measure_delay_position: float = 0.5,
    ) -> Dict[str, Any]:
        """Apply an arbitrary voltage list loaded from a CSV file.

        Args:
            cell_number: 1-based cell index.
            csv_path: path to a CSV file with a ``voltage_list`` column.
            trigger_period: seconds per sample point. Default 0.1.
            compliance: current compliance in mA. Default 0.001.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``,
            ``set_voltage``.
        """
        logger.info('Measure Current against voltage list input %s', cell_number)
        voltage_df = pd.read_csv(csv_path)
        pulse_train = voltage_df["voltage_list"].to_numpy(dtype=float)

        pulse_train_string = ','.join(map(str, pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
            "set_voltage": pulse_train.tolist(),
        }

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
    ) -> Dict[str, Any]:
        """Potentiation/depression: repeated write then erase pulse trains.

        Args:
            cell_number: 1-based cell index.
            reset_period: initial 0 V reset duration in s. Default 5.0.
            write_voltage: potentiation pulse amplitude in V. Default 1.94.
            erase_voltage: depression pulse amplitude in V. Default -1.94.
            pulse_duration: write/erase pulse width in s. Default 0.16.
            pulse_no: number of write (or erase) pulses per cycle. Default 50.
            read_voltage: read pulse amplitude in V. Default 1.0.
            read_duration: read pulse width in s. Default 0.16.
            cycle_write_erase: number of potentiation/depression cycles. Default 10.
            trigger_period: seconds per sample point. Default 0.16.
            measure_delay_position: fractional delay within trigger period. Default 0.5.
            compliance: current compliance in mA. Default 100.0.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``,
            ``fitting_params`` (from pot/dep analysis).
        """
        logger.info('Conduct potentiation and depression cycle for cell %s', cell_number)
        full_pulse_set = np.array([], dtype=float)
        reset_pulse = np.full(int(reset_period / trigger_period), 0, dtype=float)
        full_pulse_set = np.concatenate([full_pulse_set, reset_pulse])

        for cycle in range(cycle_write_erase):
            full_write_read = np.array([], dtype=float)
            for pulse in range(pulse_no):
                wp = np.full(int(pulse_duration / trigger_period), write_voltage, dtype=float)
                rp = np.full(int(read_duration / trigger_period), read_voltage, dtype=float)
                full_write_read = np.concatenate([full_write_read, wp, rp])

            full_erase_read = np.array([], dtype=float)
            for pulse in range(pulse_no):
                ep = np.full(int(pulse_duration / trigger_period), erase_voltage, dtype=float)
                rp = np.full(int(read_duration / trigger_period), read_voltage, dtype=float)
                full_erase_read = np.concatenate([full_erase_read, ep, rp])

            full_pulse_set = np.concatenate([full_pulse_set, full_write_read, full_erase_read])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)

        fitting = self._Pot_Dep_Calculation(df, cell_number, reset_period, trigger_period, pulse_no)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
            "fitting_params": fitting.to_dict(orient='list') if fitting is not None else {},
        }

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
    ) -> Dict[str, Any]:
        """Potentiation/depression with explicit pulse-to-read and pulse-to-pulse timing.

        Args:
            cell_number: 1-based cell index.
            reset_period: initial 0 V reset duration in s. Default 0.0.
            write_voltage: potentiation pulse amplitude in V. Default 0.5.
            erase_voltage: depression pulse amplitude in V. Default -0.5.
            pulse_duration: write/erase pulse width in s. Default 0.05.
            pulse_no: number of write (or erase) pulses per cycle. Default 50.
            read_voltage: read pulse amplitude in V. Default 0.1.
            read_duration: read pulse width in s. Default 0.05.
            cycle_write_erase: number of potentiation/depression cycles. Default 3.
            trigger_period: seconds per sample point. Default 0.05.
            measure_delay_position: fractional delay within trigger period. Default 0.5.
            compliance: current compliance in mA. Default 100.0.
            t_pulse_to_read: delay from pulse end to read start in s. Default 0.2.
            t_pulse_to_pulse: total pulse-to-pulse period in s. Default 0.5.
            wait_voltage: voltage held during inter-pulse waits in V. Default 0.0.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``,
            ``fitting_params`` (from pot/dep analysis).
        """
        logger.info('Conduct potentiation and depression cycle (2) for cell %s', cell_number)
        full_pulse_set = np.array([], dtype=float)
        reset_pulse = np.full(int(reset_period / trigger_period), 0, dtype=float) if reset_period > 0 else np.array([], dtype=float)
        full_pulse_set = np.concatenate([full_pulse_set, reset_pulse])

        for cycle in range(cycle_write_erase):
            full_write_read = np.array([], dtype=float)
            for pulse in range(pulse_no):
                wp = np.full(int(pulse_duration / trigger_period), write_voltage, dtype=float)
                wf = np.full(int(t_pulse_to_read / trigger_period), wait_voltage, dtype=float)
                rp = np.full(int(read_duration / trigger_period), read_voltage, dtype=float)
                wr = np.full(int((t_pulse_to_pulse - t_pulse_to_read - read_duration) / trigger_period), wait_voltage, dtype=float)
                full_write_read = np.concatenate([full_write_read, wp, wf, rp, wr])

            full_erase_read = np.array([], dtype=float)
            for pulse in range(pulse_no):
                ep = np.full(int(pulse_duration / trigger_period), erase_voltage, dtype=float)
                wf = np.full(int(t_pulse_to_read / trigger_period), wait_voltage, dtype=float)
                rp = np.full(int(read_duration / trigger_period), read_voltage, dtype=float)
                wr = np.full(int((t_pulse_to_pulse - t_pulse_to_read - read_duration) / trigger_period), wait_voltage, dtype=float)
                full_erase_read = np.concatenate([full_erase_read, ep, wf, rp, wr])

            full_pulse_set = np.concatenate([full_pulse_set, full_write_read, full_erase_read])

        pulse_train_string = ','.join(map(str, full_pulse_set))
        trigger_count = str(int(len(full_pulse_set)))
        measure_delay = str(trigger_period * measure_delay_position)
        output_string = self._send_pulse_train_to_keysight(
            compliance, pulse_train_string, trigger_count, trigger_period, measure_delay)
        df = self._string_to_dataframe(output_string)

        fitting = self._Pot_Dep_Calculation(df, cell_number, reset_period, trigger_period, pulse_no)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
            "fitting_params": fitting.to_dict(orient='list') if fitting is not None else {},
        }

    def Keysight_HT_PotDep(
        self,
        cell_number: int,
        initial_parameters_path: str = "Parameters/initial_parameters.csv",
        bo_campaigns: int = 5,
        inter_measurement_delay: float = 60.0,
    ) -> Dict[str, Any]:
        """High-throughput potentiation/depression Bayesian optimisation.

        Iterates over an initial Latin Hypercube sample then runs ``bo_campaigns``
        rounds of Bayesian optimisation, calling :meth:`Keysight_Potent_Depress_2`
        each iteration with updated pulse parameters.  Requires the ``analysis``
        extra (torch/botorch).

        Args:
            cell_number: 1-based cell index.
            initial_parameters_path: path to the LHS initial-parameters CSV with
                columns ``V_write``, ``t_write``, ``t_pulse_to_pulse``. Default
                ``"Parameters/initial_parameters.csv"``.
            bo_campaigns: number of Bayesian optimisation rounds. Default 5.
            inter_measurement_delay: seconds to wait between iterations. Default 60.

        Returns:
            Dict with key ``cell_number`` and ``results`` (list of fitting-param dicts).
        """
        logger.info('Start HT_PotDep for cell %s', cell_number)
        df_init = pd.read_csv(initial_parameters_path)
        results = []

        logger.debug("--Start initial sampling--")
        for i in range(len(df_init)):
            time.sleep(inter_measurement_delay)
            v_write = df_init.at[i, 'V_write']
            t_write = df_init.at[i, 't_write']
            t_p2p = df_init.at[i, 't_pulse_to_pulse']
            result = self.Keysight_Potent_Depress_2(
                cell_number,
                write_voltage=v_write,
                erase_voltage=-v_write,
                pulse_duration=t_write,
                t_pulse_to_pulse=t_p2p,
            )
            results.append(result.get("fitting_params", {}))
            self._htpd().make_results_summary(result.get("fitting_params"))
        logger.debug("--Initial sampling finished--")

        self.Keysight_Analog_Sweep(cell_number)

        logger.debug("--Start BO sampling. Total campaigns = %s", bo_campaigns)
        for i in range(bo_campaigns):
            time.sleep(inter_measurement_delay)
            self._htpd().main_BO(cell_number)
            result = self.Keysight_Potent_Depress_2(cell_number)
            results.append(result.get("fitting_params", {}))
            self._htpd().make_results_summary(result.get("fitting_params"))
        logger.debug("--BO sampling finished--")

        return {"cell_number": cell_number, "results": results}

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
    ) -> Dict[str, Any]:
        """Open-circuit voltage (Voc) decay: cycle the light on/off and record Voc.

        Args:
            cell_number: 1-based cell index.
            trigger_period: seconds per sample point. Default 0.1.
            light_intensity: Pico light intensity (0–100 %). Default 50.
            light_on_duration: light-on duration per cycle in s. Default 5.0.
            light_off_duration: light-off duration per cycle in s. Default 10.0.
            on_off_cycles: number of on/off cycles. Default 1.
            source_current: SMU source current in A (0 = OCV mode). Default 0.0.
            compliance_voltage: voltage compliance in V. Default 1.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        logger.info('Conduct Voc rise and decay measurement for cell %s', cell_number)
        full_on_off_pulses = np.array([], dtype=float)
        for cycle in range(on_off_cycles):
            on_pulse = np.full(int(light_on_duration / trigger_period), source_current, dtype=float)
            off_pulse = np.full(int(light_off_duration / trigger_period), source_current, dtype=float)
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_pulse, off_pulse])

        pulse_train_string = ','.join(map(str, full_on_off_pulses))
        trigger_count = str(int(len(full_on_off_pulses)))
        measure_delay = str(trigger_period * measure_delay_position)

        output_string = 'NONE'
        try:
            self.smu.timeout = 10000000
            self.smu.write_termination = '\n'
            self.smu.read_termination = '\n'
            self.smu.write("*RST")
            self.smu.write("*CLS")
            self.smu.write(":TRAC:CLE")
            self.smu.write(":TRAC:FEED SENS")
            self.smu.write(":TRAC:FEED:CONT NEXT")
            self.smu.write(":TRAC:TST:FORM ABS")
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")
            self.smu.write(":SOUR:FUNC:MODE CURR")
            self.smu.write(":SOUR:CURR:MODE LIST")
            self.smu.write(":SENS:FUNCtion 'CURRent','VOLTage'")
            self.smu.write(f":SENS:VOLT:PROT {compliance_voltage}")
            self.smu.write(f":LIST:CURR {pulse_train_string}")
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON")
            self.smu.write(f":TRIG:COUN {trigger_count}")
            self.smu.write(":TRIG:SOUR TIM")
            self.smu.write(f":TRIG:TIM {trigger_period}")
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")
            self.smu.write(":OUTP ON")
            self.smu.write(":INIT")
            logger.debug('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_light_pulse(
                light_intensity=light_intensity,
                on_off_cycles=on_off_cycles,
                light_on_duration=light_on_duration,
                light_off_duration=light_off_duration,
            )
            self.smu.query("*OPC?")
            self.smu.write(":OUTP OFF")
            output_string = self.smu.query(":FETC:ARR?")
            logger.debug("%s", self.smu.query(":SYST:ERR?"))
            self.smu.write("*RST")
        except Exception as e:
            logger.error("Error in Keysight_Voc_decay: %s", e)

        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

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
        curr_sense_range: float = 1e-7,
    ) -> Dict[str, Any]:
        """Voc profile: idle, then light on/off cycles, then a read period.

        Args:
            cell_number: 1-based cell index.
            trigger_period: seconds per sample point. Default 0.1.
            light_intensity: Pico light intensity (0–100 %). Default 6.
            wait_time: dark idle time before the first light pulse in s. Default 2.
            light_on_duration: light-on duration per cycle in s. Default 10.
            light_off_duration: light-off duration per cycle in s. Default 60.
            on_off_cycles: number of on/off cycles. Default 1.
            read_time: post-cycle dark read window in s. Default 0.
            source_current: SMU source current in A (0 = OCV). Default 0.0.
            compliance_voltage: voltage compliance in V. Default 1.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.
            NPLC_value: integration time (NPLC). Default 0.01.
            volt_sense_range: fixed voltage sense range in V. Default 2.0.
            curr_sense_range: fixed current sense range in A. Default 1e-7.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        logger.info('Conduct Voc profile measurement for cell %s', cell_number)
        pre_exposure = np.full(int(wait_time / trigger_period), source_current, dtype=float)
        full_on_off_pulses = np.array([], dtype=float)
        for cycle in range(on_off_cycles):
            on_pulse = np.full(int(light_on_duration / trigger_period), source_current, dtype=float)
            off_pulse = np.full(int(light_off_duration / trigger_period), source_current, dtype=float)
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_pulse, off_pulse])
        post_exposure = np.full(int(read_time / trigger_period), source_current, dtype=float) if read_time > 0 else np.array([], dtype=float)
        full_on_off_pulses = np.concatenate([pre_exposure, full_on_off_pulses, post_exposure])
        pulse_train_string = ','.join(map(str, full_on_off_pulses))
        trigger_count = str(int(len(full_on_off_pulses)))
        measure_delay = str(trigger_period * measure_delay_position)

        output_string = 'NONE'
        try:
            self.smu.timeout = 10000000
            self.smu.write_termination = '\n'
            self.smu.read_termination = '\n'
            self.smu.write("*RST")
            self.smu.write("*CLS")
            self.smu.write(":TRAC:CLE")
            self.smu.write(":TRAC:FEED SENS")
            self.smu.write(":TRAC:FEED:CONT NEXT")
            self.smu.write(":TRAC:TST:FORM ABS")
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")
            self.smu.write(":SOUR:FUNC:MODE CURR")
            self.smu.write(":SOUR:CURR:MODE LIST")
            self.smu.write(":SENS:FUNCtion 'CURRent','VOLTage'")
            self.smu.write(f":SENS:VOLT:PROT {compliance_voltage}")
            self.smu.write(f":LIST:CURR {pulse_train_string}")
            self.smu.write(f":SENS:VOLT:NPLC {NPLC_value}")
            self.smu.write(f":SENS:CURR:NPLC {NPLC_value}")
            self.smu.write(":SENS:VOLT:RANG:AUTO OFF")
            self.smu.write(f":SENS:VOLT:RANG {volt_sense_range}")
            self.smu.write(":SENS:CURR:RANG:AUTO OFF")
            self.smu.write(f":SENS:CURR:RANG {curr_sense_range}")
            self.smu.write(f":TRIG:COUN {trigger_count}")
            self.smu.write(":TRIG:SOUR TIM")
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")
            self.smu.write(":OUTP ON")
            self.smu.write(":INIT")
            logger.debug('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_profile_light_pulse(
                light_intensity=light_intensity,
                idle_time=wait_time,
                on_off_cycles=on_off_cycles,
                light_on_duration=light_on_duration,
                light_off_duration=light_off_duration,
                read_period=read_time,
            )
            self.smu.query("*OPC?")
            self.smu.write(":OUTP OFF")
            output_string = self.smu.query(":FETC:ARR?")
            logger.debug("%s", self.smu.query(":SYST:ERR?"))
            self.smu.write("*RST")
        except Exception:
            logger.exception("Error in Keysight_Voc_profile")

        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

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
        compliance_current: float = 1e-3,
        measure_delay_position: float = 0.5,
    ) -> Dict[str, Any]:
        """Short-circuit current (Jsc) profile under light on/off cycling.

        Args:
            cell_number: 1-based cell index.
            trigger_period: seconds per sample point. Default 0.1.
            light_intensity: Pico light intensity (0–100 %). Default 10.
            wait_time: dark idle time before first light pulse in s. Default 1.
            light_on_duration: light-on duration per cycle in s. Default 5.
            light_off_duration: light-off duration per cycle in s. Default 5.
            on_off_cycles: number of on/off cycles. Default 1.
            read_time: post-cycle dark read window in s. Default 5.
            source_voltage: SMU source voltage in V (0 = Jsc mode). Default 0.0.
            compliance_current: current compliance in A. Default 1e-3.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        logger.info('Conduct Jsc profile measurement for cell %s', cell_number)
        pre_exposure = np.full(int(wait_time / trigger_period), source_voltage, dtype=float)
        full_on_off_pulses = np.array([], dtype=float)
        for cycle in range(on_off_cycles):
            on_pulse = np.full(int(light_on_duration / trigger_period), source_voltage, dtype=float)
            off_pulse = np.full(int(light_off_duration / trigger_period), source_voltage, dtype=float)
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_pulse, off_pulse])
        post_exposure = np.full(int(read_time / trigger_period), source_voltage, dtype=float)
        full_on_off_pulses = np.concatenate([pre_exposure, full_on_off_pulses, post_exposure])
        pulse_train_string = ','.join(map(str, full_on_off_pulses))
        trigger_count = str(int(len(full_on_off_pulses)))
        measure_delay = str(trigger_period * measure_delay_position)

        output_string = 'NONE'
        try:
            self.smu.timeout = 10000000
            self.smu.write_termination = '\n'
            self.smu.read_termination = '\n'
            self.smu.write("*RST")
            self.smu.write("*CLS")
            self.smu.write(":TRAC:CLE")
            self.smu.write(":TRAC:FEED SENS")
            self.smu.write(":TRAC:FEED:CONT NEXT")
            self.smu.write(":TRAC:TST:FORM ABS")
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")
            self.smu.write(":SOUR:FUNC:MODE VOLT")
            self.smu.write(":SOUR:VOLT:MODE LIST")
            self.smu.write(":SENS:FUNCtion 'CURRent','VOLTage'")
            self.smu.write(f":SENS:CURR:PROT {str(compliance_current)}")
            self.smu.write(f":LIST:VOLT {pulse_train_string}")
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON")
            self.smu.write(f":TRIG:COUN {trigger_count}")
            self.smu.write(":TRIG:SOUR TIM")
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")
            self.smu.write(f":TRIG:ACQ:DEL {measure_delay}")
            self.smu.write(":OUTP ON")
            self.smu.write(":INIT")
            logger.debug('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_profile_light_pulse(
                light_intensity=light_intensity,
                idle_time=wait_time,
                on_off_cycles=on_off_cycles,
                light_on_duration=light_on_duration,
                light_off_duration=light_off_duration,
                read_period=read_time,
            )
            self.smu.query("*OPC?")
            self.smu.write(":OUTP OFF")
            output_string = self.smu.query(":FETC:ARR?")
            logger.debug("%s", self.smu.query(":SYST:ERR?"))
            self.smu.write("*RST")
        except Exception:
            logger.exception("Error in Keysight_Jsc_profile")

        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

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
    ) -> Dict[str, Any]:
        """Voc decay with multi-level light soaking before the on/off cycles.

        Args:
            cell_number: 1-based cell index.
            trigger_period: seconds per sample point. Default 0.1.
            light_intensity: Pico light intensity (0–100 %). Default 100.
            light_off_duration1–5: dark durations for each soaking level in s. Default 1.0.
            light_on1–5: light-on durations for each soaking level in s. Default 1.0.
            on_off_cycles: number of on/off cycles. Default 1.
            source_current: SMU source current in A. Default 0.0.
            compliance: voltage compliance in V. Default 2.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.
            soaking_time: pre-measurement light soaking duration in s. Default 0.0.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        logger.info('Conduct Voc decay with indiv soaking for cell %s', cell_number)
        soaking_pulse = np.full(int(soaking_time / trigger_period), source_current, dtype=float) if soaking_time > 0 else np.array([], dtype=float)
        full_on_off_pulses = np.array([], dtype=float)
        for cycle in range(on_off_cycles):
            off1 = np.full(int(light_off_duration1 / trigger_period), source_current, dtype=float)
            on1 = np.full(int(light_on1 / trigger_period), source_current, dtype=float)
            off2 = np.full(int(light_off_duration2 / trigger_period), source_current, dtype=float)
            on2 = np.full(int(light_on2 / trigger_period), source_current, dtype=float)
            off3 = np.full(int(light_off_duration3 / trigger_period), source_current, dtype=float)
            on3 = np.full(int(light_on3 / trigger_period), source_current, dtype=float)
            off4 = np.full(int(light_off_duration4 / trigger_period), source_current, dtype=float)
            on4 = np.full(int(light_on4 / trigger_period), source_current, dtype=float)
            off5 = np.full(int(light_off_duration5 / trigger_period), source_current, dtype=float)
            on5 = np.full(int(light_on5 / trigger_period), source_current, dtype=float)
            on_off_pulse = np.concatenate([off1, on1, off2, on2, off3, on3, off4, on4, off5, on5])
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_off_pulse])
        full_soak_on_off_pulses = np.concatenate([soaking_pulse, full_on_off_pulses])
        trigger_count = str(int(len(full_soak_on_off_pulses)))

        output_string = 'NONE'
        try:
            self.smu.timeout = 10000000
            self.smu.write_termination = '\n'
            self.smu.read_termination = '\n'
            self.smu.write("*RST")
            self.smu.write(":TRAC:CLE")
            self.smu.write(f":TRAC:POIN {trigger_count}")
            self.smu.write(":TRAC:FEED SENS")
            self.smu.write(":TRAC:FEED:CONT NEXT")
            self.smu.write(":TRAC:TST:FORM ABS")
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")
            self.smu.write(":SOUR:FUNC:MODE CURR")
            self.smu.write(":SOUR:CURR:MODE LIST")
            self.smu.write(f":SENS:VOLT:PROT {compliance}")
            self.smu.write(f":LIST:CURR {full_soak_on_off_pulses}")
            self.smu.write("SENS:VOLT:RANG AUTO")
            self.smu.write(f":TRIG:COUN {trigger_count}")
            self.smu.write(":TRIG:SOUR TIMER")
            self.smu.write(f":TRIG:TIM {trigger_period}")
            self.smu.write(":OUTP ON")
            self.smu.write(":INIT")
            logger.debug('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.voc_light_pulse_soak(
                light_intensity=light_intensity,
                on_off_cycles=on_off_cycles,
                soaking_time=soaking_time,
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
            self.smu.query("*OPC?")
            self.smu.write(":OUTP OFF")
            output_string = self.smu.query(":FETC:ARR?")
            logger.debug("%s", self.smu.query("SYST:ERR?"))
        except Exception:
            logger.exception("Error in Keysight_Voc_decay_indiv_soaking")

        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

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
    ) -> Dict[str, Any]:
        """Voc decay with variable light on/off durations per cycle.

        Args:
            cell_number: 1-based cell index.
            trigger_period: seconds per sample point. Default 0.01.
            light_intensity: Pico light intensity (0–100 %). Default 10.
            wait_time: pre-measurement idle time in s. Default 0.5.
            light_off_duration1–5: dark durations per level in s.
            light_on1–5: light-on durations per level in s.
            on_off_cycles: number of on/off cycles. Default 2.
            source_current: SMU source current in A. Default 0.0.
            voltage_compliance: voltage compliance in V. Default 1.0.
            measure_delay_position: fractional delay within trigger period. Default 0.5.

        Returns:
            Dict with keys ``cell_number``, ``time``, ``voltage``, ``current``.
        """
        logger.info('Conduct Voc decay ON/OFF variation for cell %s', cell_number)
        if self.smu is None:
            logger.info("Error: SMU is not connected.")
            return {"cell_number": cell_number, "time": [], "voltage": [], "current": []}

        pre_exposure = np.full(int(wait_time / trigger_period), source_current, dtype=float)
        full_on_off_pulses = np.array([], dtype=float)
        for cycle in range(on_off_cycles):
            on1 = np.full(int(light_on1 / trigger_period), source_current, dtype=float)
            off1 = np.full(int(light_off_duration1 / trigger_period), source_current, dtype=float)
            on2 = np.full(int(light_on2 / trigger_period), source_current, dtype=float)
            off2 = np.full(int(light_off_duration2 / trigger_period), source_current, dtype=float)
            on3 = np.full(int(light_on3 / trigger_period), source_current, dtype=float)
            off3 = np.full(int(light_off_duration3 / trigger_period), source_current, dtype=float)
            on4 = np.full(int(light_on4 / trigger_period), source_current, dtype=float)
            off4 = np.full(int(light_off_duration4 / trigger_period), source_current, dtype=float)
            on5 = np.full(int(light_on5 / trigger_period), source_current, dtype=float)
            off5 = np.full(int(light_off_duration5 / trigger_period), source_current, dtype=float)
            on_off_pulse = np.concatenate([on1, off1, on2, off2, on3, off3, on4, off4, on5, off5])
            full_on_off_pulses = np.concatenate([full_on_off_pulses, on_off_pulse])
        pulse_train = np.concatenate([pre_exposure, full_on_off_pulses])
        pulse_train_string = ','.join(map(str, pulse_train))
        trigger_count = str(int(len(pulse_train)))
        measure_delay = trigger_period * measure_delay_position

        output_string = 'NONE'
        try:
            self.smu.timeout = 10000000
            self.smu.write_termination = '\n'
            self.smu.read_termination = '\n'
            self.smu.write("*RST")
            self.smu.write("*CLS")
            self.smu.query(":SYST:ERR?")
            self.smu.write(":TRAC:CLE")
            self.smu.write(":TRAC:FEED SENS")
            self.smu.write(":TRAC:FEED:CONT NEXT")
            self.smu.write(":TRAC:TST:FORM ABS")
            self.smu.write(":FORM:ELEM:SENS TIME,VOLT,CURR")
            self.smu.write(":SOUR:FUNC:MODE CURR")
            self.smu.write(":SOUR:CURR:MODE LIST")
            self.smu.write(f":LIST:CURR {pulse_train_string}")
            self.smu.write(":SENS:FUNC 'CURR', 'VOLT'")
            self.smu.write(f":SENS:VOLT:PROT {str(voltage_compliance)}")
            self.smu.write(":SENS:REM ON")
            self.smu.write(":SENS:VOLT:RANG:AUTO ON;:SENS:CURR:RANG:AUTO ON")
            self.smu.write(f":TRIG:COUN {trigger_count}")
            self.smu.write(":TRIG:SOUR TIM")
            self.smu.write(f":TRIG:TIM {str(trigger_period)}")
            self.smu.write(f":TRIG:ACQ:DEL {str(measure_delay)}")
            self.smu.write(":OUTP ON")
            self.smu.write(":INIT")
            logger.debug('SENDING LIGHT PULSE COMMANDS TO PICO')
            self.pico_instrument.light_pulse_ON_OFF_variation(
                light_intensity=light_intensity,
                on_off_cycles=on_off_cycles,
                idle_time=wait_time,
                light_on1=light_on1, light_off_duration1=light_off_duration1,
                light_on2=light_on2, light_off_duration2=light_off_duration2,
                light_on3=light_on3, light_off_duration3=light_off_duration3,
                light_on4=light_on4, light_off_duration4=light_off_duration4,
                light_on5=light_on5, light_off_duration5=light_off_duration5,
            )
            self.smu.query("*OPC?")
            self.smu.write(":OUTP OFF")
            output_string = self.smu.query(":FETC:ARR?")
            logger.debug("%s", self.smu.query(":SYST:ERR?"))
        except Exception as e:
            logger.info("Error communicating with SMU: %s", e)

        df = self._string_to_dataframe(output_string)
        return {
            "cell_number": cell_number,
            "time": df['Time (s)'].tolist(),
            "voltage": df['Voltage (V)'].tolist(),
            "current": df['Current (A)'].tolist(),
        }

    def Keysight_Time_Gap(
        self,
        value: int = None,
        sleep: float = 60.0,
    ) -> Dict[str, Any]:
        """Idle / wait step used to insert delays into a measurement queue.

        Args:
            value: unused; present for compatibility with the common
                ``(cell_number)`` call signature.
            sleep: seconds to sleep. Default 60.

        Returns:
            Dict with key ``slept_seconds``.
        """
        logger.info("Sleeping for %s seconds...", sleep)
        time.sleep(sleep)
        return {"slept_seconds": sleep}


# ``Keysight_Digital_Retention`` is the advertised name; the implementation is
# ``Keysight_Digital_Endurance``.  Alias so PUDA + GUI resolve both names.
SMUKeysightDriver.Keysight_Digital_Retention = SMUKeysightDriver.Keysight_Digital_Endurance
