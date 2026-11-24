"""Backtest mode: the live engine driven by a simulated clock over historical bars.

The same ``Engine`` (selector, strategy, risk, broker) runs; only the feed
(a :class:`~papertrader.data.historical.HistoricalFeed` that reveals bars as
the clock passes their close) and the clock differ. Results go to a separate
database (``storage.backtest_db_path``) that is wiped at the start of each run
unless ``resume=True``. A resumed backtest continues from the last committed
tick, using the same recovery path as a live restart.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from papertrader.config import Config
from papertrader.data import HistoricalFeed, build_feed
from papertrader.data.base import DataFeed
from papertrader.engine.clock import SimClock
from papertrader.engine.engine import Engine
from papertrader.storage.db import Database


def parse_date(s: str) -> int:
    """'YYYY-MM-DD' (UTC midnight) or full ISO datetime -> epoch seconds."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def wipe_db(path: str | Path) -> None:
    """Delete a SQLite database and its WAL/SHM files."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def run_backtest(
    cfg: Config,
    start: int,
    end: int,
    *,
    feed: str | None = None,
    source: DataFeed | None = None,
    db_path: str | Path | None = None,
    resume: bool = False,
    progress: bool = False,
) -> dict:
    """Run a backtest; returns ``{"db_path", "ticks", "seconds", "bars"}``."""
    db_path = Path(db_path or cfg.storage.backtest_db_path)
    if not resume:
        wipe_db(db_path)
    db = Database(db_path, durable=False)
    hist = HistoricalFeed(source or build_feed(cfg, feed), cfg.timeframe)
    clock = SimClock(start)
    engine = Engine(cfg, db, hist, mode="backtest", clock=clock)
    warmup = max(cfg.data.history_bars, cfg.selection.lookback_bars + 1)
    counts = hist.preload(engine.symbols, start, end, warmup)
    timeline = hist.timeline(start, end)
    last = db.get_meta("last_tick_ts")
    if resume and last is not None:
        timeline = [t for t in timeline if t >= int(last)]  # replays the interrupted tick (idempotent)
    t0 = time.monotonic()
    if timeline:
        clock.set(timeline[0])
    engine.startup()
    n = len(timeline)
    for i, t in enumerate(timeline):
        clock.set(t)
        engine.tick()
        if progress and (i % 500 == 0 or i == n - 1):
            print(f"\r  backtest {i + 1}/{n} bars ({datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M})",
                  end="", flush=True)
    if progress:
        print()
    engine.shutdown("backtest complete")
    db.close()
    return {"db_path": str(db_path), "ticks": n, "seconds": time.monotonic() - t0, "bars": counts}
