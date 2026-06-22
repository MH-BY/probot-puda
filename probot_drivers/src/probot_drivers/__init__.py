"""Shared driver library for the probot platform.

Used by both the PUDA edge services (``smu-keysight-probot``, ``stage-probot``)
and the Tkinter GUI (via the ``gui`` shims), so hardware control has a single
source of truth.

Edge machine drivers:

* :class:`SMUKeysightProbotMachine` - the ``smu-keysight-probot`` edge
  (Keysight SMU + Pico light + the measurement routines). Needs the ``smu`` extra.
* :class:`StageProbot` - the ``stage-probot`` edge (Ender 3-axis stage). Needs the
  ``stage`` extra.

The SMU and light are co-located in one machine because several measurements drive
the light inline during the SMU acquisition; the stage is independent.

Imports are **lazy** (PEP 562): importing this package pulls no heavy dependencies,
so the lean ``stage-probot`` edge does not need numpy/pandas/pyvisa just to import
``StageProbot``. Each name loads its module (and that module's deps) on first access.
"""

import importlib

# Public name -> submodule that defines it.
_EXPORTS = {
    "SMUKeysightProbotMachine": "machine_smu_probot",
    "StageProbot": "stage_probot",
    "SMUKeysightProbot": "smu_keysight_probot",
    "PicoProbot": "pico_probot",
    "MeasurementProbot": "measurement_probot",
    "measurement_list": "measurement_probot",
    "MEASUREMENT_NAMES": "measurement_probot",
}

__all__ = list(_EXPORTS) + ["orchestrator_probot"]


def __getattr__(name):
    if name == "orchestrator_probot":
        return importlib.import_module(f".{name}", __name__)
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f".{module}", __name__), name)


def __dir__():
    return sorted(__all__)
