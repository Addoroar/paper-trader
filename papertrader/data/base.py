"""The ``DataFeed`` interface all market data sources implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

import numpy as np
import pandas as pd

from papertrader.data.market_hours import bar_close_times
from papertrader.models import BAR_COLUMNS, TIMEFRAME_SECONDS, is_crypto

EventCallback = Callable[..., None]


def empty_bars() -> pd.DataFrame:
    """An empty bar frame with the standard columns."""
    df = pd.DataFrame(columns=BAR_COLUMNS, dtype=float)
    df.index = pd.Index([], dtype="int64", name="ts")
    return df


def lookback_seconds(symbol: str, tf_seconds: int, bars: int) -> int:
    """Wall-clock span that contains roughly ``bars`` bars (stocks only trade ~6.5h on weekdays)."""
    if is_crypto(symbol):
        return bars * tf_seconds + tf_seconds
    if tf_seconds >= 86400:
        return int(bars * 86400 * 1.5) + 10 * 86400
    per_day = max(1, int(6.5 * 3600 / tf_seconds))
    return int(bars / per_day * 86400 * 1.5) + 10 * 86400


class DataFeed(ABC):
    """Source of OHLCV bars.

    Implementations only need :meth:`fetch_bars`. :meth:`get_bars` adds the
    no-lookahead guarantee: it returns only bars that have *closed* by ``now``.
    """

    name = "base"

    def __init__(self) -> None:
        self.on_event: EventCallback | None = None

    def emit(self, type_: str, message: str, **kw) -> None:
        """Report a data event (fetch error, retry) to the engine's event log."""
        if self.on_event:
            self.on_event(type_, message, **kw)

    @abstractmethod
    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        """Bars whose open time is in [start, end] (may include a still-forming last bar).

        Returns a frame indexed by bar open time (epoch seconds, int) with
        columns ``open, high, low, close, volume``, sorted ascending.
        """

    def get_bars(self, symbol: str, timeframe: str, now: int, limit: int) -> pd.DataFrame:
        """The last ``limit`` bars that closed at or before ``now``."""
        tf = TIMEFRAME_SECONDS[timeframe]
        start = now - lookback_seconds(symbol, tf, limit)
        df = self.fetch_bars(symbol, timeframe, start, now)
        return closed_only(df, symbol, tf, now).iloc[-limit:]


def closed_only(df: pd.DataFrame, symbol: str, tf_seconds: int, now: int) -> pd.DataFrame:
    """Drop bars that have not closed by ``now`` (the core lookahead guard)."""
    if df.empty:
        return df
    closes = bar_close_times(symbol, df.index.to_numpy(), tf_seconds)
    return df[closes <= now]


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a frame to the standard shape: int index ``ts``, float columns, sorted, unique."""
    if df.empty:
        return empty_bars()
    df = df[BAR_COLUMNS].astype(float)
    df.index = pd.Index(np.asarray(df.index, dtype="int64"), name="ts")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["open", "high", "low", "close"])
