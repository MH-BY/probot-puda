# probot-drivers

Shared driver library for the **probot** platform. Imported by both the PUDA
edge service (`probot/main.py`) and the Tkinter GUI (via the `gui_shims`), so
there is a single source of truth for hardware control.

## Components

| Module | Class | Hardware |
|---|---|---|
| `probot_smu_keysight` | `SMUKeysightProbot` | Keysight SMU (PyVISA / SCPI) |
| `probot_pico` | `PicoProbot` | Pico G2V LED (Ethernet) |
| `probot_stage` | `StageProbot` | Ender 3-axis stage (serial) |
| `probot_measurement` | `ProbotMeasurement` | the ~23 `Keysight_*` measurement routines (mixin) |
| `probot_orchestrator` | `run_scan(...)` | shared cell-scan loop (move → probe → measure → unprobe) |
| `probot` | `Probot` | composite machine wiring all of the above |

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
