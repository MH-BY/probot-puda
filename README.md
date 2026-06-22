# probot-puda

PUDA-compatible packaging of the **probot** instrument platform (Keysight SMU +
Pico G2V LED + Ender 3-axis "ProbeBot" stage).

The platform is exposed as **two** edge services, plus the original
Tkinter GUI running on the *same* driver code:

| Member | Machine id | Hardware | Notes |
|---|---|---|---|
| `smu-keysight-probot` | `smu-keysight-probot` | Keysight SMU + Pico light | co-located: several measurements drive the light inline during the SMU sweep |
| `stage-probot` | `stage-probot` | Ender 3-axis stage | independent, lean edge |
| `gui` | — | both (in-process) | the Tkinter GUI on shared drivers |
| `probot_drivers` | — | — | shared driver library (one source of truth) |

A full cell scan spans both machines, so **PUDA orchestrates the loop** by calling
`stage-probot`'s move/probe primitives interleaved with `smu-keysight-probot`'s
measurement primitives. The canonical sequence is
`probot_drivers.orchestrator_probot.run_scan`, which the GUI also uses in-process.

## Where to run

The hardware lives on the Windows lab PC. Run the edges **natively** there (uv +
`start_all_edges.bat`); avoid Docker on Windows (serial/USB/VISA passthrough into
Windows containers is unreliable). Docker is for Linux deployments. You can edit
the repo on any OS — the drivers are import-safe without hardware.

## Run the edge services (Windows lab PC)

```bash
# per edge:
cd smu-keysight-probot   # or stage-probot
cp .env.example .env      # edit MACHINE_ID, NATS_SERVERS, addresses
uv sync                   # smu edge: add --extra analysis for Keysight_HT_PotDep
uv run python main.py
# ...or launch both at once from the workspace root:
start_all_edges.bat
```

The SMU edge needs a Windows VISA backend (NI-VISA / Keysight IO Libraries) and
the `g2vpico` package; the stage edge needs `controllably` + `pyserial`.

## Run the GUI (same drivers)

```bash
cd gui
uv sync
uv run python main_tkinter.py
```

## Verify (no hardware needed)

```bash
python tests/verify.py
```
