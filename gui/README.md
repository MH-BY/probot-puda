# probot GUI  ⚠️ DEFERRED — currently not runnable

> **Status:** the repo moved to a **one-folder-per-edge** layout (ViPSA-style):
> each edge now carries its own `driver.py` and the shared `probot_drivers`
> package was removed. This GUI still imports `from probot_drivers import ...` in
> its shims, so **it will not run until it is re-wired**. The orchestrator it used
> (`probot_orchestrator.py`) has been moved into this folder so the GUI cluster
> stays together for that future work.
>
> **To revive it** (a follow-up task): change the three shims to load each edge's
> `driver.py` by path (e.g. `importlib.util.spec_from_file_location` against
> `../probot-keysight-pico/driver.py` and `../probot-stage/driver.py`), and change
> `main_tkinter.py`'s `from probot_drivers import probot_orchestrator` to the
> sibling `import probot_orchestrator`. No driver logic needs to change — only how
> the GUI locates it.

The original Tkinter GUI talks to both machines **in-process** (SMU+light and
stage), so a full cell scan runs locally here via `probot_orchestrator.run_scan`.

The three modules `keysight.py`, `pico.py`, `probebot.py` are thin compatibility
shims that preserve the GUI's original plugin contract:

- `keysight.py` → `KeysightPicoProbotMachine` (SMU + light + measurements)
- `pico.py` → `PicoProbot`
- `probebot.py` → `StageProbot`

## Run (once re-wired)

```bash
uv run python main_tkinter.py
```

Run from this directory so the shims (`import keysight` / `pico` / `probebot`)
resolve and the cwd-relative `Parameters/` folder is found.

## Notes

- Parameters are read/written in `./Parameters` (a working copy is shipped here);
  point `PARAM_DIR` elsewhere to share with an edge service.
- Connection settings honour the same env vars as the edges (`KEYSIGHT_ADDRESS`,
  `PICO_IP`, `PICO_ID`, `STAGE_PORT`); unset values fall back to `None`/`COM3`.
