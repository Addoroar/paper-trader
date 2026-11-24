"""Deterministic offline price generator.

Used by tests, demos, and whenever the real data sources are unreachable
(``data.feed: synthetic``). Prices are a geometric random walk with slowly
rotating trend regimes, seeded per (symbol, timeframe), on the same bar grid
the real sources produce (stocks only during NYSE hours, crypto 24/7). The
series is prefix-stable: asking for a later end never changes earlier bars,
so live runs, restarts and backtests all see consistent history.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from papertrader.data.base import DataFeed, empty_bars
from papertrader.data.market_hours import stock_bar_grid
from papertrader.models import BAR_COLUMNS, TIMEFRAME_SECONDS, is_crypto

EPOCH = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
YEAR = 365 * 86400

_BASE_PRICES = {
    "AAPL": 190.0, "MSFT": 410.0, "NVDA": 480.0, "AMZN": 175.0, "GOOGL": 140.0, "TSLA": 240.0,
    "SPY": 470.0, "QQQ": 400.0, "BTC/USDT": 42000.0, "ETH/USDT": 2300.0, "SOL/USDT": 100.0,
}


def _seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


class SyntheticFeed(DataFeed):
    """Offline, deterministic OHLCV generator."""

    name = "synthetic"

    def __init__(self, seed: int = 7) -> None:
        super().__init__()
        self.seed = seed
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    def _grid(self, symbol: str, tf: int, end: int) -> np.ndarray:
        if is_crypto(symbol):
            return np.arange(EPOCH, end + 1, tf, dtype=np.int64)
        return np.asarray(stock_bar_grid(EPOCH, end + 1, tf), dtype=np.int64)

    def _generate(self, symbol: str, timeframe: str, end: int) -> pd.DataFrame:
        tf = TIMEFRAME_SECONDS[timeframe]
        ts = self._grid(symbol, tf, end)
        n = len(ts)
        if n == 0:
            return empty_bars()
        def stream(tag: str) -> np.ndarray:
            g = np.random.Generator(np.random.PCG64(_seed(self.seed, symbol, timeframe, tag)))
            return g.standard_normal(n)  # prefix-stable: draw i depends only on i

        sym_rng = np.random.Generator(np.random.PCG64(_seed(self.seed, symbol)))
        annual_vol = (0.55 if is_crypto(symbol) else 0.28) * (0.8 + 0.4 * sym_rng.random())
        period = (20 + 60 * sym_rng.random()) * 86400  # trend regime length in seconds
        phase = sym_rng.random() * 2 * np.pi
        bias = (sym_rng.random() - 0.35) * 0.3  # annual drift
        dt = tf / YEAR
        sigma = annual_vol * np.sqrt(dt)
        z, w, v = stream("returns"), stream("wicks"), stream("volume")
        regime = np.sin(2 * np.pi * (ts - EPOCH) / period + phase)
        drift = (bias + 0.9 * annual_vol * regime) * dt
        r = drift + sigma * z  # no volatility drag: median path follows the drift
        base = _BASE_PRICES.get(symbol, 20 + _seed(symbol) % 480)
        close = base * np.exp(np.cumsum(r))
        open_ = np.concatenate([[base], close[:-1]])
        wick = np.abs(w) * sigma * 0.6
        high = np.maximum(open_, close) * np.exp(wick)
        low = np.minimum(open_, close) * np.exp(-wick * 0.9)
        dollar_vol = 4e7 * (tf / 3600) * np.exp(0.35 * v) * (1 + 0.3 * np.abs(regime))
        volume = dollar_vol / close
        df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume},
                          index=pd.Index(ts, name="ts"))
        return df[BAR_COLUMNS]

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        key = (symbol, timeframe)
        df = self._cache.get(key)
        if df is None or df.index[-1] < end - TIMEFRAME_SECONDS[timeframe]:
            # generate with headroom so repeated live calls don't regenerate every tick
            df = self._generate(symbol, timeframe, end + 7 * 86400)
            self._cache[key] = df
        idx = df.index
        return df[(idx >= start) & (idx <= end)]
