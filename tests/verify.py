"""Hardware-free verification for the probot-puda integration (2-edge layout).

Network access to PyPI is unavailable in this environment, so the heavy
scientific deps (numpy/pandas/scipy/matplotlib) and the hardware/vendor libs
(pyvisa/g2vpico/controllably) are replaced with lightweight stubs installed in
``sys.modules`` *before* importing ``probot_drivers``. The measurement *bodies*
are never executed here - these checks cover import-safety, construction without
hardware, PUDA primitive reflection for each edge machine, the shared
orchestrator's call order / control hooks, and the GUI plugin contract via the
shims.

Run: ``python tests/verify.py``  (exits non-zero on first failure).
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "probot_drivers" / "src"))
sys.path.insert(0, str(ROOT / "gui"))  # GUI shims live next to the GUI


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
# Tiny assert harness.
# --------------------------------------------------------------------------
_checks = []


def check(name, cond):
    _checks.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise AssertionError(name)


# --------------------------------------------------------------------------
# 1. Import safety (no hardware, no network).
# --------------------------------------------------------------------------
print("1. import safety")
import probot_drivers
from probot_drivers import KeysightPicoProbotMachine, StageProbot, measurement_list, probot_orchestrator

import keysight   # gui shim
import pico       # gui shim
import probebot   # gui shim
check("import probot_drivers + shims", True)


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
# 5. Orchestrator: call order, control hooks, return-to-safe.
# --------------------------------------------------------------------------
print("5. orchestrator")


class FakeStage:
    def __init__(self):
        self.events = []

    def cell_coordinates(self):
        return [[i, i, i] for i in range(81)]

    def move_to(self, pos):
        self.events.append(("move", pos[0]))

    def probing(self):
        self.events.append(("probe", None))

    def unprobing(self):
        self.events.append(("unprobe", None))

    def move_to_safeposition(self):
        self.events.append(("safe", None))


fstage = FakeStage()
ran = []
plan = [{"measurement": "Keysight_JV_PV"}, {"measurement": "Keysight_Voc_decay"}]
results = probot_orchestrator.run_scan(
    None, fstage, plan,
    cells=[1, 2], num_loops=1, mode="regular",
    run_measurement=lambda item, cell: ran.append((item["measurement"], cell)) or "ok",
)
kinds = [e[0] for e in fstage.events]
check("per-cell order move->probe->...->unprobe",
      kinds == ["move", "probe", "unprobe", "move", "probe", "unprobe", "safe"])
check("measurements run per cell", ran == [
    ("Keysight_JV_PV", 1), ("Keysight_Voc_decay", 1),
    ("Keysight_JV_PV", 2), ("Keysight_Voc_decay", 2)])
check("results recorded", len(results) == 4 and results[0]["cell"] == 1)

# stop hook halts the scan
fstage2 = FakeStage()
probot_orchestrator.run_scan(
    None, fstage2, [{"measurement": "m"}],
    cells=[1, 2, 3], num_loops=1, mode="regular",
    should_stop=lambda: True,
)
check("should_stop halts before any cell", [e[0] for e in fstage2.events] == ["safe"])

# custom mode does not auto-return to safe
fstage3 = FakeStage()
probot_orchestrator.run_scan(
    None, fstage3, [{"measurement": "m"}],
    cells=[1], num_loops=1, mode="custom",
    run_measurement=lambda item, cell: None,
)
check("custom mode skips auto return-to-safe", "safe" not in [e[0] for e in fstage3.events])

# built-in dispatch path (machine-based): params passed as kwargs, not CSV
class FakeMachine:
    def __init__(self):
        self.calls = []

    def Keysight_JV_PV(self, cell, **kwargs):
        self.calls.append((cell, kwargs))
        return {"cell": cell, **kwargs}


machine = FakeMachine()
fstage4 = FakeStage()
probot_orchestrator.run_scan(
    machine, fstage4,
    [{"measurement": "Keysight_JV_PV",
      "params": {"v_max": "1.2", "no_cycles": "5.0", "compliance": 100}}],
    cells=[5], num_loops=1, mode="regular",
)
cell, kw = machine.calls[0]
check("built-in dispatch passes cell number", cell == 5)
# str->literal, integer-valued float ("5.0") normalised to int, ints kept
check("params coerced to typed kwargs", kw == {"v_max": 1.2, "no_cycles": 5, "compliance": 100})

# _params_to_kwargs handles a Parameter/Value mapping and None
check("empty params -> no kwargs", probot_orchestrator._params_to_kwargs(None) == {})


# --------------------------------------------------------------------------
# 6. GUI plugin contract via shims.
# --------------------------------------------------------------------------
print("6. GUI contract")
import importlib

m = importlib.import_module("keysight")
check("keysight.measurement_list()", isinstance(m.measurement_list(), list) and m.measurement_list())
KI = getattr(m, "KeysightInstrument")
inst = KI()  # no-arg; connects SMU (stub fails gracefully) + light (stub ok)
check("KeysightInstrument() no-arg ok", inst is not None)
check("delegates measurement attr", callable(getattr(inst, "Keysight_JV_PV")))
check("delegates Digital_Retention alias", callable(getattr(inst, "Keysight_Digital_Retention")))
check("ProbeBot() constructs + connects", probebot.ProbeBot().is_connected)
check("pico.PicoInstrument() constructs", pico.PicoInstrument() is not None)


# --------------------------------------------------------------------------
# 7. Measurements: decorated, annotated -> Dict[str, Any], envelope return.
# --------------------------------------------------------------------------
print("7. measurement return contract")
import inspect
import typing
from typing import Any, Dict

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

# measurements now take their settings as typed kwargs WITH DEFAULTS (from the CSVs)
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

# SCPI/data/save helpers are PRIVATE -> not part of the PUDA primitive surface
print("8. helpers hidden from primitive surface")
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
import probot_drivers.probot_machine_keysight_pico as _pm
check("machine module does not import matplotlib/pv_param",
      not hasattr(_pm, "plt") and not hasattr(_pm, "PV_calc"))


# --------------------------------------------------------------------------
# 9. PUDA scan protocol: "JV for cells 1-15, V from -0.5 to 1" choreography.
# --------------------------------------------------------------------------
print("9. scan protocol (cells 1-15 JV with voltage override)")


class ProtoStage:
    def __init__(self):
        self.events = []

    def cell_coordinates(self):
        return [[i, i, i] for i in range(81)]

    def move_to(self, pos):
        self.events.append(("move", pos[0]))

    def probing(self):
        self.events.append(("probe", None))

    def unprobing(self):
        self.events.append(("unprobe", None))

    def move_to_safeposition(self):
        self.events.append(("safe", None))


class ProtoMachine:
    def __init__(self):
        self.jv_calls = []

    def Keysight_JV_PV(self, cell, **kw):
        self.jv_calls.append((cell, kw))
        return {"cell": cell}


pstage, pmachine = ProtoStage(), ProtoMachine()
probot_orchestrator.run_scan(
    pmachine, pstage,
    [{"measurement": "Keysight_JV_PV", "params": {"v_min": -0.5, "v_max": 1}}],
    cells=list(range(1, 16)), num_loops=1, mode="regular",
)
# per-cell move->probe->unprobe for 15 cells, then one safe at the end
expected_kinds = (["move", "probe", "unprobe"] * 15) + ["safe"]
check("protocol: move->probe->unprobe x15 then safe",
      [e[0] for e in pstage.events] == expected_kinds)
check("protocol: JV run on all 15 cells in order",
      [c for c, _ in pmachine.jv_calls] == list(range(1, 16)))
check("protocol: only v_min/v_max overridden, rest default",
      all(kw == {"v_min": -0.5, "v_max": 1} for _, kw in pmachine.jv_calls))


# --------------------------------------------------------------------------
print()
passed = sum(1 for _, ok in _checks if ok)
print(f"RESULT: {passed}/{len(_checks)} checks passed")
sys.exit(0 if passed == len(_checks) else 1)
