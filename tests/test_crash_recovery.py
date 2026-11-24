"""Crash safety: interrupted runs resume with consistent state and no duplicate trades."""

import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from papertrader.data import HistoricalFeed, SyntheticFeed
from papertrader.engine import Engine
from papertrader.engine.backtest import parse_date, run_backtest
from papertrader.engine.clock import SimClock
from papertrader.engine.lock import InstanceLock, LockError
from papertrader.engine.recovery import reconcile
from papertrader.faults import SimulatedCrash, set_fault
from papertrader.storage.db import Database

from conftest import FrameFeed, make_frame, small_config

START, END = parse_date("2025-01-01"), parse_date("2025-01-10")


def snapshot(path):
    c = sqlite3.connect(path)
    return {
        "orders": c.execute("SELECT order_id, account, symbol, side, qty, ref_price, status FROM orders "
                            "ORDER BY order_id").fetchall(),
        "fills": c.execute("SELECT order_id, qty, price, fee, realized_pnl FROM fills ORDER BY order_id").fetchall(),
        "cash": c.execute("SELECT name, round(cash, 6) FROM accounts ORDER BY name").fetchall(),
        "positions": c.execute("SELECT account, symbol, qty, round(avg_cost, 6) FROM positions ORDER BY 1,2").fetchall(),
    }


def assert_consistent(path):
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM orders WHERE status='PENDING'").fetchone()[0] == 0
    dup = c.execute("SELECT account, symbol, bar_ts, side, count(*) FROM orders GROUP BY 1,2,3,4 HAVING count(*)>1")
    assert dup.fetchall() == []
    # every filled order has exactly one fill and vice versa
    assert c.execute("SELECT count(*) FROM orders WHERE status='FILLED'").fetchone()[0] == \
        c.execute("SELECT count(*) FROM fills").fetchone()[0]
    db = Database(path)
    cfg_like = type("E", (), {})()
    from papertrader.logging_setup import EventLogger

    cfg_like.db, cfg_like.events = db, EventLogger(db)
    cfg_like.log = lambda *a, **k: cfg_like.events.log(*a, **k)
    assert all(not p for p in reconcile(cfg_like).values())


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("clean")
    cfg = small_config(tmp)
    r = run_backtest(cfg, START, END, source=SyntheticFeed(), db_path=tmp / "clean.db")
    snap = snapshot(r["db_path"])
    assert len(snap["fills"]) >= 6, "test needs a run that actually trades"
    return snap


@pytest.mark.parametrize("fault", ["after_place:raise:2", "after_place:raise:5", "after_fill:raise:3",
                                   "after_round:raise:40"])
def test_crash_mid_tick_then_resume_matches_clean_run(tmp_path, clean_run, fault):
    cfg = small_config(tmp_path)
    db_path = tmp_path / "crash.db"
    set_fault(fault)
    with pytest.raises(SimulatedCrash):
        run_backtest(cfg, START, END, source=SyntheticFeed(), db_path=db_path)
    set_fault("")
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT state FROM bot_status").fetchone()[0] == "running"  # unclean
    if fault.startswith("after_place"):
        assert c.execute("SELECT count(*) FROM orders WHERE status='PENDING'").fetchone()[0] == 1
    run_backtest(cfg, START, END, source=SyntheticFeed(), db_path=db_path, resume=True)
    assert snapshot(db_path) == clean_run
    assert_consistent(db_path)
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT count(*) FROM events WHERE type='recovered_from_interruption'").fetchone()[0] >= 1
    assert c.execute("SELECT state, restarts FROM bot_status").fetchone() == ("stopped", 1)


def test_kill_dash_nine_mid_tick_then_resume(tmp_path, clean_run):
    """A real SIGKILL between order placement and fill (indistinguishable from power loss)."""
    db_path = tmp_path / "killed.db"
    script = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(os.path.dirname(__file__))!r})
        from conftest import small_config
        from pathlib import Path
        from papertrader.data import SyntheticFeed
        from papertrader.engine.backtest import run_backtest
        cfg = small_config(Path({str(tmp_path)!r}))
        run_backtest(cfg, {START}, {END}, source=SyntheticFeed(), db_path={str(db_path)!r})
    """)
    env = {**os.environ, "PAPERTRADER_FAULT": "after_place:kill:4"}
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, timeout=300)
    assert proc.returncode == -9, proc.stderr.decode()[-2000:]
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT count(*) FROM orders WHERE status='PENDING'").fetchone()[0] == 1
    cfg = small_config(tmp_path)
    run_backtest(cfg, START, END, source=SyntheticFeed(), db_path=db_path, resume=True)
    assert snapshot(db_path) == clean_run
    assert_consistent(db_path)


def test_stale_pending_order_is_cancelled_not_filled(tmp_path):
    cfg = small_config(tmp_path)
    db_path = tmp_path / "stale.db"
    hist = HistoricalFeed(SyntheticFeed(), cfg.timeframe)
    db = Database(db_path)
    clock = SimClock(START)
    eng = Engine(cfg, db, hist, mode="live", clock=clock)
    hist.preload(eng.symbols, START, END, 80)
    timeline = hist.timeline(START, END)
    eng.startup()
    set_fault("after_place:raise:1")
    crashed_at = None
    for t in timeline:
        clock.set(t)
        try:
            eng.tick()
        except SimulatedCrash:
            crashed_at = t
            break
    set_fault("")
    assert crashed_at is not None
    pending = db.one("SELECT order_id, symbol FROM orders WHERE status='PENDING'")
    # restart 6 hours later: the order's bar is stale now
    db2 = Database(db_path)
    clock2 = SimClock(crashed_at + 6 * 3600)
    eng2 = Engine(cfg, db2, hist, mode="live", clock=clock2)
    eng2.startup()
    row = db2.one("SELECT status, reject_reason FROM orders WHERE order_id=?", (pending["order_id"],))
    assert row["status"] == "CANCELLED" and "stale" in row["reject_reason"]
    eng2.tick()
    gaps = db2.query("SELECT message FROM events WHERE type='gap_replay'")
    assert gaps and any("missed bar" in g["message"] for g in gaps)
    assert db2.scalar("SELECT count(*) FROM fills WHERE order_id=?", (pending["order_id"],)) == 0
    assert db2.scalar("SELECT count(*) FROM events WHERE type='recovered_from_interruption'") >= 1
    assert db2.scalar("SELECT count(*) FROM events WHERE type='reconciliation' AND level='ERROR'") == 0


def test_gap_replay_applies_stop_loss_on_missed_bars(tmp_path):
    d = small_config(tmp_path).model_dump()
    d["universe"] = {"stocks": [], "crypto": ["BTC/USDT"], "exclude": []}
    d["selection"].update({"lookback_bars": 5, "top_n": 1, "min_avg_volume": 0})
    d["accounts"] = [{"name": "bh", "strategy": "buy_and_hold", "params": {}, "allocation": 1000}]
    from papertrader.config import parse_config

    cfg = parse_config(d)
    closes = [100.0] * 30 + [101, 102, 103, 90, 95, 100, 104, 106, 107, 108]  # dip to 90 at bar 33
    t0 = START - 30 * 3600
    df = make_frame(closes, start=t0, spread=0.001)
    db = Database(tmp_path / "gap.db")
    clock = SimClock(t0 + 30 * 3600)  # bar 29 just closed
    eng = Engine(cfg, db, FrameFeed({"BTC/USDT": df}), clock=clock)
    eng.startup()
    eng.tick()
    assert db.scalar("SELECT count(*) FROM fills WHERE side='BUY'") == 1
    clock.set(t0 + 40 * 3600)  # "down" for 10 bars
    eng.tick()
    stop = db.one("SELECT f.bar_ts, f.price, o.kind FROM fills f JOIN orders o USING(order_id) WHERE f.side='SELL'")
    assert stop["kind"] == "stop_loss"
    assert stop["bar_ts"] == t0 + 33 * 3600  # executed on the missed bar where the low breached the stop
    gap = db.one("SELECT message FROM events WHERE type='gap_replay'")["message"]
    assert "9 missed bar" in gap and "stop_loss executed" in gap
    # the only new entry is on the latest bar, never on the stale missed ones
    buys = db.query("SELECT bar_ts FROM orders WHERE side='BUY' ORDER BY bar_ts")
    assert [b["bar_ts"] for b in buys] == [t0 + 29 * 3600, t0 + 39 * 3600]


def test_replaying_same_tick_is_idempotent(tmp_path):
    cfg = small_config(tmp_path)
    hist = HistoricalFeed(SyntheticFeed(), cfg.timeframe)
    db = Database(tmp_path / "idem.db")
    clock = SimClock(START)
    eng = Engine(cfg, db, hist, clock=clock)
    hist.preload(eng.symbols, START, END, 80)
    eng.startup()
    for t in hist.timeline(START, END)[:60]:
        clock.set(t)
        eng.tick()
    def counts():
        tables = [db.scalar(f"SELECT count(*) FROM {t}") for t in ("orders", "fills", "equity_snapshots", "selections")]
        return tables + [db.scalar("SELECT count(*) FROM events WHERE type NOT LIKE 'tick_%'")]

    before = counts()
    eng.tick()
    eng.tick()
    assert counts() == before  # only tick heartbeats are added when nothing new happened


def test_single_instance_lock(tmp_path):
    p = tmp_path / "x.db"
    with InstanceLock(p):
        assert InstanceLock(p).is_locked()
        with pytest.raises(LockError, match="already running"):
            InstanceLock(p).acquire()
    assert not InstanceLock(p).is_locked()
    InstanceLock(p).acquire().release()


def test_stale_latest_bar_is_never_traded(tmp_path):
    """A bar that closed long before 'now' (e.g. market closed at startup) is not traded on."""
    d = small_config(tmp_path).model_dump()
    d["universe"] = {"stocks": ["SPY"], "crypto": [], "exclude": []}
    d["selection"].update({"lookback_bars": 5, "top_n": 1, "min_avg_volume": 0})
    d["accounts"] = [{"name": "bh", "strategy": "buy_and_hold", "params": {}, "allocation": 1000}]
    from papertrader.config import parse_config

    cfg = parse_config(d)
    hist = HistoricalFeed(SyntheticFeed(), "1h")
    db = Database(tmp_path / "s.db")
    saturday = parse_date("2025-01-04T15:00:00")  # NYSE closed; last bar closed Friday 16:00 ET
    eng = Engine(cfg, db, hist, clock=SimClock(saturday))
    hist.preload(["SPY"], saturday - 86400 * 5, saturday, 80)
    eng.startup()
    eng.tick()
    assert db.scalar("SELECT count(*) FROM orders") == 0
    assert db.scalar("SELECT count(*) FROM cursors") == 1  # bar acknowledged, not traded
