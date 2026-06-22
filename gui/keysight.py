"""GUI compatibility shim for the Keysight equipment plugin.

The Tkinter GUI loads equipment via ``importlib.import_module("keysight")``,
expects a module-level ``measurement_list()`` and a no-arg ``KeysightInstrument``
class, then calls ``getattr(instrument, measurement)(cell_number)``.

This shim preserves that exact contract while delegating to the shared
``probot_drivers`` package, so the GUI and the PUDA ``probot-smu-keysight`` edge
run identical driver code. A single process-wide
:class:`~probot_drivers.probot_machine_smu.SMUKeysightProbotMachine` backs every
``KeysightInstrument()`` (the GUI builds one per measurement) so the VISA session
and Pico connection are opened once, not per cell.

Connection settings come from the environment (same names as the edge ``.env``):
``KEYSIGHT_ADDRESS``, ``PICO_IP``, ``PICO_ID``, ``STAGE_PORT``. Unset values fall
back to ``None`` (the SMU then auto-selects the first VISA resource).
"""

import os
import logging

from probot_drivers import SMUKeysightProbotMachine
from probot_drivers.probot_measurement import measurement_list as _measurement_list

logger = logging.getLogger(__name__)

_shared_machine = None


def measurement_list():
    """Return the available measurement names (GUI plugin contract)."""
    return _measurement_list()


def _get_machine() -> SMUKeysightProbotMachine:
    """Return the process-wide SMU+light machine, connecting on first use.

    The stage is a separate device handled by the ``probebot`` shim, so it is not
    touched here.
    """
    global _shared_machine
    if _shared_machine is None:
        machine = SMUKeysightProbotMachine(
            smu_address=os.environ.get("KEYSIGHT_ADDRESS") or None,
            smu_device_no=int(os.environ.get("KEYSIGHT_DEVICE_NO", "0")),
            pico_ip=os.environ.get("PICO_IP") or None,
            pico_id=os.environ.get("PICO_ID") or None,
            # The GUI's execute_measurement writes parameters to a cwd-relative
            # "Parameters" folder, so the machine must read from the same place.
            param_dir=os.environ.get("PARAM_DIR") or "Parameters",
            data_dir=os.environ.get("DATA_DIR") or os.path.join("Data", "Keysight"),
        )
        machine.startup()
        _shared_machine = machine
    return _shared_machine


class KeysightInstrument:
    """Adapter exposing the shared SMU+light machine via the GUI's class name."""

    def __init__(self):
        self._machine = _get_machine()

    def __getattr__(self, name):
        # Delegate measurement / helper calls (e.g. Keysight_JV_PV) to the machine.
        return getattr(self._machine, name)
