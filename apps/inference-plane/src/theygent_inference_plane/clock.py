"""Injectable clock seam.

Idle eviction and LRU ordering read time through this, so tests advance a
``ManualClock`` deterministically instead of calling ``time.sleep``.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic-ish seconds; only differences are meaningful."""
        ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """Test clock: time only moves when ``advance`` is called."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds
