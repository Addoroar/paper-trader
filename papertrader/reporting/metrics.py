"""Performance metrics computed from the state database.

All functions take a :class:`~papertrader.storage.db.Database` (read-only is
fine) so the CLI, the dashboard and the HTML report share one implementation.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from papertrader.storage.db import Database

YEAR = 365 * 86400


def compute_metrics(equity: pd.Series, realized: pd.Series, fees: float, start_value: float) -> dict:
    """Metrics for one equity curve.

    Args:
        equity: equity values indexed by epoch seconds, ascending.
        realized: realized P&L of each closing (SELL) fill.
        fees: total fees paid.
        start_value: initial capital (the account allocation).

    Returns a dict with ``return_pct``, ``max_drawdown_pct``, ``sharpe``,
    ``win_rate_pct``, ``profit_factor``, ``fees``, ``trades`` and ``equity``.
    Sharpe is annualised from the observed bar frequency, with a zero risk-free rate.
    """
    out = {"equity": start_value, "return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0,
           "win_rate_pct": None, "profit_factor": None, "fees": float(fees), "trades": int(len(realized))}
    if len(equity):
        eq = equity.astype(float)
        out["equity"] = float(eq.iloc[-1])
        out["return_pct"] = (float(eq.iloc[-1]) / start_value - 1) * 100
        curve = pd.concat([pd.Series([start_value]), eq.reset_index(drop=True)])
        peak = curve.cummax()
        out["max_drawdown_pct"] = float(((curve / peak) - 1).min() * 100)
        rets = eq.pct_change().dropna()
        span = (eq.index[-1] - eq.index[0]) if len(eq) > 1 else 0
        if len(rets) > 1 and span > 0 and rets.std() > 0:
            per_year = len(rets) / (span / YEAR)
            out["sharpe"] = float(rets.mean() / rets.std() * math.sqrt(per_year))
    if len(realized):
        wins, losses = realized[realized > 0], realized[realized < 0]
        out["win_rate_pct"] = float(len(wins) / len(realized) * 100)
        out["profit_factor"] = float(wins.sum() / -losses.sum()) if len(losses) else (math.inf if len(wins) else None)
    return out


def equity_curve(db: Database, account: str) -> pd.Series:
    """Equity snapshots of ``account`` indexed by ts."""
    rows = db.query("SELECT ts, equity FROM equity_snapshots WHERE account=? ORDER BY ts", (account,))
    return pd.Series([r["equity"] for r in rows], index=[r["ts"] for r in rows], dtype=float)


def benchmark_curve(db: Database) -> pd.Series:
    """Equal-weight universe buy-and-hold curve (scaled to the total allocation)."""
    rows = db.query("SELECT ts, value FROM benchmark ORDER BY ts")
    return pd.Series([r["value"] for r in rows], index=[r["ts"] for r in rows], dtype=float)


def account_summary(db: Database) -> pd.DataFrame:
    """One row per account: cash, equity, open positions and all metrics."""
    rows = []
    for a in db.query("SELECT name, strategy, allocation, cash FROM accounts ORDER BY name"):
        name = a["name"]
        eq = equity_curve(db, name)
        fills = db.query("SELECT side, realized_pnl, fee FROM fills WHERE account=?", (name,))
        realized = pd.Series([f["realized_pnl"] for f in fills if f["side"] == "SELL"], dtype=float)
        fees = sum(f["fee"] for f in fills)
        m = compute_metrics(eq, realized, fees, a["allocation"])
        npos = db.scalar("SELECT count(*) FROM positions WHERE account=?", (name,), 0)
        rows.append({"account": name, "strategy": a["strategy"], "allocation": a["allocation"], "cash": a["cash"],
                     "open_positions": npos, "realized_pnl": float(realized.sum()), "fills": len(fills), **m})
    return pd.DataFrame(rows)


def benchmark_return_pct(db: Database) -> float | None:
    """Return of the universe benchmark since the first tick."""
    b = benchmark_curve(db)
    if len(b) < 1:
        return None
    total = db.scalar("SELECT sum(allocation) FROM accounts", default=0.0)
    return (float(b.iloc[-1]) / total - 1) * 100 if total else None


def trades_frame(db: Database) -> pd.DataFrame:
    """All fills joined with their order (kind and reason)."""
    rows = db.query(
        "SELECT f.ts, f.bar_ts, f.account, f.symbol, f.side, f.qty, f.price, f.fee, f.realized_pnl, o.kind,"
        " o.reason, f.order_id FROM fills f JOIN orders o USING(order_id) ORDER BY f.ts, f.rowid"
    )
    df = pd.DataFrame([dict(r) for r in rows],
                      columns=["ts", "bar_ts", "account", "symbol", "side", "qty", "price", "fee", "realized_pnl",
                               "kind", "reason", "order_id"])
    if not df.empty:
        df.insert(0, "time", pd.to_datetime(df["ts"], unit="s", utc=True))
        df.loc[df["side"] == "BUY", "realized_pnl"] = np.nan
    return df


def selection_performance(db: Database) -> pd.DataFrame:
    """How the selector's picks did until the next rebalance, vs a seeded random pick and the universe average.

    For each rebalance, the forward return of a symbol is its price at the next
    rebalance divided by its price at this one. Returns one row per account
    and kind (``primary`` / ``random_shadow`` / ``universe``) with the mean
    forward return per period, the number of periods, and the compounded total.
    """
    rows = db.query("SELECT account, step, kind, method, symbol, price, selected FROM selections "
                    "WHERE price IS NOT NULL ORDER BY account, step")
    if not rows:
        return pd.DataFrame(columns=["account", "kind", "method", "periods", "avg_period_return_pct",
                                     "compounded_return_pct"])
    df = pd.DataFrame([dict(r) for r in rows])
    out = []
    for account, g in df.groupby("account"):
        prim = g[g["kind"] == "primary"]
        steps = sorted(prim["step"].unique())
        price_at = {s: dict(zip(prim[prim["step"] == s]["symbol"], prim[prim["step"] == s]["price"])) for s in steps}
        series: dict[tuple[str, str], list[float]] = {}
        for s0, s1 in zip(steps, steps[1:]):
            p0, p1 = price_at[s0], price_at[s1]
            fwd = {sym: p1[sym] / p0[sym] - 1 for sym in p0 if sym in p1 and p0[sym]}
            if not fwd:
                continue
            series.setdefault(("universe", "equal weight"), []).append(float(np.mean(list(fwd.values()))))
            for kind, gk in g[g["step"] == s0].groupby("kind"):
                picks = [fwd[sym] for sym in gk[gk["selected"] == 1]["symbol"] if sym in fwd]
                if picks:
                    series.setdefault((kind, gk["method"].iloc[0]), []).append(float(np.mean(picks)))
        for (kind, method), vals in series.items():
            out.append({"account": account, "kind": kind, "method": method, "periods": len(vals),
                        "avg_period_return_pct": float(np.mean(vals) * 100),
                        "compounded_return_pct": float((np.prod([1 + v for v in vals]) - 1) * 100)})
    return pd.DataFrame(out)
