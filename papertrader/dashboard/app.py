"""Streamlit dashboard. Launch with ``python -m papertrader dashboard``.

Reads the SQLite state read-only, so it works whether the bot is running or
stopped, and refreshes itself every 30 seconds.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from papertrader.cli import bot_state
from papertrader.config import ConfigError, load_config
from papertrader.reporting.export import DISCLAIMER, equity_frame
from papertrader.reporting.metrics import (
    account_summary,
    benchmark_curve,
    benchmark_return_pct,
    equity_curve,
    selection_performance,
    trades_frame,
)
from papertrader.storage.db import Database

# Categorical series colours, assigned to accounts in a fixed order (colour follows the account).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
BENCH = "#8a8985"
DECISION_TYPES = ["symbols_selected", "signal", "hold", "trade_skipped", "order_placed", "order_filled",
                  "order_rejected", "order_cancelled", "risk_triggered", "gap_replay", "daily_summary"]
HEALTH_TYPES = ["startup", "shutdown", "recovered_from_interruption", "reconciliation", "data_fetch_error",
                "data_fetch_retry", "error", "order_cancelled"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--backtest", action="store_true")
    return p.parse_args(sys.argv[1:])


def open_db(path: Path) -> Database | None:
    if not path.exists():
        return None
    try:
        return Database(path, readonly=True)
    except Exception:  # noqa: BLE001 - e.g. WAL index missing after a clean close; open normally
        return Database(path)


def ts_to_dt(s):
    return pd.to_datetime(s, unit="s", utc=True)


def fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def line_chart(series: dict[str, pd.Series], colors: dict[str, str], y_title: str, bench: pd.Series | None = None):
    fig = go.Figure()
    for name, s in series.items():
        if len(s):
            fig.add_trace(go.Scatter(x=ts_to_dt(s.index), y=s.values, name=name, mode="lines",
                                     line=dict(width=2, color=colors[name]),
                                     hovertemplate="%{x|%Y-%m-%d %H:%M}<br>" + name + ": %{y:.2f}<extra></extra>"))
    if bench is not None and len(bench):
        fig.add_trace(go.Scatter(x=ts_to_dt(bench.index), y=bench.values, name="universe buy & hold",
                                 mode="lines", line=dict(width=2, dash="dot", color=BENCH),
                                 hovertemplate="%{x|%Y-%m-%d %H:%M}<br>benchmark: %{y:.2f}<extra></extra>"))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0), yaxis_title=y_title)
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)", zerolinecolor="rgba(128,128,128,0.4)")
    return fig


def main() -> None:
    args = parse_args()
    st.set_page_config(page_title="papertrader", layout="wide")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        st.error(str(e))
        return

    with st.sidebar:
        st.header("papertrader")
        which = st.radio("Database", ["Live", "Backtest"], index=1 if args.backtest else 0, horizontal=True)
        auto = st.toggle("Auto-refresh (30s)", value=True)
        st.caption(DISCLAIMER)
    path = Path(cfg.storage.backtest_db_path if which == "Backtest" else cfg.storage.db_path)

    @st.fragment(run_every="30s" if auto else None)
    def body() -> None:
        db = open_db(path)
        if db is None:
            st.info(f"No state at `{path}` yet. Start the bot with `python -m papertrader run --config {args.config}`"
                    + (" or run a backtest." if which == "Backtest" else "."))
            return
        try:
            render(db, cfg)
        finally:
            db.close()
        st.caption(f"{path} · refreshed {time.strftime('%H:%M:%S')}")

    body()


def render(db: Database, cfg) -> None:
    accounts = [r["name"] for r in db.query("SELECT name FROM accounts ORDER BY created_at, rowid")]
    order = [a.name for a in cfg.accounts if a.name in accounts] + [a for a in accounts if
                                                                    a not in {x.name for x in cfg.accounts}]
    colors = {a: SERIES[i % len(SERIES)] for i, a in enumerate(order)}
    summary = account_summary(db)
    tabs = st.tabs(["Overview", "Accounts", "Trades", "Decision log", "Selection", "Health"])
    with tabs[0]:
        overview(db, cfg, summary, order, colors)
    with tabs[1]:
        accounts_tab(db, summary, order)
    with tabs[2]:
        trades_tab(db, order)
    with tabs[3]:
        decisions_tab(db, order)
    with tabs[4]:
        selection_tab(db, order, colors)
    with tabs[5]:
        health_tab(db, cfg)


def overview(db, cfg, summary: pd.DataFrame, order, colors) -> None:
    st_ = bot_state(db, cfg.heartbeat_stale_seconds)
    total_alloc = summary["allocation"].sum() if not summary.empty else 0
    total_eq = summary["equity"].sum() if not summary.empty else 0
    ret = (total_eq / total_alloc - 1) * 100 if total_alloc else 0
    bench = benchmark_return_pct(db)
    c = st.columns(5)
    c[0].metric("Total equity", fmt_money(total_eq), f"{total_eq - total_alloc:+,.2f}")
    c[1].metric("Return", f"{ret:+.2f}%")
    c[2].metric("vs buy & hold", "-" if bench is None else f"{ret - bench:+.2f} pp",
                help=f"Universe equal-weight buy & hold: {'-' if bench is None else f'{bench:+.2f}%'}")
    status = "not running" if "stale" in st_["label"] else st_["state"]
    c[3].metric("Bot status", status.capitalize(), help=f"{st_['label']} · {st_['mode']} mode")
    c[4].metric("Last heartbeat", st_["heartbeat_age"], help=f"last processed bar time: {st_['last_tick']}")
    if "stale" in st_["label"]:
        st.warning(st_["label"])

    mode = st.radio("Show", ["Return %", "Equity"], horizontal=True, label_visibility="collapsed", key="ov_mode")
    series, bench_s = {}, benchmark_curve(db)
    alloc = dict(zip(summary["account"], summary["allocation"])) if not summary.empty else {}
    for a in order:
        eq = equity_curve(db, a)
        series[a] = (eq / alloc[a] - 1) * 100 if mode == "Return %" else eq
    # the universe benchmark is a % comparison, so it is drawn on the Return % view only
    b = (bench_s / total_alloc - 1) * 100 if len(bench_s) and mode == "Return %" and total_alloc else None
    if not any(len(s) for s in series.values()):
        st.info("No equity history yet - it appears after the first bar is processed.")
        return
    st.plotly_chart(line_chart(series, colors, "return %" if mode == "Return %" else cfg.currency, b),
                    use_container_width=True)
    eq_df = equity_frame(db)
    st.download_button("Download equity CSV", eq_df.to_csv(index=False), "equity.csv", "text/csv", key="dl_eq")


def accounts_tab(db, summary: pd.DataFrame, order) -> None:
    if summary.empty:
        st.info("No accounts yet.")
        return
    view = summary.set_index("account").loc[[a for a in order if a in set(summary["account"])]].reset_index()
    st.dataframe(view[["account", "strategy", "allocation", "cash", "equity", "return_pct", "max_drawdown_pct",
                       "sharpe", "win_rate_pct", "profit_factor", "trades", "fees", "realized_pnl",
                       "open_positions"]],
                 hide_index=True, use_container_width=True,
                 column_config={"return_pct": st.column_config.NumberColumn("return %", format="%+.2f"),
                                "max_drawdown_pct": st.column_config.NumberColumn("max DD %", format="%.2f"),
                                "sharpe": st.column_config.NumberColumn(format="%.2f"),
                                "win_rate_pct": st.column_config.NumberColumn("win %", format="%.0f"),
                                "profit_factor": st.column_config.NumberColumn("profit factor", format="%.2f"),
                                "cash": st.column_config.NumberColumn(format="%.2f"),
                                "equity": st.column_config.NumberColumn(format="%.2f"),
                                "fees": st.column_config.NumberColumn(format="%.2f"),
                                "realized_pnl": st.column_config.NumberColumn("realized P&L", format="%+.2f")})
    st.subheader("Open positions")
    rows = []
    for a in order:
        snap = db.one("SELECT prices_json, ts FROM equity_snapshots WHERE account=? ORDER BY ts DESC LIMIT 1", (a,))
        prices = json.loads(snap["prices_json"]) if snap else {}
        for p in db.query("SELECT * FROM positions WHERE account=? ORDER BY symbol", (a,)):
            px = prices.get(p["symbol"], p["avg_cost"])
            rows.append({"account": a, "symbol": p["symbol"], "qty": p["qty"], "avg cost": p["avg_cost"],
                         "last price": px, "value": p["qty"] * px,
                         "unrealized P&L": (px - p["avg_cost"]) * p["qty"],
                         "unrealized %": (px / p["avg_cost"] - 1) * 100,
                         "opened": ts_to_dt(p["opened_ts"])})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={"unrealized P&L": st.column_config.NumberColumn(format="%+.2f"),
                                    "unrealized %": st.column_config.NumberColumn(format="%+.2f"),
                                    "value": st.column_config.NumberColumn(format="%.2f")})
    else:
        st.caption("No open positions.")


def trades_tab(db, order) -> None:
    df = trades_frame(db)
    if df.empty:
        st.info("No trades yet.")
        return
    c = st.columns(4)
    accts = c[0].multiselect("Account", order, default=[], key="tr_acct")
    syms = c[1].multiselect("Symbol", sorted(df["symbol"].unique()), default=[], key="tr_sym")
    sides = c[2].multiselect("Side", ["BUY", "SELL"], default=[], key="tr_side")
    dmin, dmax = df["time"].min().date(), df["time"].max().date()
    rng = c[3].date_input("Date range", (dmin, dmax), min_value=dmin, max_value=dmax, key="tr_dates")
    if accts:
        df = df[df["account"].isin(accts)]
    if syms:
        df = df[df["symbol"].isin(syms)]
    if sides:
        df = df[df["side"].isin(sides)]
    if isinstance(rng, tuple) and len(rng) == 2:
        df = df[(df["time"].dt.date >= rng[0]) & (df["time"].dt.date <= rng[1])]
    closed = df[df["side"] == "SELL"]
    m = st.columns(4)
    m[0].metric("Trades", len(df))
    m[1].metric("Realized P&L", f"{closed['realized_pnl'].sum():+,.2f}")
    m[2].metric("Win rate", f"{(closed['realized_pnl'] > 0).mean() * 100:.0f}%" if len(closed) else "-")
    m[3].metric("Fees", f"{df['fee'].sum():,.2f}")
    view = df.drop(columns=["ts", "bar_ts"]).sort_values("time", ascending=False)
    view["time"] = view["time"].dt.strftime("%Y-%m-%d %H:%M UTC")
    st.dataframe(view, hide_index=True,
                 use_container_width=True,
                 column_config={"realized_pnl": st.column_config.NumberColumn("realized P&L", format="%+.2f"),
                                "price": st.column_config.NumberColumn(format="%.4f"),
                                "fee": st.column_config.NumberColumn(format="%.2f"),
                                "reason": st.column_config.TextColumn(width="large")})
    st.download_button("Download trades CSV", df.to_csv(index=False), "trades.csv", "text/csv", key="dl_tr")


def decisions_tab(db, order) -> None:
    c = st.columns([2, 2, 2, 1])
    q = c[0].text_input("Search", placeholder="e.g. NVDA, stop-loss, crossed above", key="dl_q")
    types = c[1].multiselect("Event type", DECISION_TYPES + HEALTH_TYPES,
                             default=[t for t in DECISION_TYPES if t != "hold"], key="dl_types")
    accts = c[2].multiselect("Account", order, key="dl_acct")
    limit = c[3].number_input("Max rows", 100, 20000, 1000, step=100, key="dl_limit")
    sql = "SELECT ts, level, type, account, symbol, message FROM events WHERE 1=1"
    params: list = []
    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"
        params += types
    if accts:
        sql += f" AND account IN ({','.join('?' * len(accts))})"
        params += accts
    if q:
        sql += " AND (message LIKE ? OR symbol LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    df = pd.DataFrame([dict(r) for r in db.query(sql, tuple(params))],
                      columns=["ts", "level", "type", "account", "symbol", "message"])
    if df.empty:
        st.info("No matching events.")
        return
    df.insert(0, "time", ts_to_dt(df.pop("ts")).dt.strftime("%Y-%m-%d %H:%M UTC"))
    st.caption(f"{len(df)} most recent matching events (newest first). Include 'hold' to see every "
               "bar where a strategy considered a symbol and chose not to trade.")
    st.dataframe(df, hide_index=True, use_container_width=True, height=620,
                 column_config={"message": st.column_config.TextColumn(width="large")})


def selection_tab(db, order, colors) -> None:
    rows = db.query("SELECT account, step, ts, kind, symbol, rank, score, price, selected, reason FROM selections "
                    "ORDER BY ts")
    if not rows:
        st.info("No selections yet.")
        return
    df = pd.DataFrame([dict(r) for r in rows])
    df["time"] = ts_to_dt(df["ts"])
    acct = st.selectbox("Account", [a for a in order if a in set(df["account"])], key="sel_acct")
    prim = df[(df["account"] == acct) & (df["kind"] == "primary")]
    chosen = prim[prim["selected"] == 1]
    st.subheader("Selected symbols over time")
    syms = sorted(prim["symbol"].unique())
    fig = go.Figure(go.Scatter(
        x=chosen["time"], y=chosen["symbol"], mode="markers",
        marker=dict(size=10, color=colors.get(acct, SERIES[0]), line=dict(width=2, color="white")),
        customdata=chosen[["rank", "reason"]].to_numpy(),
        hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{y}: rank #%{customdata[0]}<br>%{customdata[1]}<extra></extra>"))
    fig.update_layout(height=max(260, 28 * len(syms)), margin=dict(l=10, r=10, t=10, b=10),
                      yaxis=dict(categoryorder="array", categoryarray=syms[::-1]))
    fig.update_xaxes(showgrid=False)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Latest ranking")
    last = prim[prim["step"] == prim["step"].max()].sort_values("rank", na_position="last")
    st.dataframe(last[["symbol", "rank", "score", "price", "selected", "reason"]], hide_index=True,
                 use_container_width=True)

    st.subheader("Selector vs random baseline")
    st.caption("Mean forward return of the picked symbols until the next rebalance, compared with a seeded random "
               "pick from the same filtered universe and with the equal-weight universe.")
    perf = selection_performance(db)
    if perf.empty:
        st.caption("Needs at least two rebalances.")
        return
    st.dataframe(perf, hide_index=True, use_container_width=True,
                 column_config={"avg_period_return_pct": st.column_config.NumberColumn("avg period return %",
                                                                                        format="%+.3f"),
                                "compounded_return_pct": st.column_config.NumberColumn("compounded %",
                                                                                        format="%+.2f")})
    p = perf[perf["account"] == acct]
    kind_color = {"primary": colors.get(acct, SERIES[0]), "random_shadow": BENCH, "universe": "#c3c2b7"}
    bar = go.Figure(go.Bar(x=p["kind"], y=p["compounded_return_pct"],
                           marker=dict(color=[kind_color.get(k, BENCH) for k in p["kind"]], cornerradius=4),
                           hovertemplate="%{x}: %{y:+.2f}%<extra></extra>"))
    bar.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="compounded forward return %",
                      bargap=0.5)
    st.plotly_chart(bar, use_container_width=True)


def health_tab(db, cfg) -> None:
    st_ = bot_state(db, cfg.heartbeat_stale_seconds)
    c = st.columns(4)
    c[0].metric("State", ("not running" if "stale" in st_["label"] else st_["state"]).capitalize(),
                help=st_["label"])
    c[1].metric("Last heartbeat", st_["heartbeat_age"])
    c[2].metric("Restarts / recoveries", st_["restarts"])
    errs = db.scalar("SELECT count(*) FROM events WHERE level='ERROR'", default=0)
    c[3].metric("Errors logged", errs)
    counts = db.query(f"SELECT type, count(*) n, max(ts) last FROM events WHERE type IN "
                      f"({','.join('?' * len(HEALTH_TYPES))}) GROUP BY type", tuple(HEALTH_TYPES))
    if counts:
        cdf = pd.DataFrame([dict(r) for r in counts])
        cdf["last"] = ts_to_dt(cdf["last"])
        st.dataframe(cdf, hide_index=True)
    df = pd.DataFrame([dict(r) for r in db.query(
        "SELECT ts, wall_ts, level, type, account, symbol, message FROM events WHERE level IN ('WARNING','ERROR') OR "
        f"type IN ({','.join('?' * len(HEALTH_TYPES))}) ORDER BY id DESC LIMIT 500", tuple(HEALTH_TYPES))])
    st.subheader("Errors, data failures, restarts and recoveries")
    if df.empty:
        st.caption("Nothing to report.")
        return
    df.insert(0, "time", ts_to_dt(df.pop("ts")).dt.strftime("%Y-%m-%d %H:%M UTC"))
    df["logged at"] = pd.to_datetime(df.pop("wall_ts"), unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
    st.dataframe(df, hide_index=True, use_container_width=True, height=480,
                 column_config={"message": st.column_config.TextColumn(width="large")})


if __name__ == "__main__":  # `streamlit run` executes this file as __main__
    main()
