"""Startup recovery: finish or cancel interrupted orders, then reconcile the books.

**Pending orders.** An order is committed as PENDING before its fill is
applied. If the process died in between, the order is still PENDING at
startup. If its bar is still the latest closed bar for the symbol, the fill
is completed at the same deterministic price it would have had. If newer bars
exist, the price is stale, so the order is cancelled and logged instead of
being filled as if it were current.

**Reconciliation.** For each account, cash and positions are rebuilt from
the immutable fill ledger (allocation − buys − fees + sells) and compared with
the stored values. Cash plus positions (at the prices of the last equity
snapshot) is also compared with that snapshot. Any mismatch is logged as a
``reconciliation`` event at ERROR level.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from papertrader import logging_setup as ev
from papertrader.storage import repo

if TYPE_CHECKING:
    from papertrader.engine.engine import Engine

TOL = 0.01


def resolve_pending_orders(engine: "Engine") -> list[tuple[str, str]]:
    """Fill or cancel every PENDING order. Returns ``[(order_id, 'filled'|'cancelled'|'rejected')]``."""
    out = []
    now = engine.clock()
    for order in repo.pending_orders(engine.db):
        try:
            bars = engine.feed.get_bars(order.symbol, engine.cfg.timeframe, now, 2)
            latest = int(bars.index[-1]) if not bars.empty else None
        except Exception:  # noqa: BLE001
            latest = None
        if latest is not None and order.bar_ts >= latest:
            fill = engine.broker.fill_order(order.order_id, now)
            verdict = "filled" if fill else "rejected"
            engine.log(ev.RECOVERY,
                       f"completed interrupted order {order.order_id}: {order.side} {order.qty:g} {order.symbol} "
                       f"(bar {order.bar_ts} is still current) -> {verdict}",
                       level="WARNING", account=order.account, symbol=order.symbol, order_id=order.order_id)
        else:
            why = ("its bar is no longer the latest; not filling on a stale price" if latest is not None
                   else "no market data available to confirm the price")
            engine.broker.cancel_order(order.order_id, f"cancelled on recovery: {why}")
            engine.log(ev.ORDER_CANCELLED,
                       f"interrupted order {order.order_id} ({order.side} {order.qty:g} {order.symbol}) cancelled: {why}",
                       level="WARNING", account=order.account, symbol=order.symbol, order_id=order.order_id)
            verdict = "cancelled"
        out.append((order.order_id, verdict))
    return out


def ledger(engine: "Engine", account: str, allocation: float) -> tuple[float, dict[str, float]]:
    """Cash and position quantities implied by the fill ledger alone."""
    cash = allocation
    qty: dict[str, float] = {}
    for f in engine.db.query("SELECT side, symbol, qty, price, fee FROM fills WHERE account=? ORDER BY rowid",
                             (account,)):
        if f["side"] == "BUY":
            cash -= f["qty"] * f["price"] + f["fee"]
            qty[f["symbol"]] = qty.get(f["symbol"], 0.0) + f["qty"]
        else:
            cash += f["qty"] * f["price"] - f["fee"]
            qty[f["symbol"]] = qty.get(f["symbol"], 0.0) - f["qty"]
    return cash, {s: q for s, q in qty.items() if abs(q) > 1e-9}


def reconcile(engine: "Engine") -> dict[str, list[str]]:
    """Check every account's books. Returns ``{account: [problems]}`` (empty lists if all good)."""
    results: dict[str, list[str]] = {}
    for acc in engine.db.query("SELECT name, allocation, cash FROM accounts ORDER BY name"):
        name = acc["name"]
        problems: list[str] = []
        cash_l, qty_l = ledger(engine, name, acc["allocation"])
        if abs(cash_l - acc["cash"]) > TOL:
            problems.append(f"cash {acc['cash']:.2f} != ledger {cash_l:.2f}")
        positions = repo.get_positions(engine.db, name)
        for sym in set(qty_l) | set(positions):
            have = positions[sym].qty if sym in positions else 0.0
            if abs(have - qty_l.get(sym, 0.0)) > 1e-6:
                problems.append(f"{sym} qty {have:g} != ledger {qty_l.get(sym, 0.0):g}")
        snap = engine.db.one("SELECT * FROM equity_snapshots WHERE account=? ORDER BY ts DESC LIMIT 1", (name,))
        note = "no equity snapshot yet"
        if snap:
            later = engine.db.scalar("SELECT count(*) FROM fills WHERE account=? AND ts>?", (name, snap["ts"]), 0)
            prices = json.loads(snap["prices_json"])
            if later:
                note = f"{later} fill(s) after the last snapshot (interrupted mid-round); snapshot comparison skipped"
            else:
                value = sum(p.qty * prices.get(s, p.avg_cost) for s, p in positions.items())
                eq = acc["cash"] + value
                if abs(eq - snap["equity"]) > TOL:
                    problems.append(f"cash+positions {eq:.2f} != last snapshot equity {snap['equity']:.2f}")
                note = f"cash+positions {eq:.2f} matches last snapshot {snap['equity']:.2f}"
        results[name] = problems
        if problems:
            engine.log(ev.RECONCILIATION, f"DISCREPANCY in {name}: " + "; ".join(problems), level="ERROR",
                       account=name, ok=False, problems=problems)
        else:
            engine.log(ev.RECONCILIATION,
                       f"{name} OK: cash {acc['cash']:.2f} and {len(positions)} position(s) match the fill ledger; {note}",
                       account=name, ok=True)
    return results
