"""Local plotting for the GUI.

PUDA machine commands **return** their measured data as a list of record dicts
(``list[dict]``) and also save a CSV as a side effect. The GUI plots locally from
the returned records so the lab still gets live plots. Richer/analysis plots are
done agent-side (see ../skills-reference) from the same records.
"""

import logging

logger = logging.getLogger(__name__)


def plot_records(result) -> None:
    """Plot current vs time (or voltage) from a measurement's returned records.

    Args:
        result: the value returned by a measurement command — a ``list[dict]`` of
            per-point records (keys like ``Time (s)``, ``Voltage (V)``,
            ``Current (A)``). Non-list results (e.g. ``Keysight_Time_Gap``) are ignored.
    """
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return
    keys = result[0].keys()
    cur_key = next((k for k in keys if str(k).startswith("Current")), None)
    x_key = "Time (s)" if "Time (s)" in keys else ("Voltage (V)" if "Voltage (V)" in keys else None)
    if cur_key is None or x_key is None:
        return
    try:
        import matplotlib.pyplot as plt
    except Exception:
        logger.warning("matplotlib not available; skipping plot")
        return
    try:
        x = [r.get(x_key) for r in result]
        y = [r.get(cur_key) for r in result]
        fig, ax = plt.subplots(figsize=(8, 3))
        ax.plot(x, y, color="blue", alpha=0.6)
        ax.set_xlabel(x_key)
        ax.set_ylabel(cur_key)
        plt.show(block=False)
    except Exception:
        logger.exception("Failed to plot measurement result")


# Backwards-compatible alias (the GUI used to call plot_envelope).
plot_envelope = plot_records
