"""GUI compatibility shim for the ProbeBot stage.

The GUI does ``from probebot import ProbeBot`` then ``ProbeBot()`` (no args) and
calls ``cell_coordinates`` / ``move_to`` / ``probing`` / ``unprobing`` /
``move_to_cell1`` / ``move_to_cell81`` / ``move_to_safeposition``.

This shim subclasses the shared :class:`~probot_drivers.probot_stage.StageProbot`
(which carries all of those method names, including the ``probing``/``unprobing``
aliases) and connects on construction, reproducing the original behaviour.
Unlike the original ``probebot.py``, no hardware work happens at import time.
"""

import os

from probot_drivers import StageProbot


class ProbeBot(StageProbot):
    """No-arg stage adapter for the GUI (connects on construction)."""

    def __init__(self):
        super().__init__(port=os.environ.get("STAGE_PORT", "COM3"))
        self.startup()
