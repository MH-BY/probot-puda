# probot-keysight-pico

PUDA edge service for the probot **Keysight SMU + Pico light**. The light is
co-located with the SMU (not a separate edge) because several measurements drive
the light inline during the SMU acquisition with sub-second timing.

The whole edge is one self-contained `driver.py` (Keysight SMU transport + Pico
light + the `KeysightPicoProbotMachine` that defines every measurement); `main.py`
imports it with `from driver import KeysightPicoProbotMachine`.

Primitives exposed: the measurement routines (`Keysight_*`), manual light control
(`light_on` / `light_off`), and `identify`. The stage is a separate edge service
(`probot-stage`); a full cell scan is orchestrated by PUDA calling the stage and
SMU primitives in sequence (`move_to_cell` → `probe` → measure → `unprobe`, then
`move_to_safeposition` once at the end).

## Setup (native, recommended on the Windows lab PC)

```bash
cp .env.example .env
# edit MACHINE_ID, NATS_SERVERS, KEYSIGHT_ADDRESS, PICO_IP/ID
uv sync
uv run python main.py   # or start_edge.bat
```

Requires a Windows VISA backend (NI-VISA or Keysight IO Libraries) for pyvisa,
plus the `g2vpico` package.

## Docker (Linux deployments only)

```bash
docker compose up -d --build
```

Avoid Docker on Windows for this service - USB/VISA passthrough into Windows
containers is unreliable; run natively instead.

## Notes

- `MPLBACKEND=Agg` is set in the container; export it too when running headless.
- Measurement settings come in as command arguments (with defaults baked into each
  `Keysight_*` signature); the edge does not read parameter CSVs. Only output data
  is written — to `./Data` (mounted by the compose file).
- Measurement primitives are synchronous and can run for many seconds/minutes. If
  the PUDA `EdgeRunner` dispatches on the asyncio loop, wrap calls in
  `asyncio.to_thread` so telemetry keeps flowing.
