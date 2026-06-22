"""GUI compatibility shim for the Pico light.

The GUI does ``import pico`` then ``pico.PicoInstrument()`` (no args) and calls
``light_on()`` / ``light_off()``. This shim subclasses the shared
:class:`~probot_drivers.pico_probot.PicoProbot` (which carries those methods) and
connects on construction, reproducing the original behaviour.

Unlike the original ``pico.py``, there is **no** module-level singleton / network
call at import time - that side effect is gone, so importing this module is safe
with no hardware present.
"""

import os

from probot_drivers import PicoProbot


class PicoInstrument(PicoProbot):
    """No-arg Pico adapter for the GUI (connects on construction)."""

    def __init__(self):
        super().__init__(
            ip=os.environ.get("PICO_IP", "169.254.182.113"),
            device_id=os.environ.get("PICO_ID", "00000000e11c66da"),
        )
        self.startup()
