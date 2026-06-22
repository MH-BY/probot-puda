"""Shared cell-scan orchestration for the probot platform.

The per-cell scan loop (move -> probe -> run measurement(s) -> unprobe, with
stop/pause control and an optional return-to-safe) used to live inside the GUI's
``regular_measurement_thread`` / ``custom_measurement_thread``. It is extracted
here so that **both** the PUDA edge service and the Tkinter GUI drive identical
scan logic.

Callers supply:

* ``machine`` - object exposing the measurement methods (the composite
  :class:`~probot_drivers.probot.Probot`, or the GUI's ``KeysightInstrument``
  shim) and a ``_param_file`` resolver,
* ``stage``   - object exposing ``cell_coordinates``/``move_to``/``probing``/
  ``unprobing``/``move_to_safeposition`` (the :class:`StageProbot` or its shim),
* control hooks ``should_stop`` / ``is_paused`` / ``on_progress`` (the GUI wires
  its ``stop_event.is_set`` / ``pause_event.is_set`` / ``print_to_output``; the
  edge service passes its own or the defaults).
"""

from __future__ import annotations

import csv
import logging
import time

logger = logging.getLogger(__name__)


def _write_param_csv(path, params) -> None:
    """Persist measurement parameters to ``path`` (``Parameter,Value`` columns).

    Accepts a pandas DataFrame (GUI style, written verbatim via ``to_csv``) or a
    plain mapping / iterable of ``(name, value)`` pairs (edge style).
    """
    if hasattr(params, "to_csv"):  # pandas DataFrame
        params.to_csv(path, index=False)
        return

    items = params.items() if hasattr(params, "items") else list(params)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Parameter", "Value"])
        for name, value in items:
            writer.writerow([name, value])


def run_one_measurement(machine, item, cell_number, on_progress=lambda m: None):
    """Write the item's parameters (if any), then dispatch one measurement.

    Args:
        machine: object owning the measurement methods + ``_param_file``.
        item: ``{"measurement": str, "params": DataFrame | mapping | None}``.
        cell_number: 1-based cell number passed to the measurement method.
    """
    measurement = item["measurement"]
    params = item.get("params")
    if params is not None:
        path = machine._param_file(f"parameter_{measurement}.csv")
        try:
            _write_param_csv(path, params)
        except Exception:
            logger.exception("Failed to write parameters for %s", measurement)

    fn = getattr(machine, measurement, None)
    if fn is None:
        on_progress(f"Error: measurement '{measurement}' not found")
        logger.error("Measurement '%s' not found on %r", measurement, machine)
        return None
    return fn(cell_number)


def run_scan(
    machine,
    stage,
    plan,
    *,
    cells,
    num_loops: int = 1,
    mode: str = "regular",
    should_stop=lambda: False,
    is_paused=lambda: False,
    on_progress=lambda m: None,
    return_to_safe=None,
    run_measurement=None,
) -> list[dict]:
    """Run a full cell scan.

    For each loop and each cell: move to the cell, probe, run every item in
    ``plan`` (writing its parameters first), then unprobe. Honors ``should_stop``
    (checked between every step) and ``is_paused`` (blocks until cleared). On
    completion, optionally returns the stage to its safe position.

    Args:
        machine: measurement host (Probot composite or GUI shim).
        stage: motion controller (StageProbot or shim).
        plan: ordered list of ``{"measurement": str, "params": ... | None}``.
        cells: iterable of 1-based cell numbers to visit.
        num_loops: how many times to repeat the whole cell list.
        mode: ``"regular"`` (auto return-to-safe) or ``"custom"`` (no auto return;
            the GUI prompts the user instead). Overridden by ``return_to_safe``.
        return_to_safe: force the return-to-safe behaviour; when ``None`` it is
            derived from ``mode``.
        run_measurement: optional ``callable(item, cell_number)`` used to run one
            measurement. When ``None``, the built-in dispatcher writes the item's
            parameters and calls ``machine.<measurement>(cell_number)``. The GUI
            passes its own callback so it keeps its multi-equipment plugin
            dispatch while sharing this loop.

    Returns:
        A list of ``{"loop", "cell", "measurement", "result"}`` records.
    """
    if return_to_safe is None:
        return_to_safe = mode != "custom"

    if run_measurement is None:
        def run_measurement(item, cell_number):
            return run_one_measurement(machine, item, cell_number, on_progress)

    cells = list(cells)
    coords = stage.cell_coordinates()
    results: list[dict] = []

    try:
        for loop_index in range(num_loops):
            if should_stop():
                break
            on_progress(f"--- Loop {loop_index + 1}/{num_loops} ---")

            for cell_number in cells:
                if should_stop():
                    break
                on_progress(f"Processing Cell {cell_number}")

                stage.move_to(coords[cell_number - 1])
                stage.probing()

                for item in plan:
                    if should_stop():
                        break
                    on_progress(f"  Running: {item['measurement']}")
                    result = run_measurement(item, cell_number)
                    results.append({
                        "loop": loop_index + 1,
                        "cell": cell_number,
                        "measurement": item["measurement"],
                        "result": result,
                    })
                    while is_paused():
                        on_progress("Measurement paused.")
                        time.sleep(1)

                stage.unprobing()

                while is_paused():
                    on_progress("Measurement paused.")
                    time.sleep(1)
    finally:
        if return_to_safe:
            on_progress("Moving to safe position...")
            try:
                stage.move_to_safeposition()
                on_progress("✓ Reached safe position")
            except Exception:
                logger.exception("Failed to return stage to safe position")

    return results
