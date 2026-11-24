"""Clocks: wall time for live runs, a settable clock for backtests and tests."""

from __future__ import annotations

import time


class WallClock:
    """Real time, in whole epoch seconds."""

    def __call__(self) -> int:
        return int(time.time())


class SimClock:
    """A clock the backtest advances bar by bar."""

    def __init__(self, t: int = 0) -> None:
        self.t = int(t)

    def set(self, t: int) -> None:
        """Move the clock to ``t``."""
        self.t = int(t)

    def __call__(self) -> int:
        return self.t
