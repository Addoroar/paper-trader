"""SQLite cache in front of a network feed, with retry and market-hours awareness."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd

from papertrader.data.base import DataFeed, empty_bars, normalize
from papertrader.data.market_hours import is_market_open, last_session_close
from papertrader.data.retry import with_retry
from papertrader.models import TIMEFRAME_SECONDS, is_crypto


class CachedFeed(DataFeed):
    """Caches bars from ``inner`` in ``<cache_dir>/bars.db`` and only fetches what's missing.

    Stocks are not re-fetched while the exchange is closed once the last
    session's bars are cached. Network failures are retried with exponential
    backoff; if they still fail, cached data is used and the error is logged.
    """

    def __init__(self, inner: DataFeed, cache_dir: str | Path, retries: int = 4, base_delay: float = 1.0) -> None:
        super().__init__()
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self.retries = retries
        self.base_delay = base_delay
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(Path(cache_dir) / "bars.db", isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS bars (source TEXT, symbol TEXT, tf TEXT, ts INTEGER, open REAL, high REAL,"
            " low REAL, close REAL, volume REAL, PRIMARY KEY (source, symbol, tf, ts))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS fetch_log (source TEXT, symbol TEXT, tf TEXT, fetched_at INTEGER,"
            " covered_from INTEGER, PRIMARY KEY (source, symbol, tf))"
        )

    def _set_emit(self) -> None:
        self.inner.on_event = self.on_event

    def _cached(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT ts, open, high, low, close, volume FROM bars WHERE source=? AND symbol=? AND tf=? AND ts BETWEEN ? AND ?"
            " ORDER BY ts",
            (self.inner.name, symbol, timeframe, start, end),
        ).fetchall()
        if not rows:
            return empty_bars()
        return normalize(pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts"))

    def _store(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)",
            [(self.inner.name, symbol, timeframe, int(ts), *map(float, row)) for ts, row in
             zip(df.index, df[["open", "high", "low", "close", "volume"]].to_numpy())],
        )

    def _needs_fetch(self, symbol: str, timeframe: str, start: int, now: int) -> tuple[bool, int]:
        r = self.conn.execute(
            "SELECT fetched_at, covered_from FROM fetch_log WHERE source=? AND symbol=? AND tf=?",
            (self.inner.name, symbol, timeframe),
        ).fetchone()
        last_ts = self.conn.execute(
            "SELECT max(ts) FROM bars WHERE source=? AND symbol=? AND tf=?", (self.inner.name, symbol, timeframe)
        ).fetchone()[0]
        if r is None or last_ts is None or r[1] > start:
            return True, start
        fetched_at = r[0]
        fetch_from = max(start, int(last_ts) - 2 * TIMEFRAME_SECONDS[timeframe])
        if not is_crypto(symbol) and not is_market_open(symbol, now) and fetched_at >= last_session_close(now) + 900:
            return False, fetch_from  # market closed and we already have the final bars
        return True, fetch_from

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        self._set_emit()
        now = int(time.time())
        need, fetch_from = self._needs_fetch(symbol, timeframe, start, min(end, now))
        if need:
            try:
                fresh = with_retry(
                    lambda: self.inner.fetch_bars(symbol, timeframe, fetch_from, end),
                    what=f"fetch {symbol} {timeframe} from {self.inner.name}",
                    retries=self.retries, base_delay=self.base_delay, emit=self.emit, symbol=symbol,
                )
                self._store(symbol, timeframe, fresh)
                r = self.conn.execute(
                    "SELECT covered_from FROM fetch_log WHERE source=? AND symbol=? AND tf=?",
                    (self.inner.name, symbol, timeframe),
                ).fetchone()
                covered = min(fetch_from, r[0]) if r else fetch_from
                self.conn.execute(
                    "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                    (self.inner.name, symbol, timeframe, now, covered),
                )
            except Exception:  # noqa: BLE001 - fall back to cache; error already logged by with_retry
                pass
        return self._cached(symbol, timeframe, start, end)
