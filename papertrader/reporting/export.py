"""CSV and self-contained HTML reports."""

from __future__ import annotations

import html
import time
from pathlib import Path

import pandas as pd

from papertrader.reporting.metrics import (
    account_summary,
    benchmark_curve,
    benchmark_return_pct,
    equity_curve,
    selection_performance,
    trades_frame,
)
from papertrader.storage.db import Database

DISCLAIMER = ("This is a simulation with fake money. Simulated fills are optimistic (no partial fills, queue position "
              "or market impact) and past performance does not predict future results.")


def equity_frame(db: Database) -> pd.DataFrame:
    """Long-format equity history for every account plus the benchmark."""
    rows = db.query("SELECT ts, account, cash, positions_value, equity FROM equity_snapshots ORDER BY ts, account")
    df = pd.DataFrame([dict(r) for r in rows], columns=["ts", "account", "cash", "positions_value", "equity"])
    b = benchmark_curve(db)
    if len(b):
        df = pd.concat([df, pd.DataFrame({"ts": b.index, "account": "universe_benchmark", "equity": b.values})])
    if not df.empty:
        df.insert(0, "time", pd.to_datetime(df["ts"], unit="s", utc=True))
    return df


def export_csv(db: Database, out_dir: str | Path) -> list[Path]:
    """Write trades.csv, equity.csv, accounts.csv and decisions.csv; returns the paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, df in [("trades", trades_frame(db)), ("equity", equity_frame(db)), ("accounts", account_summary(db))]:
        p = out / f"{name}.csv"
        df.to_csv(p, index=False)
        paths.append(p)
    ev = pd.DataFrame([dict(r) for r in db.query(
        "SELECT ts, level, type, account, symbol, message FROM events ORDER BY id")])
    if not ev.empty:
        ev.insert(0, "time", pd.to_datetime(ev["ts"], unit="s", utc=True))
    p = out / "decisions.csv"
    ev.to_csv(p, index=False)
    paths.append(p)
    return paths


def _thin(s: pd.Series, n: int = 1500) -> pd.Series:
    """Downsample a long curve for plotting (keeps the last point)."""
    if len(s) <= n:
        return s
    step = len(s) // n + 1
    return pd.concat([s.iloc[::step], s.iloc[-1:]])


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return html.escape(str(v))


def _table(df: pd.DataFrame) -> str:
    if df.empty:
        return "<p><em>none</em></p>"
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = "".join("<tr>" + "".join(f"<td>{_fmt(v)}</td>" for v in row) + "</tr>" for row in df.itertuples(index=False))
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def export_html(db: Database, out_path: str | Path, title: str = "papertrader report") -> Path:
    """A single HTML file with metrics, an equity chart, selection performance and recent trades."""
    import plotly.graph_objects as go

    fig = go.Figure()
    for a in db.query("SELECT name FROM accounts ORDER BY name"):
        eq = _thin(equity_curve(db, a["name"]))
        fig.add_trace(go.Scatter(x=pd.to_datetime(eq.index, unit="s", utc=True), y=eq.values, name=a["name"]))
    alloc = {r["name"]: r["allocation"] for r in db.query("SELECT name, allocation FROM accounts")}
    fig.update_layout(height=420, margin=dict(l=40, r=20, t=30, b=40), legend=dict(orientation="h"),
                      title="Equity by account")
    pct = go.Figure()
    for a, alloc_v in alloc.items():
        eq = _thin(equity_curve(db, a))
        pct.add_trace(go.Scatter(x=pd.to_datetime(eq.index, unit="s", utc=True), y=(eq / alloc_v - 1) * 100, name=a))
    b = _thin(benchmark_curve(db))
    if len(b):
        total = sum(alloc.values())
        pct.add_trace(go.Scatter(x=pd.to_datetime(b.index, unit="s", utc=True), y=(b / total - 1) * 100,
                                 name="universe buy & hold", line=dict(dash="dot", color="gray")))
    pct.update_layout(height=420, margin=dict(l=40, r=20, t=30, b=40), legend=dict(orientation="h"),
                      title="Return % vs universe buy & hold", yaxis_title="%")
    summary = account_summary(db)
    cols = ["account", "strategy", "allocation", "cash", "equity", "return_pct", "max_drawdown_pct", "sharpe",
            "win_rate_pct", "profit_factor", "trades", "fees", "open_positions"]
    trades = trades_frame(db)
    recent = trades.tail(200)[["time", "account", "symbol", "side", "qty", "price", "fee", "realized_pnl", "kind",
                               "reason"]] if not trades.empty else trades
    bench = benchmark_return_pct(db)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#222}}table{{border-collapse:collapse;font-size:13px;
margin:8px 0 24px}}td,th{{border:1px solid #ddd;padding:4px 8px;text-align:right}}th{{background:#f4f4f4}}
td:last-child{{text-align:left}}.note{{background:#fff8e1;padding:8px 12px;border-left:4px solid #f0b400}}</style>
</head><body><h1>{html.escape(title)}</h1>
<p>Generated {time.strftime('%Y-%m-%d %H:%M:%S')}. Universe buy &amp; hold return:
<b>{'-' if bench is None else f'{bench:+.2f}%'}</b></p>
<p class="note">{DISCLAIMER}</p>
<h2>Accounts</h2>{_table(summary[cols] if not summary.empty else summary)}
{pct.to_html(full_html=False, include_plotlyjs='cdn')}
{fig.to_html(full_html=False, include_plotlyjs=False)}
<h2>Selector vs random baseline (mean forward return per rebalance period)</h2>{_table(selection_performance(db))}
<h2>Recent trades (last 200)</h2>{_table(recent)}
</body></html>"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc)
    return p
