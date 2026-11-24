import time

import pandas as pd
import pytest

from papertrader.data import HistoricalFeed, SyntheticFeed
from papertrader.data.base import DataFeed, normalize
from papertrader.data.cache import CachedFeed
from papertrader.data.market_hours import bar_close_times, is_market_open, session_bounds
from papertrader.data.retry import DataFetchError, with_retry

from conftest import T0, make_frame


def test_synthetic_deterministic_and_prefix_stable():
    a = SyntheticFeed().fetch_bars("AAPL", "1h", T0, T0 + 10 * 86400)
    b = SyntheticFeed().fetch_bars("AAPL", "1h", T0, T0 + 40 * 86400)
    pd.testing.assert_frame_equal(a, b.loc[a.index])
    assert (a["high"] >= a[["open", "close"]].max(axis=1)).all()
    assert (a["low"] <= a[["open", "close"]].min(axis=1)).all()


def test_stock_bars_only_in_market_hours():
    df = SyntheticFeed().fetch_bars("SPY", "1h", T0, T0 + 14 * 86400)
    for ts in df.index:
        assert is_market_open("SPY", int(ts))
    assert len(SyntheticFeed().fetch_bars("BTC/USDT", "1h", T0, T0 + 86400 - 1)) == 24


def test_market_hours_and_bar_close():
    o, c = session_bounds(pd.Timestamp("2025-01-06").date())  # a Monday
    assert c - o == 6.5 * 3600
    last_bar = o + 6 * 3600  # 15:30 hourly bar closes at 16:00, not 16:30
    assert bar_close_times("SPY", [last_bar], 3600)[0] == c
    assert not is_market_open("SPY", o - 60)
    assert is_market_open("BTC/USDT", o - 60)
    assert not is_market_open("SPY", int(pd.Timestamp("2025-12-25 15:00", tz="America/New_York").timestamp()))


def test_retry_backoff_then_success_and_failure():
    calls, events, sleeps = [], [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return "ok"

    assert with_retry(flaky, what="x", retries=4, base_delay=1, emit=lambda *a, **k: events.append(a[0]),
                      sleep=sleeps.append) == "ok"
    assert events == ["data_fetch_retry", "data_fetch_retry"]
    assert sleeps[1] > sleeps[0] >= 1  # exponential
    with pytest.raises(DataFetchError):
        with_retry(lambda: 1 / 0, what="y", retries=1, base_delay=0, emit=lambda *a, **k: events.append(a[0]),
                   sleep=lambda s: None)
    assert events[-1] == "data_fetch_error"


class CountingFeed(DataFeed):
    name = "counting"

    def __init__(self, df, fail=False):
        super().__init__()
        self.df, self.calls, self.fail = df, 0, fail

    def fetch_bars(self, symbol, timeframe, start, end):
        self.calls += 1
        if self.fail:
            raise ConnectionError("offline")
        return self.df[(self.df.index >= start) & (self.df.index <= end)]


def test_cache_serves_and_falls_back(tmp_path):
    now = int(time.time()) // 3600 * 3600
    df = make_frame(range(100, 150), start=now - 50 * 3600)
    inner = CountingFeed(df)
    feed = CachedFeed(inner, tmp_path, retries=0, base_delay=0)
    got = feed.get_bars("BTC/USDT", "1h", now, 20)
    assert len(got) == 20 and inner.calls == 1
    inner.fail = True
    events = []
    feed.on_event = lambda t, m, **k: events.append(t)
    again = feed.get_bars("BTC/USDT", "1h", now, 20)  # network down -> cached data
    pd.testing.assert_frame_equal(normalize(got), normalize(again))
    assert "data_fetch_error" in events


def test_historical_feed_reveals_bars_only_after_close():
    h = HistoricalFeed(SyntheticFeed(), "1h")
    h.load_frame("BTC/USDT", make_frame(range(10)))
    assert len(h.get_bars("BTC/USDT", "1h", T0 + 3599, 100)) == 0
    assert len(h.get_bars("BTC/USDT", "1h", T0 + 3600, 100)) == 1
    assert list(h.timeline(T0, T0 + 3 * 3600)) == [T0 + 3600, T0 + 7200, T0 + 10800]
