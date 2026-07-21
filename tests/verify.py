"""Hardware-free verification for the probot-puda edges (one-folder-per-edge layout).

Each edge is now self-contained: its driver code lives in ``<edge>/driver.py`` and
there is no shared ``probot_drivers`` package. This harness loads each edge's
``driver.py`` directly by file path (the same way each edge's ``main.py`` does with
``from driver import ...``, just under a unique module name so both can coexist in
one process for testing).

Network access to PyPI is unavailable here, so the heavy scientific deps
(numpy/pandas/scipy/matplotlib) and the hardware/vendor libs
(pyvisa/g2vpico/controllably) are replaced with lightweight stubs installed in
``sys.modules`` *before* the drivers import. The measurement *bodies* are never
executed - these checks cover import-safety, construction without hardware, PUDA
primitive reflection per edge, and the measurement return/`_`-helper contract.

The Tkinter GUI and the shared ``probot_orchestrator`` are DEFERRED (see
gui/README.md), so their call-order / plugin-contract checks are not run here.

Run: ``python tests/verify.py``  (exits non-zero on first failure).
"""

import sys
import types
import importlib.util
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Install stubs for unavailable third-party / hardware modules.
# --------------------------------------------------------------------------
def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


for _n in ("numpy", "pandas"):
    _mod(_n)

_scipy = _mod("scipy")
_stats = _mod("scipy.stats")
_stats.linregress = lambda *a, **k: None
_scipy.stats = _stats

_mpl = _mod("matplotlib")
_mpl.pyplot = _mod("matplotlib.pyplot")
_mpl.colors = _mod("matplotlib.colors")

_pyvisa = _mod("pyvisa")


class _RM:
    def list_resources(self):
        return []

    def open_resource(self, *a, **k):
        raise RuntimeError("no VISA device (stub)")


_pyvisa.ResourceManager = _RM

_g2v = _mod("g2vpico")


class _G2VPico:
    def __init__(self, *a, **k):
        pass

    def set_global_intensity(self, *a, **k):
        pass


_g2v.G2VPico = _G2VPico

_ctrl = _mod("controllably")
_move = _mod("controllably.Move")
_cart = _mod("controllably.Move.Cartesian")


class _Ender:
    def __init__(self, *a, **k):
        self.coordinates = [1.0, 2.0, 3.0]

    def moveTo(self, *a, **k):
        pass

    def moveBy(self, *a, **k):
        pass


_cart.Ender = _Ender
_ctrl.Move = _move
_move.Cartesian = _cart


# --------------------------------------------------------------------------
# Load each edge's self-contained driver.py by path (unique module names so the
# two edges can be imported together in this single test process).
# --------------------------------------------------------------------------
def _load_edge_driver(edge_folder, mod_name):
    path = ROOT / edge_folder / "driver.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Tiny assert harness.
# --------------------------------------------------------------------------
_checks = []


def check(name, cond):
    _checks.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise AssertionError(name)


# --------------------------------------------------------------------------
# 0. Installable dependency metadata.
# --------------------------------------------------------------------------
print("0. dependency metadata")
with (ROOT / "probot-stage" / "pyproject.toml").open("rb") as f:
    stage_dependencies = tomllib.load(f)["project"]["dependencies"]
stage_dependency_names = {
    re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower()
    for requirement in stage_dependencies
}
check(
    "stage uses the published control-lab-ly distribution",
    "control-lab-ly" in stage_dependency_names
    and "controllably" not in stage_dependency_names,
)


# --------------------------------------------------------------------------
# 1. Import safety (no hardware, no network) — each edge's driver.py loads.
# --------------------------------------------------------------------------
print("1. import safety")
kp = _load_edge_driver("probot-keysight-pico", "kp_driver")
stage_mod = _load_edge_driver("probot-stage", "stage_driver")

KeysightPicoProbotMachine = kp.KeysightPicoProbotMachine
StageProbot = stage_mod.StageProbot
measurement_list = kp.measurement_list
check("both edge driver.py modules import", True)
check("keysight-pico bundles its sub-drivers",
      hasattr(kp, "KeysightProbot") and hasattr(kp, "PicoProbot"))


# --------------------------------------------------------------------------
# 2. Both edge machines construct without touching hardware.
# --------------------------------------------------------------------------
print("2. construction without hardware")
smu = KeysightPicoProbotMachine()
stage = StageProbot(port="COMX")
check("smu machine wires smu+light", smu._smu and smu.light)
check("smu/light not connected yet", not smu._smu.is_connected and not smu.light.is_connected)
check("stage not connected yet", not stage.is_connected)


# --------------------------------------------------------------------------
# 3. PUDA primitive reflection per edge machine.
# --------------------------------------------------------------------------
print("3. primitive reflection")
names = measurement_list()
check("measurement names present", len(names) >= 20)
missing = [n for n in names if not callable(getattr(smu, n, None))]
check(f"all measurements callable on SMU machine (missing={missing})", not missing)
for prim in ("light_on", "light_off", "identify", "home", "shutdown", "startup", "measurement_list"):
    check(f"smu primitive present: {prim}", callable(getattr(smu, prim, None)))
check("smu get_position returns {}", smu.get_position() == {})

for prim in ("cell_coordinates", "move_to", "move_to_cell", "probe", "unprobe",
             "probing", "unprobing", "move_to_cell1", "move_to_cell81",
             "move_to_safeposition", "home", "shutdown", "startup", "get_position"):
    check(f"stage primitive present: {prim}", callable(getattr(stage, prim, None)))


# --------------------------------------------------------------------------
# 4. Stage telemetry shape.
# --------------------------------------------------------------------------
print("4. stage get_position shape")
stage.startup()  # uses stubbed Ender -> coordinates [1,2,3]
check("stage position shape", stage.get_position() == {"x": 1.0, "y": 2.0, "z": 3.0})


# --------------------------------------------------------------------------
# 5. Measurements: defined ON the class, annotated, typed kwargs with defaults.
# --------------------------------------------------------------------------
print("5. measurement return contract")
import inspect
import typing

# CRITICAL (PUDA): every measurement must be defined DIRECTLY on the machine class,
# because PUDA exposes only own methods, not inherited ones.
own = set(vars(KeysightPicoProbotMachine))
missing_own = [n for n in measurement_list() if n not in own]
check(f"all measurements defined ON the machine class (missing={missing_own})", not missing_own)
check("measurement __qualname__ belongs to the machine class",
      KeysightPicoProbotMachine.Keysight_JV_PV.__qualname__.startswith("KeysightPicoProbotMachine"))
for lc in ("startup", "shutdown", "home", "reset", "get_position", "identify",
           "measurement_list", "light_on", "light_off"):
    check(f"lifecycle/command defined on class: {lc}", lc in own)

# Measurements RETURN their data (list[dict] records) — not an envelope, not a file.
jvpv = KeysightPicoProbotMachine.Keysight_JV_PV
check("measurement not decorated (returns data directly)", not hasattr(jvpv, "__wrapped__"))
rann = inspect.signature(jvpv, eval_str=True).return_annotation
check("measurement annotated -> list[dict]",
      rann in (list[dict], typing.List[dict]))
# annotations may be strings (from `from __future__ import annotations`); resolve
# them the way PUDA does before checking types.
rsig = inspect.signature(jvpv, eval_str=True)
check("measurement cell_number annotated int",
      rsig.parameters["cell_number"].annotation is int)

# measurements take their settings as typed kwargs WITH DEFAULTS (from the CSVs)
sig = inspect.signature(jvpv)
for p in ("v_min", "v_max", "volt_step", "compliance", "scan_rate", "cell_area", "no_cycles"):
    check(f"JV_PV param present: {p}", p in sig.parameters)
check("JV_PV params have defaults (callable with just cell_number)",
      all(pp.default is not inspect._empty
          for n, pp in sig.parameters.items() if n not in ("self", "cell_number")))
ap_sig = inspect.signature(KeysightPicoProbotMachine.Keysight_analog_pulse)
check("integer-count default normalised to int (analog_pulse.no_of_pulses)",
      isinstance(ap_sig.parameters["no_of_pulses"].default, int)
      and ap_sig.parameters["no_of_pulses"].default == 5)


# --------------------------------------------------------------------------
# 6. SCPI/data/save helpers are PRIVATE -> not part of the PUDA primitive surface.
# --------------------------------------------------------------------------
print("6. helpers hidden from primitive surface")
for h in ("make_voltage_pulses", "send_pulse_train_to_keysight", "string_to_dataframe",
          "savefile", "savefile_1"):
    check(f"helper not public: {h}", getattr(smu, h, None) is None)
    check(f"private helper present: _{h}", callable(getattr(smu, "_" + h, None)))

# analysis + plotting are removed from the machine (they become agent skills)
for gone in ("make_graph", "make_graph_IV", "make_graph_IV_1", "Pot_Dep_Calculation",
             "_make_graph", "_make_graph_IV", "_make_graph_IV_1", "_Pot_Dep_Calculation",
             "_htpd", "PV_calc"):
    check(f"analysis/plotting removed from machine: {gone}", getattr(smu, gone, None) is None)
check("Keysight_HT_PotDep (analysis) removed from commands",
      "Keysight_HT_PotDep" not in measurement_list()
      and not hasattr(KeysightPicoProbotMachine, "Keysight_HT_PotDep"))
check("driver module does not import matplotlib/pv_param",
      not hasattr(kp, "plt") and not hasattr(kp, "PV_calc"))


# --------------------------------------------------------------------------
print()
passed = sum(1 for _, ok in _checks if ok)
print(f"RESULT: {passed}/{len(_checks)} checks passed")
sys.exit(0 if passed == len(_checks) else 1)
