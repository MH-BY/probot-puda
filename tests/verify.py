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
from probot_drivers import SMUKeysightProbotMachine, StageProbot, measurement_list, orchestrator_probot

import keysight   # gui shim
import pico       # gui shim
import probebot   # gui shim
check("import probot_drivers + shims", True)


# --------------------------------------------------------------------------
# 2. Both edge machines construct without touching hardware.
# --------------------------------------------------------------------------
print("2. construction without hardware")
smu = SMUKeysightProbotMachine()
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
results = orchestrator_probot.run_scan(
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
orchestrator_probot.run_scan(
    None, fstage2, [{"measurement": "m"}],
    cells=[1, 2, 3], num_loops=1, mode="regular",
    should_stop=lambda: True,
)
check("should_stop halts before any cell", [e[0] for e in fstage2.events] == ["safe"])

# custom mode does not auto-return to safe
fstage3 = FakeStage()
orchestrator_probot.run_scan(
    None, fstage3, [{"measurement": "m"}],
    cells=[1], num_loops=1, mode="custom",
    run_measurement=lambda item, cell: None,
)
check("custom mode skips auto return-to-safe", "safe" not in [e[0] for e in fstage3.events])

# built-in dispatch path (machine-based), with parameter writing
import tempfile
import csv as _csv

tmp = Path(tempfile.mkdtemp())


class FakeMachine:
    def __init__(self):
        self.called = []

    def _param_file(self, name):
        return str(tmp / name)

    def Keysight_JV_PV(self, cell):
        self.called.append(cell)
        return {"cell": cell}


machine = FakeMachine()
fstage4 = FakeStage()
orchestrator_probot.run_scan(
    machine, fstage4,
    [{"measurement": "Keysight_JV_PV", "params": {"v_max": 1.0, "compliance": 100}}],
    cells=[5], num_loops=1, mode="regular",
)
check("built-in dispatch called machine method", machine.called == [5])
written = tmp / "parameter_Keysight_JV_PV.csv"
check("params written to CSV", written.exists())
rows = list(_csv.reader(open(written)))
check("param CSV header + rows", rows[0] == ["Parameter", "Value"] and ["v_max", "1.0"] in rows)


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
print("7. measurement output envelope")
import inspect
from typing import Any, Dict
from probot_drivers.measurement_probot import _measurement_result, MeasurementProbot


class _Dummy(MeasurementProbot):
    def __init__(self):
        self._param_dir = "."
        self._data_dir = "."

    @_measurement_result
    def fake(self, cell_number):
        self._record_output("f.csv", "kw", None)
        return None


env = _Dummy().fake(7)
check("envelope keys", set(env) == {"measurement", "cell_number", "outputs", "result"})
check("envelope cell_number", env["cell_number"] == 7)
check("envelope records saved outputs",
      env["outputs"] == [{"file": "f.csv", "keyword": "kw", "data": None}])

jvpv = SMUKeysightProbotMachine.Keysight_JV_PV
check("measurement is decorated (wrapped)", hasattr(jvpv, "__wrapped__"))
check("measurement annotated -> Dict[str, Any]",
      inspect.signature(jvpv).return_annotation == Dict[str, Any])
check("measurement cell_number annotated int",
      inspect.signature(jvpv).parameters["cell_number"].annotation is int)


# --------------------------------------------------------------------------
print()
passed = sum(1 for _, ok in _checks if ok)
print(f"RESULT: {passed}/{len(_checks)} checks passed")
sys.exit(0 if passed == len(_checks) else 1)
