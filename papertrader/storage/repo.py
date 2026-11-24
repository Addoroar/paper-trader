"""Typed read/write helpers over the state database."""

from __future__ import annotations

import json
import time

from papertrader.models import Fill, Order, OrderStatus, Position, Side
from papertrader.storage.db import Database


def ensure_account(db: Database, name: str, strategy: str, params: dict, allocation: float) -> None:
    """Create the account row on first run (never resets an existing account)."""
    db.execute(
        "INSERT OR IGNORE INTO accounts(name, strategy, params_json, allocation, cash, step, created_at)"
        " VALUES (?,?,?,?,?,0,?)",
        (name, strategy, json.dumps(params, sort_keys=True), allocation, allocation, int(time.time())),
    )


def get_cash(db: Database, account: str) -> float:
    """Current cash of ``account``."""
    return float(db.scalar("SELECT cash FROM accounts WHERE name=?", (account,), 0.0))


def get_step(db: Database, account: str) -> int:
    """Number of completed decision rounds for ``account``."""
    return int(db.scalar("SELECT step FROM accounts WHERE name=?", (account,), 0))


def get_positions(db: Database, account: str) -> dict[str, Position]:
    """Open positions of ``account`` keyed by symbol."""
    rows = db.query("SELECT * FROM positions WHERE account=? ORDER BY symbol", (account,))
    return {
        r["symbol"]: Position(r["account"], r["symbol"], r["qty"], r["avg_cost"], r["opened_ts"], r["last_bar_ts"])
        for r in rows
    }


def get_position(db: Database, account: str, symbol: str) -> Position | None:
    """One position or None."""
    return get_positions(db, account).get(symbol)


def row_to_order(r) -> Order:
    """Convert an ``orders`` row."""
    return Order(
        order_id=r["order_id"], account=r["account"], symbol=r["symbol"], side=Side(r["side"]),
        qty=r["qty"], ref_price=r["ref_price"], bar_ts=r["bar_ts"], kind=r["kind"],
        status=OrderStatus(r["status"]), reason=r["reason"], created_ts=r["created_ts"],
        reject_reason=r["reject_reason"],
    )


def get_order(db: Database, order_id: str) -> Order | None:
    """Look up an order by id."""
    r = db.one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    return row_to_order(r) if r else None


def pending_orders(db: Database) -> list[Order]:
    """All orders placed but not yet filled/rejected/cancelled."""
    return [row_to_order(r) for r in db.query("SELECT * FROM orders WHERE status='PENDING' ORDER BY created_ts")]


def get_fill(db: Database, order_id: str) -> Fill | None:
    """The fill of ``order_id`` if any."""
    r = db.one("SELECT * FROM fills WHERE order_id=?", (order_id,))
    if not r:
        return None
    return Fill(r["order_id"], r["account"], r["symbol"], Side(r["side"]), r["qty"], r["price"],
                r["fee"], r["realized_pnl"], r["bar_ts"], r["ts"])


def get_cursor(db: Database, account: str, symbol: str) -> int:
    """Last processed bar open time for (account, symbol); 0 if none."""
    return int(db.scalar("SELECT last_bar_ts FROM cursors WHERE account=? AND symbol=?", (account, symbol), 0))


def set_cursor(db: Database, account: str, symbol: str, bar_ts: int) -> None:
    """Advance the cursor (never moves backwards)."""
    db.execute(
        "INSERT INTO cursors(account, symbol, last_bar_ts) VALUES(?,?,?) "
        "ON CONFLICT(account, symbol) DO UPDATE SET last_bar_ts=max(last_bar_ts, excluded.last_bar_ts)",
        (account, symbol, bar_ts),
    )
