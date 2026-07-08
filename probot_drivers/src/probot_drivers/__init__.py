"""Shared driver library for the probot platform.

Used by both the PUDA edge services (``probot-keysight-pico``, ``probot-stage``)
and the Tkinter GUI (via the ``gui`` shims), so hardware control has a single
source of truth.

Edge machine drivers:

* :class:`KeysightPicoProbotMachine` - the ``probot-keysight-pico`` edge
  (Keysight SMU + Pico light + the measurement routines). Needs the ``smu`` extra.
* :class:`ProbotStage` - the ``probot-stage`` edge (Ender 3-axis stage). Needs the
  ``stage`` extra.

The SMU and light are co-located in one machine because several measurements drive
the light inline during the SMU acquisition; the stage is independent.

Imports are **lazy** (PEP 562): importing this package pulls no heavy dependencies,
so the lean ``probot-stage`` edge does not need numpy/pandas/pyvisa just to import
``ProbotStage``. Each name loads its module (and that module's deps) on first access.
"""

import importlib

# Public name -> submodule that defines it.
_EXPORTS = {
    "KeysightPicoProbotMachine": "probot_machine_keysight_pico",
    "ProbotStage": "probot_stage",
    "StageProbot": "probot_stage",
    "KeysightProbot": "probot_keysight",
    "PicoProbot": "probot_pico",
    "measurement_list": "probot_machine_keysight_pico",
    "MEASUREMENT_NAMES": "probot_machine_keysight_pico",
}

__all__ = list(_EXPORTS) + ["probot_orchestrator"]


def __getattr__(name):
    if name == "probot_orchestrator":
        return importlib.import_module(f".{name}", __name__)
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f".{module}", __name__), name)


def __dir__():
    return sorted(__all__)
