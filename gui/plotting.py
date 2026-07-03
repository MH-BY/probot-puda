"""Local plotting for the GUI.

PUDA machine commands return raw data and no longer plot (plotting/analysis moved
to the agent side — see ../skills-reference). The GUI plots locally from the raw
data in a measurement's return envelope so the lab still gets live plots.

This is a simple current-vs-time (or current-vs-voltage) plot that works for any
measurement. Richer, measurement-specific plots (IV per cycle, PV overlays, etc.)
can be rebuilt here or done agent-side from the same raw data.
"""

import logging

logger = logging.getLogger(__name__)


def plot_envelope(envelope) -> None:
    """Plot each saved raw table from a measurement result envelope (non-blocking).

    Args:
        envelope: the dict returned by a measurement command, i.e.
            ``{"measurement", "cell_number", "outputs": [{"file","keyword","data"}], ...}``.
    """
    if not isinstance(envelope, dict):
        return
    try:
        import matplotlib.pyplot as plt
    except Exception:
        logger.warning("matplotlib not available; skipping plot")
        return
    for rec in envelope.get("outputs", []) or []:
        data = rec.get("data")
        if isinstance(data, dict):
            _plot_one(plt, data, str(rec.get("keyword", "")))


def _plot_one(plt, data: dict, title: str) -> None:
    cur_key = next((k for k in data if str(k).startswith("Current")), None)
    if cur_key is None:
        return
    x_key = "Time (s)" if "Time (s)" in data else ("Voltage (V)" if "Voltage (V)" in data else None)
    if x_key is None:
        return
    try:
        fig, ax = plt.subplots(figsize=(8, 3))
        ax.plot(data[x_key], data[cur_key], color="blue", alpha=0.6)
        ax.set_xlabel(x_key)
        ax.set_ylabel(cur_key)
        ax.set_title(title)
        plt.show(block=False)
    except Exception:
        logger.exception("Failed to plot %s", title)
