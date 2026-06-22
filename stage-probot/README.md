# stage-probot

PUDA edge service for the probot **Ender 3-axis "ProbeBot" stage**. A separate,
lean edge (only the `stage` driver extra: pyserial + controllably).

Primitives exposed: `cell_coordinates`, `move_to`, `move_to_cell`, `probe` /
`unprobe` (aka `probing`/`unprobing`), `move_to_cell1`, `move_to_cell81`,
`move_to_safeposition`, `home`. Publishes the live stage position as telemetry.

A full cell scan is orchestrated by PUDA calling this edge's move/probe primitives
interleaved with the `smu-keysight-probot` measurement primitives (reference
sequence: `probot_drivers.orchestrator_probot.run_scan`).

## Setup (native, recommended on the Windows lab PC)

```bash
cp .env.example .env
# edit MACHINE_ID, NATS_SERVERS, STAGE_PORT
uv sync
uv run python main.py   # or start_edge.bat
```

## Docker (Linux deployments only)

```bash
docker compose up -d --build
```

Avoid Docker on Windows for this service - COM-port passthrough into Windows
containers is unreliable; run natively instead.
