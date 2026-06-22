"""Pico G2V LED light controller for the probot platform.

Refactor of the original ``pico.py``. Two important changes versus the original:

* **No import-time side effects.** The original module instantiated the device
  and called ``light_off()`` at import, which hit the network on every
  ``import pico``. Connection now happens only in :meth:`startup`.
* **Configurable address.** The Pico IP and device id were hardcoded; they are
  now constructor arguments (sourced from the edge ``.env`` in production).

The public light methods are preserved verbatim so the measurement routines and
the GUI behave exactly as before.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


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
