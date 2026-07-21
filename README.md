# probot-puda

PUDA-compatible packaging of the **probot** instrument platform (Keysight SMU +
Pico G2V LED + Ender 3-axis "ProbeBot" stage).

The platform is exposed as **two** edge services, in a **one-folder-per-edge**
layout (ViPSA-style): each edge is self-contained, with its own `driver.py` and no
shared library.

| Member | Machine id | Hardware | Notes |
|---|---|---|---|
| `probot-keysight-pico` | `probot-keysight-pico` | Keysight SMU + Pico light | co-located: several measurements drive the light inline during the SMU sweep. All in one `driver.py`. |
| `probot-stage` | `probot-stage` | Ender 3-axis stage | independent, lean edge (`driver.py`) |

Each `main.py` imports its driver with a sibling `from driver import …` (run from
inside the edge folder), exactly like ViPSA's edges.

A full cell scan spans both machines, so **PUDA orchestrates the loop** by calling
`probot-stage`'s move/probe primitives interleaved with `probot-keysight-pico`'s
measurement primitives: `move_to_cell` → `probe` → measure → `unprobe`, then
`move_to_safeposition` once at the end.

> The original Tkinter **GUI** (`gui/`) is **deferred**: it ran on a shared driver
> library that no longer exists and needs re-wiring to import each edge's
> `driver.py` before it will run again. See `gui/README.md`.

## Where to run

The hardware lives on the Windows lab PC. Run the edges **natively** there (uv +
`start_all_edges.bat`); avoid Docker on Windows (serial/USB/VISA passthrough into
Windows containers is unreliable). Docker is for Linux deployments. You can edit
the repo on any OS — the drivers are import-safe without hardware.

## Run the edge services (Windows lab PC)

```bash
# per edge:
cd probot-keysight-pico   # or probot-stage
cp .env.example .env      # edit MACHINE_ID, NATS_SERVERS, addresses
uv sync
uv run python main.py
# ...or launch both at once from the workspace root:
start_all_edges.bat
```

The SMU edge needs a Windows VISA backend (NI-VISA / Keysight IO Libraries) and
the `g2vpico` package; the stage edge needs `controllably` + `pyserial`.

## Verify (no hardware needed)

```bash
python tests/verify.py
```
