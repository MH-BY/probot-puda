# probot GUI

The original Tkinter GUI, running on the shared `probot_drivers` package (no
fork). It talks to both machines **in-process** (SMU+light and stage), so a full
cell scan runs locally here via the shared `orchestrator_probot.run_scan`.

The three modules `keysight.py`, `pico.py`, `probebot.py` are thin compatibility
shims that preserve the GUI's original plugin contract while delegating to
`probot_drivers`:

- `keysight.py` → `SMUKeysightProbotMachine` (SMU + light + measurements)
- `pico.py` → `PicoProbot`
- `probebot.py` → `StageProbot`

## Run

```bash
uv sync                       # add --extra analysis for Keysight_HT_PotDep
uv run python main_tkinter.py
```

Run from this directory so the shims (`import keysight` / `pico` / `probebot`)
resolve and the cwd-relative `Parameters/` folder is found.

## Notes

- Parameters are read/written in `./Parameters` (a working copy is shipped here);
  point `PARAM_DIR` elsewhere to share with an edge service.
- Connection settings honour the same env vars as the edges (`KEYSIGHT_ADDRESS`,
  `PICO_IP`, `PICO_ID`, `STAGE_PORT`); unset values fall back to `None`/`COM3`.
