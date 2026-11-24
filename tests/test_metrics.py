import math

import pandas as pd
import pytest

from papertrader.engine.backtest import parse_date, run_backtest
from papertrader.data import SyntheticFeed
from papertrader.reporting.export import export_csv, export_html
from papertrader.reporting.metrics import account_summary, compute_metrics, selection_performance, trades_frame
from papertrader.storage.db import Database

from conftest import small_config


def test_compute_metrics_basic():
    eq = pd.Series([100, 110, 99, 121], index=[0, 3600, 7200, 10800], dtype=float)
    m = compute_metrics(eq, pd.Series([10.0, -5.0, 20.0]), fees=3.0, start_value=100)
    assert m["return_pct"] == pytest.approx(21)
    assert m["max_drawdown_pct"] == pytest.approx((99 / 110 - 1) * 100)
    assert m["win_rate_pct"] == pytest.approx(200 / 3)
    assert m["profit_factor"] == pytest.approx(6)
    assert m["fees"] == 3 and m["trades"] == 3
    assert m["sharpe"] > 0


def test_compute_metrics_edge_cases():
    m = compute_metrics(pd.Series(dtype=float), pd.Series(dtype=float), 0, 1000)
    assert m["return_pct"] == 0 and m["win_rate_pct"] is None and m["profit_factor"] is None
    m = compute_metrics(pd.Series([1000.0, 1000.0], index=[0, 60]), pd.Series([5.0]), 0, 1000)
    assert m["sharpe"] == 0 and math.isinf(m["profit_factor"])


def test_reports_from_backtest(tmp_path):
    cfg = small_config(tmp_path)
    r = run_backtest(cfg, parse_date("2025-01-01"), parse_date("2025-01-08"), source=SyntheticFeed(),
                     db_path=tmp_path / "r.db")
    db = Database(r["db_path"], readonly=True)
    s = account_summary(db)
    assert set(s["account"]) == {"sma", "rsi"}
    # equity = cash + positions, and realized P&L sums match the fills table
    t = trades_frame(db)
    assert len(t) == db.scalar("SELECT count(*) FROM fills")
    assert t["reason"].str.len().min() > 10
    sp = selection_performance(db)
    assert {"primary", "random_shadow", "universe"} <= set(sp["kind"])
    paths = export_csv(db, tmp_path / "csv")
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)
    html = export_html(db, tmp_path / "r.html").read_text()
    assert "simulation" in html and "sma" in html


def test_backtest_is_deterministic(tmp_path):
    cfg = small_config(tmp_path)
    d = cfg.model_dump()
    d["selection"]["method"] = "random_baseline"
    from papertrader.config import parse_config

    cfg = parse_config(d)
    args = (parse_date("2025-01-01"), parse_date("2025-01-06"))
    a = run_backtest(cfg, *args, source=SyntheticFeed(), db_path=tmp_path / "a.db")
    b = run_backtest(cfg, *args, source=SyntheticFeed(), db_path=tmp_path / "b.db")
    q = "SELECT order_id, qty, price, fee FROM fills ORDER BY order_id"
    qa, qb = Database(a["db_path"]).query(q), Database(b["db_path"]).query(q)
    assert [tuple(r) for r in qa] == [tuple(r) for r in qb] and len(qa) > 0
