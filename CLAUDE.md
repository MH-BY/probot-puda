# CLAUDE.md — probot-puda

Guidance for AI agents (and humans) continuing this work. Read this first.

## What this is

`probot-puda` makes the **probot** lab instrument platform compatible with
[PUDA](https://github.com/PUDAP/puda), following the conventions of the reference
`Vipsa-platform-example`. The probot controls three instruments:

- **Keysight SMU** (source-measure unit) — PyVISA / SCPI
- **Pico G2V LED** light source — Ethernet (`g2vpico`)
- **Ender 3-axis "ProbeBot" stage** — serial COM port (`controllably`)

It runs as **two PUDA edge services** plus the original Tkinter **GUI**, all on a
single **shared driver library** so there is one source of truth (no fork).

The original (pre-PUDA) source lives in a sibling folder `../probot-source/`
(`keysight.py`, `pico.py`, `probebot.py`, `main_tkinter_6_v3.py`, `HT_PotDep.py`,
`PV_param_calculation.py`, `Parameters/`). That is the **provenance** for this
package — consult it when in doubt about original behaviour.

## Architecture

| Member | Machine id | Hardware | Driver class |
|---|---|---|---|
| `probot-smu-keysight/` | `probot-smu-keysight` | Keysight SMU + Pico light | `SMUKeysightProbotMachine` |
| `probot-stage/` | `probot-stage` | Ender 3-axis stage | `StageProbot` |
| `gui/` | — | both, in-process | uses shims → shared drivers |
| `probot_drivers/` | — | shared library | (all of the below) |

A PUDA edge service = one process: load `.env` config → build the driver →
connect to NATS via `puda.EdgeNatsClient` / `EdgeRunner` → PUDA reflects the
driver's **public methods** as callable **primitives** + publishes telemetry.
Each `main.py` mirrors `../Vipsa-platform-example/keithley-2450/main.py`.

```
probot-puda/
├── pyproject.toml              # uv workspace: members = probot-smu-keysight, probot-stage, gui, probot_drivers
├── start_all_edges.bat         # launches both edges (Windows)
├── tests/verify.py             # hardware-free verification (run with python tests/verify.py)
├── probot_drivers/             # SHARED library (installable, src-layout)
│   └── src/probot_drivers/
│       ├── __init__.py             # LAZY (PEP 562) — importing the package pulls no heavy deps
│       ├── probot_smu_keysight.py  # SMUKeysightProbot  — raw PyVISA session (transport only)
│       ├── probot_pico.py          # PicoProbot         — Pico G2V light
│       ├── probot_stage.py         # StageProbot        — Ender stage (also the stage edge's machine)
│       ├── probot_measurement.py   # ProbotMeasurement  — the ~22 Keysight_* routines (VERBATIM port)
│       ├── probot_machine_smu.py   # SMUKeysightProbotMachine — composes SMU+light+measurements
│       ├── probot_orchestrator.py  # run_scan() — the shared cell-scan loop
│       ├── analysis/ht_potdep.py   # Bayesian-opt fitting (torch/botorch; lazy import)
│       ├── analysis/pv_param.py    # PV J-V parameter extraction
│       └── parameters/*.csv        # packaged default measurement parameters
├── probot-smu-keysight/        # edge 1 (main.py + ViPSA scaffold + working Parameters/)
├── probot-stage/               # edge 2 (main.py + ViPSA scaffold)
└── gui/                        # keysight.py / pico.py / probebot.py shims + main_tkinter.py + Parameters/
```

## Key design decisions — and WHY (do not undo without reason)

1. **Two edges, not one composite machine.** The Keysight SMU and Pico light are
   **co-located in one edge** (`SMUKeysightProbotMachine`) because six measurements
   drive the light *inline during* the SMU acquisition with sub-second timing
   (`Keysight_Light_Pulse`, `Keysight_Voc_decay`, `Keysight_Voc_profile`,
   `Keysight_Jsc_profile`, `Keysight_Voc_decay_indiv_soaking`,
   `Keysight_Voc_decay_ON_OFF_Variation`). Splitting SMU and light into separate
   processes would break that timing. The **stage is independent**, so it is its
   own lean edge. (The user explicitly chose this 2-edge split.)

2. **A full cell scan spans both edges → PUDA orchestrates it.** No single edge
   can run a whole scan. `probot_orchestrator.run_scan()` is the **canonical
   sequence** (per cell: `move_to` → `probe` → run measurement(s) → `unprobe`,
   with stop/pause control + return-to-safe). The **GUI** uses it in-process (it
   holds both drivers); a PUDA-side recipe should replicate it by calling
   `probot-stage` move/probe primitives interleaved with `probot-smu-keysight`
   measurement primitives.

3. **Measurements are argument-based primitives** (as of the
   `feat/measurements-as-primitives` work). Each `Keysight_*` measurement takes
   its settings as **typed keyword arguments with defaults** (the defaults are the
   values from the corresponding `parameter_*.csv`), instead of reading a CSV.
   PUDA / an AI recipe can call e.g. `Keysight_JV_PV(cell_number=1, v_max=1.2)`
   directly. The measurement **bodies are otherwise the original
   `../probot-source/keysight.py` logic** (CSV-read preamble removed, `df_parameters`
   for saving rebuilt from the args via `_params_df`) — **still save CSVs + plots**
   and return the Dict envelope. Preserve the body logic; only the parameter
   interface changed.

4. **SCPI/data/save/plot helpers are private** (`_`-prefixed:
   `_make_voltage_pulses`, `_send_pulse_train_to_keysight`, `_string_to_dataframe`,
   `_savefile`, `_make_graph*`, `_Pot_Dep_Calculation`), so PUDA reflects only the
   real measurements + lifecycle + `light_on/off` as primitives — not plumbing.
   All measurements still live in ONE module (`probot_measurement.py`).

5. **`self.smu` is the raw PyVISA resource; `self.pico_instrument` aliases the
   light.** `SMUKeysightProbotMachine` exposes `smu` as a `@property` returning
   `self._smu.smu`, and sets `self.pico_instrument = self.light`, so the ported
   routines (which call `self.smu.write(...)` and `self.pico_instrument.light_on()`)
   run unchanged.

6. **Connect in `startup()`, never in `__init__`/at import.** Every driver's
   constructor only stores config; hardware libs (`pyvisa`, `g2vpico`,
   `controllably`) are imported lazily inside `startup()`. This keeps the package
   importable and unit-testable with **no hardware and no network**. The original
   `pico.py` connected + called `light_off()` at import — that side effect was
   removed.

7. **Lazy package `__init__` (PEP 562) + dependency extras.** Importing
   `probot_drivers` pulls nothing heavy; names load on access. Deps are split into
   extras: `stage` (pyserial, controllably), `smu` (pyvisa, g2vpico, numpy, pandas,
   scipy, matplotlib), `analysis` (torch, botorch, gpytorch, seaborn). The stage
   edge depends on `probot-drivers[stage]` only, so it stays lean.

8. **Measurements return a uniform `Dict[str, Any]` envelope** via the
   `_measurement_result` decorator (in `probot_measurement.py`) — `{measurement,
   cell_number, outputs, result}`, where `outputs` is the list of saved-file
   records collected by `_savefile`/`_savefile_1` (`_record_output`). The decorator
   wraps the bodies. Because measurements now return an envelope, any *internal*
   measurement-to-measurement call must take `["result"]` — see `Keysight_HT_PotDep`,
   which drives `Keysight_Potent_Depress_2` by rewriting
   `parameter_Keysight_Potent_Depress_2.csv` (for the BO interchange) then reading
   it back as kwargs via `_read_params` and taking `["result"]`.

9. **Orchestrator/GUI pass params as kwargs.** `probot_orchestrator._params_to_kwargs`
   coerces a params mapping (or a GUI `Parameter/Value` DataFrame) to kwargs,
   parsing strings and normalising integer-valued floats to `int`; both
   `run_one_measurement` and the GUI's `execute_measurement` call
   `machine.<measurement>(cell_number, **kwargs)` (no CSV writing).

10. **Configurable, writable `param_dir`/`data_dir`.** `Keysight_HT_PotDep` and the
   GUI **rewrite** parameter CSVs at runtime, so the parameter dir must be writable.
   Resolved via constructor args / `PROBOT_PARAM_DIR` / `PROBOT_DATA_DIR`, default
   to the packaged `parameters/` and `./Data/Keysight`. `_param_file` has a
   **case-insensitive fallback** (the source mixes `Voltage_Steady` vs
   `voltage_steady`).

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
  smu-keysight-probot.Keysight_JV_PV(cell_number=n, v_min=-0.5, v_max=1.0)   # other params default
  stage-probot.unprobe()                 # runs after the measurement returns
stage-probot.move_to_safeposition()      # once, at the very end
```

Key points that make this reliable and correct:
- **"Finished?" is implicit.** Measurements are synchronous — the `Keysight_JV_PV`
  call returns (with the data envelope) only when the sweep is done — so the next
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
# per edge (from its folder):
cp .env.example .env          # set MACHINE_ID, NATS_SERVERS, KEYSIGHT_ADDRESS, PICO_IP/ID, STAGE_PORT
uv sync                       # smu edge: add --extra analysis for Keysight_HT_PotDep
uv run python main.py         # or start_edge.bat ; or start_all_edges.bat at the root

# GUI (from gui/):
uv sync && uv run python main_tkinter.py
```

The SMU edge needs a Windows VISA backend (NI-VISA / Keysight IO Libraries) +
`g2vpico`; the stage edge needs `controllably` + `pyserial`. Set `MPLBACKEND=Agg`
when running headless (the measurement routines import `matplotlib.pyplot`).

## Verify (no hardware / no network needed)

```bash
python tests/verify.py     # 49 checks
```

`tests/verify.py` installs lightweight stubs for the heavy/hardware libs in
`sys.modules` before importing, then checks import-safety, construction without
hardware, primitive reflection per edge, the orchestrator's call order / control
hooks, the GUI plugin contract, and the measurement output envelope. It does NOT
execute real measurement bodies (those need real numpy + hardware).

## Pending / TODO for future agents

- **`uv lock` + real `uv sync`** were never run here (no network/uv in the build
  env). Run them on a machine with internet. Confirm **`g2vpico`** and
  **`controllably`** resolve on PyPI; if vendored, add them as `path`/`git` deps in
  `probot_drivers/pyproject.toml`. Then add `--frozen` back to the Dockerfiles.
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
  `probot_machine_smu.py` makes the advertised name resolve. (Pre-existing source
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
