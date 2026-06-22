"""SMU+light machine for the probot platform (the ``probot-smu-keysight`` edge).

The Keysight SMU and the Pico light live in **one** machine because several
measurements drive the light inline, during the SMU acquisition, with sub-second
timing (``Keysight_Light_Pulse``, ``Keysight_Voc_decay``, ``Keysight_Voc_profile``,
``Keysight_Jsc_profile``, ``Keysight_Voc_decay_indiv_soaking``,
``Keysight_Voc_decay_ON_OFF_Variation``). Splitting them into separate processes
would break that timing, so they are co-located here.

The stage is a *separate* edge service (see :class:`StageProbot`).

The measurement routines are inherited from
:class:`~probot_drivers.probot_measurement.ProbotMeasurement` and expect
``self.smu`` to be the raw PyVISA resource and ``self.pico_instrument`` to be the
light driver, so this class exposes ``smu`` as a property over the SMU sub-driver
and aliases ``pico_instrument`` to ``light``.
"""

from __future__ import annotations

import logging
import os

from .probot_smu_keysight import SMUKeysightProbot
from .probot_pico import PicoProbot
from .probot_measurement import ProbotMeasurement, measurement_list

logger = logging.getLogger(__name__)

# Packaged default parameter directory (writable in an editable/dev install).
# For production set PROBOT_PARAM_DIR to a writable, mounted location.
_DEFAULT_PARAM_DIR = os.path.join(os.path.dirname(__file__), "parameters")
_DEFAULT_DATA_DIR = os.path.join("Data", "Keysight")


class SMUKeysightProbotMachine(ProbotMeasurement):
    """The probot SMU+light machine: Keysight SMU + Pico light + measurements."""

    instrument_family = "probot_smu_keysight"

    def __init__(
        self,
        smu_address: str | None = None,
        smu_device_no: int = 0,
        pico_ip: str | None = None,
        pico_id: str | None = None,
        param_dir: str | None = None,
        data_dir: str | None = None,
    ) -> None:
        """Wire up the SMU + light sub-controllers (does not connect)."""
        self._smu = SMUKeysightProbot(address=smu_address, device_no=smu_device_no)
        self.light = PicoProbot(ip=pico_ip, device_id=pico_id)
        # Alias expected by the ported measurement routines.
        self.pico_instrument = self.light

        self._param_dir = param_dir or os.environ.get("PROBOT_PARAM_DIR") or _DEFAULT_PARAM_DIR
        self._data_dir = data_dir or os.environ.get("PROBOT_DATA_DIR") or _DEFAULT_DATA_DIR

        logger.info(
            "SMUKeysightProbotMachine initialised (smu_address=%s, pico_ip=%s, param_dir=%s)",
            smu_address, pico_ip, self._param_dir,
        )

    @property
    def smu(self):
        """Raw PyVISA resource (consumed by the ported measurement routines)."""
        return self._smu.smu

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self) -> bool:
        """Connect the SMU and the light. Returns True if both connected."""
        logger.info("Starting up SMU + light")
        ok_smu = self._smu.startup()
        ok_light = self.light.startup()
        if ok_smu and ok_light:
            logger.info("SMU+light startup complete")
        else:
            logger.warning("SMU+light startup partial (smu=%s, light=%s)", ok_smu, ok_light)
        return ok_smu and ok_light

    def shutdown(self) -> bool:
        """Shut down the SMU and the light (required PUDA lifecycle method)."""
        return all([self._smu.shutdown(), self.light.shutdown()])

    def home(self) -> bool:
        """No homing for the SMU/light (required PUDA lifecycle method)."""
        return True

    def get_position(self) -> dict:
        """The SMU/light machine has no spatial position."""
        return {}

    def identify(self) -> str:
        """Return the SMU identification string."""
        return self._smu.identify()

    def measurement_list(self) -> list:
        """Return the available measurement primitive names."""
        return measurement_list()

    # ------------------------------------------------------------------
    # Manual light primitives (the synchronized ones are inside measurements)
    # ------------------------------------------------------------------

    def light_on(self):
        """Turn the light fully on."""
        return self.light.light_on()

    def light_off(self):
        """Turn the light off."""
        return self.light.light_off()


# The source ``measurement_list()`` advertises ``Keysight_Digital_Retention`` while
# the implementation method is ``Keysight_Digital_Endurance`` (it reads the
# ``parameter_Keysight_Digital_Retention.csv`` file). Alias so the advertised name
# resolves both as a PUDA primitive and from the GUI plugin dispatch.
SMUKeysightProbotMachine.Keysight_Digital_Retention = ProbotMeasurement.Keysight_Digital_Endurance
