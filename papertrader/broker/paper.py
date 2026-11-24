"""Simulated broker: cash, positions, average cost, fees, slippage and P&L.

Orders go through two durable steps, each its own SQLite transaction:

1. :meth:`PaperBroker.place_order` inserts the order as ``PENDING``. The order
   id is deterministic (account + symbol + bar + side), and the insert is
   ``INSERT OR IGNORE``, so replaying a tick re-finds the existing order
   instead of creating a second one.
2. :meth:`PaperBroker.fill_order` atomically writes the fill, updates cash and
   the position, and marks the order ``FILLED`` (or ``REJECTED`` if it is
   unaffordable / would short). Filling an already-resolved order is a no-op.

The fill price depends only on the stored ``ref_price`` and side, so a pending
order left behind by a crash fills at exactly the price it would have had.
Long-only, no leverage.
"""

from __future__ import annotations

import hashlib

from papertrader import faults
from papertrader.logging_setup import ORDER_FILLED, ORDER_PLACED, ORDER_REJECTED, EventLogger
from papertrader.models import Fill, Order, OrderStatus, Side, is_crypto
from papertrader.storage import repo
from papertrader.storage.db import Database

EPS = 1e-9


def make_order_id(account: str, symbol: str, bar_ts: int, side: Side | str) -> str:
    """Deterministic order id: same account/symbol/bar/side -> same id."""
    raw = f"{account}|{symbol}|{int(bar_ts)}|{Side(side).value}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def round_qty(symbol: str, qty: float) -> float:
    """Whole shares for stocks, 6 decimals for crypto (always rounded down)."""
    if is_crypto(symbol):
        return int(qty * 1e6 + EPS) / 1e6
    return float(int(qty + EPS))


class PaperBroker:
    """Executes simulated orders against the state database."""

    def __init__(self, db: Database, events: EventLogger, fees_pct: float, slippage_pct: float) -> None:
        self.db = db
        self.events = events
        self.fee_rate = fees_pct / 100.0
        self.slip_rate = slippage_pct / 100.0

    # -- pricing ------------------------------------------------------
    def fill_price(self, side: Side, ref_price: float) -> float:
        """Reference price moved against us by the slippage."""
        return ref_price * (1 + self.slip_rate) if side == Side.BUY else ref_price * (1 - self.slip_rate)

    def max_affordable_qty(self, symbol: str, cash: float, ref_price: float) -> float:
        """Largest quantity whose cost (with slippage and fee) fits in ``cash``."""
        unit = self.fill_price(Side.BUY, ref_price) * (1 + self.fee_rate)
        return round_qty(symbol, max(cash, 0.0) / unit) if unit > 0 else 0.0

    # -- order lifecycle ------------------------------------------------
    def place_order(
        self,
        account: str,
        symbol: str,
        side: Side,
        qty: float,
        ref_price: float,
        bar_ts: int,
        *,
        kind: str,
        reason: str,
        now: int,
    ) -> tuple[Order, bool]:
        """Record an order as PENDING. Returns ``(order, created)``.

        If an order with the same deterministic id exists, it is returned
        unchanged with ``created=False`` (idempotent replay).
        """
        oid = make_order_id(account, symbol, bar_ts, side)
        with self.db.tx():
            cur = self.db.execute(
                "INSERT OR IGNORE INTO orders(order_id, account, symbol, side, qty, ref_price, bar_ts, kind,"
                " status, reason, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (oid, account, symbol, Side(side).value, float(qty), float(ref_price), int(bar_ts), kind,
                 OrderStatus.PENDING.value, reason, int(now)),
            )
            created = cur.rowcount == 1
            if created:
                self.events.log(
                    ORDER_PLACED,
                    f"{side} {qty:g} {symbol} @ ref {ref_price:.4f} ({kind})",
                    account=account, symbol=symbol, order_id=oid, side=str(side), qty=qty,
                    ref_price=ref_price, bar_ts=bar_ts, kind=kind,
                )
        order = repo.get_order(self.db, oid)
        assert order is not None
        return order, created

    def fill_order(self, order_id: str, now: int) -> Fill | None:
        """Apply a PENDING order in one transaction. Returns the fill or None if rejected/already done."""
        with self.db.tx():
            order = repo.get_order(self.db, order_id)
            if order is None:
                raise KeyError(f"no such order {order_id}")
            if order.status != OrderStatus.PENDING:
                return repo.get_fill(self.db, order_id) if order.status == OrderStatus.FILLED else None
            reject = self._validate(order)
            if reject:
                self.db.execute(
                    "UPDATE orders SET status='REJECTED', reject_reason=? WHERE order_id=?", (reject, order_id)
                )
                self.events.log(
                    ORDER_REJECTED, f"{order.side} {order.qty:g} {order.symbol} rejected: {reject}",
                    level="WARNING", account=order.account, symbol=order.symbol, order_id=order_id, reason=reject,
                )
                return None
            fill = self._apply(order, now)
            self.events.log(
                ORDER_FILLED,
                f"{order.side} {fill.qty:g} {order.symbol} filled @ {fill.price:.4f}, fee {fill.fee:.2f}"
                + (f", realized P&L {fill.realized_pnl:+.2f}" if order.side == Side.SELL else ""),
                account=order.account, symbol=order.symbol, order_id=order_id, side=str(order.side),
                qty=fill.qty, price=fill.price, fee=fill.fee, realized_pnl=fill.realized_pnl, kind=order.kind,
            )
            return fill

    def execute(self, account: str, symbol: str, side: Side, qty: float, ref_price: float, bar_ts: int, *,
                kind: str, reason: str, now: int) -> tuple[Order, Fill | None]:
        """Place then fill an order (two transactions, with a crash-test fault point between)."""
        order, _ = self.place_order(account, symbol, side, qty, ref_price, bar_ts, kind=kind, reason=reason, now=now)
        faults.maybe_fault("after_place")
        fill = self.fill_order(order.order_id, now)
        faults.maybe_fault("after_fill")
        return repo.get_order(self.db, order.order_id) or order, fill

    def cancel_order(self, order_id: str, reason: str) -> None:
        """Cancel a PENDING order (used by recovery for stale orders)."""
        with self.db.tx():
            self.db.execute(
                "UPDATE orders SET status='CANCELLED', reject_reason=? WHERE order_id=? AND status='PENDING'",
                (reason, order_id),
            )

    # -- internals ------------------------------------------------------
    def _validate(self, order: Order) -> str | None:
        if order.qty <= 0:
            return "quantity must be positive"
        if order.ref_price <= 0:
            return "invalid price"
        cash = repo.get_cash(self.db, order.account)
        if order.side == Side.BUY:
            price = self.fill_price(Side.BUY, order.ref_price)
            cost = order.qty * price * (1 + self.fee_rate)
            if cost > cash + 1e-6:
                return f"insufficient funds: need {cost:.2f}, have {cash:.2f}"
        else:
            pos = repo.get_position(self.db, order.account, order.symbol)
            held = pos.qty if pos else 0.0
            if order.qty > held + EPS:
                return f"cannot sell {order.qty:g}, only {held:g} held (long-only, no shorting)"
        return None

    def _apply(self, order: Order, now: int) -> Fill:
        price = self.fill_price(order.side, order.ref_price)
        notional = order.qty * price
        fee = notional * self.fee_rate
        cash = repo.get_cash(self.db, order.account)
        pos = repo.get_position(self.db, order.account, order.symbol)
        realized = 0.0
        if order.side == Side.BUY:
            cash -= notional + fee
            if pos:
                new_qty = pos.qty + order.qty
                avg = (pos.qty * pos.avg_cost + notional + fee) / new_qty
                self.db.execute(
                    "UPDATE positions SET qty=?, avg_cost=?, last_bar_ts=? WHERE account=? AND symbol=?",
                    (new_qty, avg, order.bar_ts, order.account, order.symbol),
                )
            else:
                self.db.execute(
                    "INSERT INTO positions(account, symbol, qty, avg_cost, opened_ts, last_bar_ts) VALUES (?,?,?,?,?,?)",
                    (order.account, order.symbol, order.qty, (notional + fee) / order.qty, order.bar_ts, order.bar_ts),
                )
        else:
            assert pos is not None
            proceeds = notional - fee
            cash += proceeds
            realized = proceeds - pos.avg_cost * order.qty
            remaining = pos.qty - order.qty
            if remaining <= EPS:
                self.db.execute("DELETE FROM positions WHERE account=? AND symbol=?", (order.account, order.symbol))
            else:
                self.db.execute(
                    "UPDATE positions SET qty=? WHERE account=? AND symbol=?", (remaining, order.account, order.symbol)
                )
        self.db.execute("UPDATE accounts SET cash=? WHERE name=?", (cash, order.account))
        self.db.execute("UPDATE orders SET status='FILLED' WHERE order_id=?", (order.order_id,))
        self.db.execute(
            "INSERT INTO fills(order_id, account, symbol, side, qty, price, fee, realized_pnl, cash_after, bar_ts, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (order.order_id, order.account, order.symbol, order.side.value, order.qty, price, fee, realized, cash,
             order.bar_ts, int(now)),
        )
        return Fill(order.order_id, order.account, order.symbol, order.side, order.qty, price, fee, realized,
                    order.bar_ts, int(now))
