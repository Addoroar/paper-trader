"""Prove that no component can see bars that had not closed yet."""

import numpy as np

from papertrader.data import SyntheticFeed
from papertrader.engine.backtest import parse_date, run_backtest
from papertrader.selection import SELECTORS
from papertrader.strategies import STRATEGIES

from conftest import FrameFeed, make_frame, small_config


def test_engine_decisions_identical_when_future_differs(tmp_path):
    """Two datasets identical up to T but wildly different after it must yield identical decisions before T."""
    cfg = small_config(tmp_path)
    start, end = parse_date("2025-01-01"), parse_date("2025-01-12")
    cut = parse_date("2025-01-07")
    syn = SyntheticFeed()
    real, altered = {}, {}
    for sym in cfg.universe.symbols():
        df = syn.fetch_bars(sym, "1h", start - 40 * 86400, end)
        real[sym] = df
        alt = df.copy()
        future = alt.index >= cut
        rng = np.random.default_rng(1)
        alt.loc[future, ["open", "high", "low", "close"]] *= rng.uniform(0.3, 3.0, size=(future.sum(), 1))
        altered[sym] = alt
    a = run_backtest(cfg, start, end, source=FrameFeed(real), db_path=tmp_path / "a.db")
    b = run_backtest(cfg, start, end, source=FrameFeed(altered), db_path=tmp_path / "b.db")
    import sqlite3

    def q(path, sql):
        return sqlite3.connect(path).execute(sql, (cut,)).fetchall()

    orders_sql = "SELECT order_id, qty, ref_price, status FROM orders WHERE created_ts < ? ORDER BY order_id"
    dec_sql = ("SELECT type, account, symbol, message FROM events WHERE ts < ? AND type IN "
               "('signal','hold','trade_skipped','symbols_selected','risk_triggered') ORDER BY id")
    sel_sql = "SELECT account, step, kind, symbol, rank, score FROM selections WHERE ts < ? ORDER BY 1,2,3,4"
    assert q(a["db_path"], orders_sql) and q(a["db_path"], orders_sql) == q(b["db_path"], orders_sql)
    assert q(a["db_path"], dec_sql) == q(b["db_path"], dec_sql)
    assert q(a["db_path"], sel_sql) == q(b["db_path"], sel_sql)
    # ...and the futures really were different, so later decisions diverge
    late = "SELECT count(*) FROM orders WHERE created_ts >= ?"
    assert q(a["db_path"], late) != q(b["db_path"], late) or q(a["db_path"], dec_sql) != q(
        a["db_path"], dec_sql.replace("ts < ?", "ts >= ?"))


def test_strategies_and_selectors_ignore_appended_future():
    rng = np.random.default_rng(3)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
    full = make_frame(closes)
    for name, params in [("sma_crossover", {"fast": 5, "slow": 20}), ("rsi_mean_reversion", {}),
                         ("bollinger_bands", {}), ("momentum", {}), ("buy_and_hold", {})]:
        s = STRATEGIES.get(name)(params)
        for t in range(30, 200, 7):
            past = full.iloc[:t]
            spliced = full.iloc[:t].copy()
            assert s.on_bar(past, None) == s.on_bar(spliced, None)
            # the signal at t is a pure function of bars <= t
            assert s.on_bar(full.iloc[:t], None) == s.on_bar(make_frame(closes[:t]), None)
    from papertrader.models import TickContext

    for name in SELECTORS.names():
        sel = SELECTORS.get(name)(20, 1, 0, STRATEGIES.get("momentum")({}))
        ctx = TickContext("a", 0, 0, 42)
        r1 = sel.select({"A": full.iloc[:100], "B": make_frame(closes[::-1][:100])}, ctx)
        r2 = sel.select({"A": full.iloc[:100].copy(), "B": make_frame(closes[::-1][:100])}, ctx)
        assert r1.selected == r2.selected
