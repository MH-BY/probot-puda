# skills-reference

Post-processing code that used to run inside the probot measurement commands.
Per the PUDA design, **machine commands are pure instrument I/O that return raw
data** — analysis and plotting are done on the **AI-agent side (Hermes)**, which
receives the raw data. This folder holds the original analysis code as a
**reference for building those agent skills**. It is **not imported by the edge
services or the drivers**.

- `pv_param.py` — photovoltaic J-V parameter extraction (Voc, Jsc, FF, PCE,
  Rshunt, Rseries) from a J-V sweep DataFrame. Formerly used by `Keysight_JV_PV`.
- `ht_potdep.py` — potentiation/depression fitting + Bayesian optimization
  (torch/botorch/gpytorch). Formerly the `Keysight_HT_PotDep` command; this whole
  workflow (LHS sweep → measure → fit → BO → repeat) belongs on the agent side,
  orchestrating the `Keysight_Potent_Depress_2` command.

These operate on the raw data returned by the machine commands (the measurement
envelope's `outputs[].data`, i.e. columns like `Voltage (V)`, `Current (A)`,
`Time (s)`, `Cycle`).
