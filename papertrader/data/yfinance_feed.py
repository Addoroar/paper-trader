"""Stocks/ETFs via yfinance (free, no API key)."""

from __future__ import annotations

import pandas as pd

from papertrader.data.base import DataFeed, empty_bars, normalize
from papertrader.models import TIMEFRAME_SECONDS

# How far back Yahoo serves each interval (days); requests are clamped to this.
MAX_HISTORY_DAYS = {"1m": 29, "5m": 59, "15m": 59, "1h": 729, "1d": 365 * 30}
CHUNK_DAYS = {"1m": 7, "5m": 59, "15m": 59, "1h": 729, "1d": 365 * 30}


class YFinanceFeed(DataFeed):
    """Fetch bars with ``yfinance.Ticker.history``."""

    name = "yfinance"

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        import time

        import yfinance as yf

        now = int(time.time())
        start = max(start, now - MAX_HISTORY_DAYS[timeframe] * 86400)
        if start > end:
            return empty_bars()
        chunk = CHUNK_DAYS[timeframe] * 86400
        frames = []
        t = start
        while t <= end:
            t2 = min(end + TIMEFRAME_SECONDS[timeframe], t + chunk)
            raw = yf.Ticker(symbol).history(
                start=pd.Timestamp(t, unit="s", tz="UTC"),
                end=pd.Timestamp(t2, unit="s", tz="UTC"),
                interval=timeframe,
                auto_adjust=True,
                actions=False,
                raise_errors=True,
            )
            if raw is not None and not raw.empty:
                raw = raw.rename(columns=str.lower)
                if timeframe == "1d":
                    idx = raw.index.tz_localize(None) if raw.index.tz is not None else raw.index
                    idx = pd.DatetimeIndex(idx).tz_localize("America/New_York")
                else:
                    idx = raw.index if raw.index.tz is not None else raw.index.tz_localize("UTC")
                raw.index = (idx.tz_convert("UTC").as_unit("s").asi8).astype("int64")
                frames.append(raw)
            t = t2
        if not frames:
            return empty_bars()
        df = normalize(pd.concat(frames))
        return df[(df.index >= start) & (df.index <= end)]
