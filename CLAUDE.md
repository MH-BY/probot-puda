# CLAUDE.md — probot-puda

Guidance for AI agents (and humans) continuing this work. Read this first.

## What this is

`probot-puda` makes the **probot** lab instrument platform compatible with
[PUDA](https://github.com/PUDAP/puda), following the conventions of the reference
`Vipsa-platform-example`. The probot controls three instruments:

- **Keysight SMU** (source-measure unit) — PyVISA / SCPI
- **Pico G2V LED** light source — Ethernet (`g2vpico`)
- **Ender 3-axis "ProbeBot" stage** — serial COM port (`controllably`)

It runs as **two PUDA edge services** in a **one-folder-per-edge** layout
(ViPSA-style): each edge is self-contained, with its own `driver.py` and **no
shared library**. (Earlier the drivers lived in a shared `probot_drivers/`
package; that was flattened into the edges — see decision 1b.)

The original (pre-PUDA) source lives in a sibling folder `../probot-source/`
(`keysight.py`, `pico.py`, `probebot.py`, `main_tkinter_6_v3.py`, `HT_PotDep.py`,
`PV_param_calculation.py`, `Parameters/`). That is the **provenance** for this
package — consult it when in doubt about original behaviour.

## Architecture

| Member | Machine id | Hardware | Driver class (in `<edge>/driver.py`) |
|---|---|---|---|
| `probot-keysight-pico/` | `probot-keysight-pico` | Keysight SMU + Pico light | `KeysightPicoProbotMachine` |
| `probot-stage/` | `probot-stage` | Ender 3-axis stage | `ProbotStage` (alias `StageProbot`) |

A PUDA edge service = one process: load `.env` config → build the driver →
connect to NATS via `puda.EdgeNatsClient` / `EdgeRunner` → PUDA reflects the
driver's **public methods** as callable **primitives** + publishes telemetry.
Each `main.py` mirrors `../Vipsa-platform-example/keithley-2450/main.py` and
imports its driver with a sibling `from driver import …` (run from the edge folder).

```
probot-puda/
├── pyproject.toml              # uv workspace: members = probot-keysight-pico, probot-stage  (that's it)
├── start_all_edges.bat         # launches both edges (Windows)
├── tests/verify.py             # hardware-free verification (loads each edge's driver.py by path)
├── probot-keysight-pico/       # EDGE 1 — self-contained
│   ├── main.py                 #   from driver import KeysightPicoProbotMachine
│   ├── driver.py               #   KeysightProbot (SMU transport) + PicoProbot (light) +
│   │                           #   KeysightPicoProbotMachine (composes both; all 21 Keysight_*
│   │                           #   measurements defined directly on the class — see decision 3)
│   └── Dockerfile, compose.yml, .env.example, start_edge.bat, README.md
├── probot-stage/               # EDGE 2 — self-contained
│   ├── main.py                 #   from driver import ProbotStage
│   ├── driver.py               #   ProbotStage / StageProbot — Ender stage
│   └── Dockerfile, compose.yml, .env.example, start_edge.bat, README.md
├── skills-reference/           # analysis code (pv_param, ht_potdep) for Hermes agent skills
│                               #   (NOT imported by any edge)
└── gui/                        # DEFERRED Tkinter GUI (shims + main_tkinter.py + probot_orchestrator.py)
                                #   — imports the removed probot_drivers; needs re-wiring (gui/README.md)
```

## Key design decisions — and WHY (do not undo without reason)

1. **Two edges, not one composite machine.** The Keysight SMU and Pico light are
   **co-located in one edge** (`KeysightPicoProbotMachine`) because six measurements
   drive the light *inline during* the SMU acquisition with sub-second timing
   (`Keysight_Light_Pulse`, `Keysight_Voc_decay`, `Keysight_Voc_profile`,
   `Keysight_Jsc_profile`, `Keysight_Voc_decay_indiv_soaking`,
   `Keysight_Voc_decay_ON_OFF_Variation`). Splitting SMU and light into separate
   processes would break that timing. The **stage is independent**, so it is its
   own lean edge. (The user explicitly chose this 2-edge split.)

1b. **One folder per edge — no shared library** (ViPSA layout). Each edge's whole
   driver lives in its own `driver.py` and `main.py` imports it as a sibling
   (`from driver import …`), run from inside the edge folder. The former shared
   `probot_drivers/` package was flattened: `KeysightProbot` + `PicoProbot` + the
   machine now sit together in `probot-keysight-pico/driver.py`, and the stage in
   `probot-stage/driver.py`. Each edge's `pyproject.toml` lists its device deps
   directly (no extras). The trade-off the user accepted: the in-process **GUI**
   lost its shared drivers and is now **deferred** (see the GUI note below /
   `gui/README.md`). Do not reintroduce a shared library without reason.

2. **A full cell scan spans both edges → PUDA orchestrates it.** No single edge
   can run a whole scan. The **canonical sequence** is, per cell, `move_to_cell` →
   `probe` → run measurement(s) → `unprobe`, then `move_to_safeposition` once at
   the end. A PUDA-side recipe realises it by calling `probot-stage` move/probe
   primitives interleaved with `probot-keysight-pico` measurement primitives. (The
   reference implementation `run_scan()` — with stop/pause control + return-to-safe
   — now lives in `gui/probot_orchestrator.py`, moved there with the deferred GUI
   that was its only in-process caller.)

3. **Measurements are argument-based primitives** (as of the
   `feat/measurements-as-primitives` work). Each `Keysight_*` measurement takes
   its settings as **typed keyword arguments with defaults** (the defaults are the
   values from the corresponding `parameter_*.csv`), instead of reading a CSV.
   PUDA / an AI recipe can call e.g. `Keysight_JV_PV(cell_number=1, v_max=1.2)`
   directly. The measurement **bodies are otherwise the original
   `../probot-source/keysight.py` logic** (CSV-read preamble removed, `df_parameters`
   for saving rebuilt from the args via `_params_df`; the return records via
   `.to_dict(orient="records")`). Commands are pure instrument
   I/O: they **return their data as `list[dict]` records and also save a CSV** for
   the lab, but do **no analysis and no plotting** (see decisions 4b and 8).
   Preserve the sweep logic.

4. **PUDA exposes only PUBLIC methods DEFINED DIRECTLY on the machine class** —
   it does NOT reflect inherited methods (per docs.puda.co: *"Only methods defined
   on this driver wrapper class are exposed to PUDA"*). So all 21 `Keysight_*`
   measurements are defined **in the `KeysightPicoProbotMachine` class body** (not a
   mixin/base class) — that's why `probot_machine_keysight_pico.py` is one big self-contained
   class rather than a thin class + a `ProbotMeasurement` mixin. The
   SCPI/data/save helpers are `_`-prefixed (`_make_voltage_pulses`,
   `_send_pulse_train_to_keysight`, `_string_to_dataframe`, `_savefile`) so PUDA does
   not surface them. **If you
   add a measurement, define it on this class** (a test in `verify.py` asserts every
   `measurement_list()` name is in `KeysightPicoProbotMachine.__dict__`). The
   composed sub-drivers (`KeysightProbot`, `PicoProbot`) are held as attributes,
   which is fine — only the machine's own methods are commands.

4b. **Analysis + plotting are agent-side, NOT in the machine commands** (per the
   platform owner). A PUDA command is pure instrument I/O: run the sweep, save the
   raw CSV, return the `list[dict]` records. No `print`, no `matplotlib`, no PV-param
   extraction / pot-dep fitting / Bayesian optimization inside a command. That code
   now lives in `../skills-reference/` (`pv_param.py`, `ht_potdep.py`) as a reference
   for **Hermes agent skills** operating on the returned raw data. The
   `Keysight_HT_PotDep` command was removed for this reason (it was an
   analysis/optimization loop). The **GUI** does its own local plotting
   (`gui/plotting.py`) from the returned records. Don't reintroduce analysis or
   plotting into a command.

5. **`self.smu` is the raw PyVISA resource; `self.pico_instrument` aliases the
   light.** `KeysightPicoProbotMachine` exposes `smu` as a `@property` returning
   `self._smu.smu`, and sets `self.pico_instrument = self.light`, so the ported
   routines (which call `self.smu.write(...)` and `self.pico_instrument.light_on()`)
   run unchanged.

6. **Connect in `startup()`, never in `__init__`/at import.** Every driver's
   constructor only stores config; hardware libs (`pyvisa`, `g2vpico`,
   `controllably`) are imported lazily inside `startup()`. This keeps the package
   importable and unit-testable with **no hardware and no network**. The original
   `pico.py` connected + called `light_off()` at import — that side effect was
   removed.

7. **Per-edge deps, declared directly (no shared package, no extras).** Each edge's
   `pyproject.toml` lists exactly what it needs: the stage edge → pyserial +
   controllably (stays lean); the keysight-pico edge → pyvisa, g2vpico, numpy,
   pandas, scipy. No matplotlib/torch — plotting is a (deferred) GUI concern and
   analysis is agent-side. Heavy/hardware libs are still imported **lazily inside
   `startup()`** (decision 6), so each `driver.py` imports cleanly with no hardware
   — that is what makes `tests/verify.py` able to load both drivers with stubs.

8. **Measurements RETURN their data as `list[dict]` records** (ViPSA
   `Keithley2450` style — one dict per measured point, keys like `Time (s)`,
   `Voltage (V)`, `Current (A)`). The **AI agent only sees the return value, not
   files** — so the data must come back in the return. Each command also **saves a
   CSV as a side effect** (via `_savefile`) for the local lab workflow, but that is
   secondary. There is no wrapper/decorator and no file-centric envelope. To return
   records the body ends with `return <table>.to_dict(orient="records")` (multi-cycle
   commands accumulate records across cycles, e.g. `Keysight_JV_PV`). `Keysight_Time_Gap`
   returns a small `dict`.

9. **Orchestrator/GUI pass params as kwargs.** `probot_orchestrator._params_to_kwargs`
   coerces a params mapping (or a GUI `Parameter/Value` DataFrame) to kwargs,
   parsing strings and normalising integer-valued floats to `int`; both
   `run_one_measurement` and the GUI's `execute_measurement` call
   `machine.<measurement>(cell_number, **kwargs)` (no CSV writing).

10. **Only a writable `data_dir` — no parameter directory.** Because measurements
   take their settings as kwargs (decision 3), the machine never reads
   `parameter_*.csv` at runtime, so there is **no `param_dir`**. The old
   `_param_dir`/`_param_file` reader and the edge's `Parameters/` folder were
   removed. `data_dir` (where results are saved) is resolved via the constructor
   arg / `PROBOT_DATA_DIR`, default `./Data/Keysight`. The `parameter_*.csv` values
   survive only as the **defaults baked into each measurement's signature** (their
   provenance is `../probot-source/Parameters/`).

## Scan protocol (how PUDA sequences a request)

A full cell scan spans **both** edges, so PUDA's planner composes it from the two
machines' primitives (there is no single "scan" primitive — see decision 2). The
primitives and their docstrings are written so the planner produces the correct
choreography. Example — the request *"do JV measurement for cell 1 to 15 with
voltage from -0.5 V to 1 V"* should yield this protocol (steps run sequentially;
each waits for the previous to finish because primitives are synchronous):

```
for n in 1..15:
  stage-probot.move_to_cell(cell_number=n)
  stage-probot.probe()
  probot-keysight-pico.Keysight_JV_PV(cell_number=n, v_min=-0.5, v_max=1.0)   # other params default
  stage-probot.unprobe()                 # runs after the measurement returns
stage-probot.move_to_safeposition()      # once, at the very end
```

Key points that make this reliable and correct:
- **"Finished?" is implicit.** Measurements are synchronous — the `Keysight_JV_PV`
  call returns (with the record data) only when the sweep is done — so the next
  step (`unprobe`) naturally runs after completion. PUDA executes steps in order.
- **Only overrides are passed.** `v_min`/`v_max` come from the request; every other
  parameter uses its signature default. This is exactly why measurements take typed
  args with defaults.
- **The choreography is documented on the primitives** (`move_to_cell` → `probe` →
  measure → `unprobe`, then `move_to_safeposition`) so the planner inserts probe /
  unprobe / return-to-safe. If you add primitives, keep this contract in their
  docstrings.
- `cell_number` in the measurement only labels the saved data; the *stage* is what
  physically moves (on the other edge).

## Conventions

- **Naming:** probot-specific drivers carry a `*_probot` suffix (modules/classes)
  to avoid PUDA namespace collisions with other platforms on the same bus.
- **GUI shims keep the GUI's required names** (`keysight.py`/`KeysightInstrument`,
  `pico.py`/`PicoInstrument`, `probebot.py`/`ProbeBot`) and delegate to the
  `*_probot` drivers, so disambiguation never reaches the GUI. The GUI itself is
  the original, edited only to call `probot_orchestrator.run_scan` from its two
  measurement threads (it keeps its own plugin dispatch via the `run_measurement`
  callback).
- **Docstrings** follow the `../good-format-example/` style (summary, `Args:` with
  ranges/defaults, `Returns:` with a data schema). New code should match.
- **Type hints** on signatures, including return annotations.

## Run

Hardware is on a **Windows lab PC**. Run edges **natively** there (avoid Docker on
Windows — serial/USB/VISA passthrough is unreliable; Docker is for Linux only).
You can edit the repo on any OS (drivers are import-safe without hardware).

```bash
# per edge (from its folder — the sibling `from driver import …` needs cwd = edge dir):
cp .env.example .env          # set MACHINE_ID, NATS_SERVERS, KEYSIGHT_ADDRESS, PICO_IP/ID, STAGE_PORT
uv sync
uv run python main.py         # or start_edge.bat ; or start_all_edges.bat at the root
```

The SMU edge needs a Windows VISA backend (NI-VISA / Keysight IO Libraries) +
`g2vpico`; the stage edge needs `controllably` + `pyserial`. Set `MPLBACKEND=Agg`
when running headless (the measurement routines import `matplotlib.pyplot`).

The Tkinter **GUI** (`gui/`) is **deferred** — it needs re-wiring to import each
edge's `driver.py` before it will run (see `gui/README.md`).

## Verify (no hardware / no network needed)

```bash
python tests/verify.py     # 75 checks
```

`tests/verify.py` installs lightweight stubs for the heavy/hardware libs in
`sys.modules`, then **loads each edge's `driver.py` by file path** and checks
import-safety, construction without hardware, primitive reflection per edge, and
the measurement return / `_`-helper contract. It does NOT execute real measurement
bodies (those need real numpy + hardware). The orchestrator call-order and GUI
plugin-contract checks were dropped when the GUI was deferred.

## Pending / TODO for future agents

- **`uv lock` + real `uv sync`** were never run here (no network/uv in the build
  env). Run them on a machine with internet. Confirm **`g2vpico`** and
  **`controllably`** resolve on PyPI; if vendored, add them as `path`/`git` deps in
  each edge's `pyproject.toml`. Then add `--frozen` back to the Dockerfiles.
- **Re-wire or retire the GUI** (`gui/`): point its shims at the edges' `driver.py`
  files, or drop it. It currently imports the removed `probot_drivers`.
- **Real-hardware test**: measurement bodies and a live NATS run were not exercised.
- **Blocking primitives**: measurements are synchronous and can run for many
  seconds/minutes (VISA `*OPC?`, `time.sleep`, `smu.timeout` up to ~10000 s).
  Check how `puda==0.0.15`'s `EdgeRunner` dispatches primitives — if on the asyncio
  event loop, wrap measurement calls in `asyncio.to_thread` so telemetry/heartbeat
  keep flowing.
- **Python pinned `>=3.11,<3.13`** because of torch wheel availability.
- **Richer light primitives**: only `light_on`/`light_off` are exposed on the SMU
  machine; add `set_light_intensity` / manual `light_pulse` passthroughs if PUDA
  needs them.
- **Richer `Args:` docs**: per-measurement parameter keys could be filled in from
  each `parameter_*.csv` (only `Keysight_JV_PV` keys are known/listed so far).

## Gotchas

- **`Keysight_Digital_Retention`** is advertised by `measurement_list()` but the
  implementation method is named `Keysight_Digital_Endurance` (it reads
  `parameter_Keysight_Digital_Retention.csv`). A class-level alias in
  `probot_machine_keysight_pico.py` makes the advertised name resolve. (Pre-existing source
  quirk.)
- **`Keysight_Voltage_list`** in the original source has an over-indented (12-space)
  body — keep its docstring at the same indentation.
- **`matplotlib`** must use a non-interactive backend in headless/edge contexts
  (`MPLBACKEND=Agg`); otherwise `plt.show()` calls inside measurements misbehave.
- **Don't reintroduce import-time side effects** (network/file I/O at module import)
  — the package must import cleanly with no hardware.

## Reference material (siblings of this folder)

- `../Vipsa-platform-example/` — the PUDA edge-service pattern to mirror
  (especially `keithley-2450/main.py`, `driver.py`, root `pyproject.toml`).
- `../good-format-example/` — the preferred class/docstring style
  (`gistfile1-biologic.txt`, `gistfile1-machine.txt`).
- `../probot-source/` — the original probot code (provenance for the verbatim port).
