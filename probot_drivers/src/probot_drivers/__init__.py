"""Shared driver library for the probot platform.

Used by the PUDA edge services (``probot-smu-keysight``, ``probot-stage``) and the
Tkinter GUI (via the ``gui`` shims), so hardware control
has a single source of truth.

Edge machine drivers (from the shared library):

* :class:`SMUKeysightProbotMachine` - composite SMU+light+measurements driver.
  The ``probot-smu-keysight`` edge now uses its own self-contained
  ``driver.py`` (:class:`SMUKeysightDriver`) which imports analysis helpers
  from this package.  Needs the ``smu`` extra.
* :class:`PicoProbot` - Pico G2V light controller.  Used internally by the
  SMU machine for inline light-synchronised measurements.
* :class:`ProbotStage` - Ender 3-axis stage (``probot-stage`` edge). Needs the
  ``stage`` extra.

Imports are **lazy** (PEP 562): importing this package pulls no heavy dependencies,
so the lean ``probot-stage`` edge does not need numpy/pandas/pyvisa just to import
``ProbotStage``. Each name loads its module (and that module's deps) on first access.
"""

import importlib

# Public name -> submodule that defines it.
_EXPORTS = {
    "SMUKeysightProbotMachine": "probot_machine_smu",
    "ProbotStage": "probot_stage",
    "StageProbot": "probot_stage",
    "SMUKeysightProbot": "probot_smu_keysight",
    "PicoProbot": "probot_pico",
    "ProbotMeasurement": "probot_measurement",
    "MeasurementProbot": "probot_measurement",
    "measurement_list": "probot_measurement",
    "MEASUREMENT_NAMES": "probot_measurement",
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
