"""Replay feed for backtests: preloads history, then serves it bar by bar.

``get_bars(now=t)`` returns only bars that had closed by ``t``, exactly like a
live feed would at that moment, so the engine cannot see the future.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.data.base import DataFeed, empty_bars, lookback_seconds
from papertrader.data.market_hours import bar_close_times
from papertrader.models import TIMEFRAME_SECONDS


class HistoricalFeed(DataFeed):
    """Serves preloaded bars as of a simulated time."""

    name = "historical"

    def __init__(self, source: DataFeed, timeframe: str) -> None:
        super().__init__()
        self.source = source
        self.timeframe = timeframe
        self.tf = TIMEFRAME_SECONDS[timeframe]
        self.frames: dict[str, pd.DataFrame] = {}
        self.closes: dict[str, np.ndarray] = {}

    def preload(self, symbols: list[str], start: int, end: int, warmup_bars: int) -> dict[str, int]:
        """Load [start - warm-up, end] for each symbol. Returns bar counts per symbol."""
        self.source.on_event = self.on_event
        counts = {}
        for sym in symbols:
            s0 = start - lookback_seconds(sym, self.tf, warmup_bars)
            try:
                df = self.source.fetch_bars(sym, self.timeframe, s0, end)
            except Exception as e:  # noqa: BLE001
                self.emit("data_fetch_error", f"could not load history for {sym}: {e}", level="ERROR", symbol=sym)
                df = empty_bars()
            self.load_frame(sym, df)
            counts[sym] = len(df)
        return counts

    def load_frame(self, symbol: str, df: pd.DataFrame) -> None:
        """Install a frame directly (used by tests)."""
        self.frames[symbol] = df
        self.closes[symbol] = bar_close_times(symbol, df.index.to_numpy(), self.tf)

    def timeline(self, start: int, end: int) -> list[int]:
        """All distinct bar-close times in [start, end] across symbols, ascending."""
        allc = [c[(c >= start) & (c <= end)] for c in self.closes.values() if len(c)]
        if not allc:
            return []
        return [int(x) for x in np.unique(np.concatenate(allc))]

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        df = self.frames.get(symbol)
        if df is None:
            return empty_bars()
        return df[(df.index >= start) & (df.index <= end)]

    def get_bars(self, symbol: str, timeframe: str, now: int, limit: int) -> pd.DataFrame:
        df = self.frames.get(symbol)
        if df is None or df.empty:
            return empty_bars()
        i = int(np.searchsorted(self.closes[symbol], now, side="right"))
        return df.iloc[max(0, i - limit) : i]
