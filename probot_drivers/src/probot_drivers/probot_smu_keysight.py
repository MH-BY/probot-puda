"""Keysight SMU transport for the probot platform.

Refactor of the connection half of the original ``keysight.py``
``KeysightInstrument.__init__``. Responsibilities here are deliberately narrow:
open / hold / close the PyVISA session. The measurement logic lives in
:mod:`probot_drivers.probot_measurement` (a mixin on the composite ``Probot``),
which reaches the raw resource through this object's :attr:`smu` attribute.

Changes versus the original:

* **No work in ``__init__``** — the VISA session opens in :meth:`startup`, so the
  object is importable / constructable with no instrument attached.
* **Explicit address.** The original grabbed ``list_resources()[0]`` (the first
  VISA resource found, which is brittle when several instruments are present).
  An explicit ``address`` is preferred; ``device_no`` indexing remains as a
  fallback when no address is configured (mirrors the Vipsa Keithley driver).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SMUKeysightProbot:
    """Own the PyVISA session for the probot Keysight source-measure unit."""

    instrument_family = "keysight_smu_probot"

    def __init__(self, address: str | None = None, device_no: int = 0) -> None:
        """Store connection config (does not connect).

        Args:
            address: VISA resource string (e.g. ``"USB0::0x0957::...::INSTR"``).
                When ``None``, :meth:`startup` falls back to the ``device_no``-th
                resource returned by the VISA resource manager.
            device_no: Index into ``list_resources()`` used only when ``address``
                is not given.
        """
        self.address = address
        self.device_no = device_no
        self.rm = None
        self.smu = None  # raw pyvisa resource; consumed by the measurement mixin

    @property
    def is_connected(self) -> bool:
        """Return True once :meth:`startup` has opened the VISA session."""
        return self.smu is not None

    def startup(self) -> bool:
        """Open the VISA session to the SMU."""
        if self.is_connected:
            return True
        try:
            import pyvisa

            self.rm = pyvisa.ResourceManager()
            address = self.address
            if address is None:
                resources = list(self.rm.list_resources())
                if not resources:
                    raise RuntimeError("No VISA resources found")
                address = resources[self.device_no]
            self.address = address
            self.smu = self.rm.open_resource(address)
            logger.info("Connected to Keysight SMU at %s", address)
            return True
        except Exception:
            logger.exception("Failed to connect to Keysight SMU (address=%s)", self.address)
            self.smu = None
            return False

    def identify(self) -> str:
        """Return the SMU ``*IDN?`` string."""
        return self.smu.query("*IDN?")

    def shutdown(self) -> bool:
        """Turn the output off (best effort) and close the VISA session."""
        try:
            if self.is_connected:
                try:
                    self.smu.write(":OUTP OFF")
                except Exception:
                    logger.exception("Error turning SMU output off during shutdown")
                self.smu.close()
        finally:
            self.smu = None
        return True
