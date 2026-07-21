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

import ast
import logging
import time

logger = logging.getLogger(__name__)


def _params_to_kwargs(params) -> dict:
    """Coerce a params payload to measurement keyword arguments.

    Measurements now take their settings as typed keyword arguments, so an item's
    ``params`` are passed via ``**kwargs``. Accepts a mapping ``{name: value}`` or a
    pandas ``Parameter, Value`` DataFrame (GUI style). String values are parsed with
    ``ast.literal_eval`` and integer-valued floats are normalised to ``int`` (so
    counts like ``no_of_pulses`` stay valid for ``range()``), matching how the
    original CSV parser typed values.
    """
    if params is None:
        return {}
    if hasattr(params, "iterrows"):  # pandas DataFrame with Parameter/Value columns
        items = [(r["Parameter"], r["Value"]) for _, r in params.iterrows()]
    elif hasattr(params, "items"):
        items = list(params.items())
    else:
        items = list(params)

    kwargs = {}
    for name, value in items:
        if isinstance(value, str):
            try:
                value = ast.literal_eval(value)
            except Exception:
                pass
        if isinstance(value, float) and not isinstance(value, bool) and value.is_integer():
            value = int(value)
        kwargs[str(name).strip()] = value
    return kwargs


def run_one_measurement(machine, item, cell_number, on_progress=lambda m: None):
    """Dispatch one measurement, passing its parameters as keyword arguments.

    Args:
        machine: object owning the measurement methods.
        item: ``{"measurement": str, "params": mapping | DataFrame | None}``.
        cell_number: 1-based cell number passed to the measurement method.
    """
    measurement = item["measurement"]
    kwargs = _params_to_kwargs(item.get("params"))
    fn = getattr(machine, measurement, None)
    if fn is None:
        on_progress(f"Error: measurement '{measurement}' not found")
        logger.error("Measurement '%s' not found on %r", measurement, machine)
        return None
    return fn(cell_number, **kwargs)


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
