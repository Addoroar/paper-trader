"""Shared test helpers."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest
import yaml

from papertrader.config import EXAMPLE_CONFIG, Config, parse_config
from papertrader.faults import set_fault
from papertrader.logging_setup import EventLogger
from papertrader.storage.db import Database

T0 = 1_735_689_600  # 2025-01-01 00:00 UTC


def make_frame(closes, start: int = T0, tf: int = 3600, volume: float = 1e6, spread: float = 0.002) -> pd.DataFrame:
    """OHLCV frame from a close series (open = previous close)."""
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + spread)
    l = np.minimum(o, c) * (1 - spread)
    idx = pd.Index(start + tf * np.arange(len(c)), name="ts", dtype="int64")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": volume}, index=idx)


def base_config_dict(tmp_path) -> dict:
    d = yaml.safe_load(EXAMPLE_CONFIG)
    d["storage"] = {"db_path": str(tmp_path / "live.db"), "backtest_db_path": str(tmp_path / "bt.db")}
    d["logging"]["dir"] = str(tmp_path / "logs")
    d["data"]["feed"] = "synthetic"
    d["data"]["cache_dir"] = str(tmp_path / "cache")
    return d


@pytest.fixture
def cfg_dict(tmp_path) -> dict:
    return base_config_dict(tmp_path)


@pytest.fixture
def cfg(cfg_dict) -> Config:
    return parse_config(copy.deepcopy(cfg_dict))


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture
def events(db) -> EventLogger:
    return EventLogger(db, lambda: T0)


@pytest.fixture(autouse=True)
def _no_faults():
    set_fault("")
    yield
    set_fault("")


class FrameFeed:
    """Factory for a DataFeed serving fixed frames (imported lazily to keep conftest light)."""

    def __new__(cls, frames: dict):
        from papertrader.data.base import DataFeed

        class _F(DataFeed):
            name = "frames"

            def fetch_bars(self, symbol, timeframe, start, end):
                df = frames.get(symbol)
                if df is None:
                    from papertrader.data.base import empty_bars

                    return empty_bars()
                return df[(df.index >= start) & (df.index <= end)]

        return _F()


def small_config(tmp_path, **over) -> Config:
    """A compact, fast config for engine tests (synthetic data, 4 symbols)."""
    d = base_config_dict(tmp_path)
    d["universe"] = {"stocks": ["AAPL", "SPY"], "crypto": ["BTC/USDT", "ETH/USDT"]}
    d["selection"].update({"lookback_bars": 20, "top_n": 2, "rebalance_every": 12})
    d["data"]["history_bars"] = 60
    d["accounts"] = [
        {"name": "sma", "strategy": "sma_crossover", "params": {"fast": 5, "slow": 15}, "allocation": 5000},
        {"name": "rsi", "strategy": "rsi_mean_reversion", "params": {"period": 7, "oversold": 35, "overbought": 65},
         "allocation": 5000},
    ]
    for k, v in over.items():
        d[k] = v
    return parse_config(d)
