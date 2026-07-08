# probot-drivers

Shared driver library for the **probot** platform. Imported by both the PUDA
edge service (`probot/main.py`) and the Tkinter GUI (via the `gui_shims`), so
there is a single source of truth for hardware control.

## Components

| Module | Class | Role |
|---|---|---|
| `probot_keysight` | `KeysightProbot` | Keysight SMU transport (PyVISA / SCPI) — held by the machine |
| `probot_pico` | `PicoProbot` | Pico G2V LED (Ethernet) — held by the machine |
| `probot_stage` | `StageProbot` / `ProbotStage` | Ender 3-axis stage — the `probot-stage` edge machine |
| `probot_machine_keysight_pico` | `KeysightPicoProbotMachine` | the `probot-keysight-pico` edge machine: composes SMU + light and defines all 21 `Keysight_*` measurement commands directly on the class |
| `probot_orchestrator` | `run_scan(...)` | shared cell-scan loop (move → probe → measure → unprobe) |

> PUDA exposes only public methods **defined directly** on a machine class, so the
> measurements live in `KeysightPicoProbotMachine`'s body (not a mixin).

`analysis/ht_potdep.py` (Bayesian optimization, needs the `analysis` extra) and
`analysis/pv_param.py` (PV parameter extraction) hold the post-processing.

## Design notes

- Constructors never touch hardware; call `startup()` to connect. This keeps the
  classes importable and unit-testable with no instruments present.
- The composite `Probot` exposes the raw VISA resource as `self.smu` and a
  `self.pico_instrument` alias so the measurement routines (ported verbatim from
  the original `keysight.py`) run unchanged.
- Parameter CSVs are read/written from a configurable directory (`param_dir`
  constructor arg / `PROBOT_PARAM_DIR`), defaulting to the packaged defaults.
