"""Versioned schema migrations tracked with ``PRAGMA user_version``.

To change the schema, append a new SQL script to :data:`MIGRATIONS`; never
edit an existing one. Each migration runs in its own transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from papertrader.storage.db import Database

MIGRATIONS: list[str] = [
    # v1: initial schema
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

    CREATE TABLE accounts (
        name TEXT PRIMARY KEY,
        strategy TEXT NOT NULL,
        params_json TEXT NOT NULL,
        allocation REAL NOT NULL,
        cash REAL NOT NULL,
        step INTEGER NOT NULL DEFAULT 0,          -- decision rounds completed
        created_at INTEGER NOT NULL
    );

    CREATE TABLE positions (
        account TEXT NOT NULL REFERENCES accounts(name),
        symbol TEXT NOT NULL,
        qty REAL NOT NULL,
        avg_cost REAL NOT NULL,
        opened_ts INTEGER NOT NULL,
        last_bar_ts INTEGER NOT NULL,
        PRIMARY KEY (account, symbol)
    );

    CREATE TABLE orders (
        order_id TEXT PRIMARY KEY,                -- deterministic: account|symbol|bar_ts|side
        account TEXT NOT NULL REFERENCES accounts(name),
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        qty REAL NOT NULL,
        ref_price REAL NOT NULL,
        bar_ts INTEGER NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PENDING','FILLED','REJECTED','CANCELLED')),
        reason TEXT NOT NULL,
        reject_reason TEXT,
        created_ts INTEGER NOT NULL
    );
    CREATE INDEX orders_account ON orders(account, bar_ts);
    CREATE INDEX orders_status ON orders(status);

    CREATE TABLE fills (
        order_id TEXT PRIMARY KEY REFERENCES orders(order_id),  -- at most one fill per order
        account TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty REAL NOT NULL,
        price REAL NOT NULL,
        fee REAL NOT NULL,
        realized_pnl REAL NOT NULL,
        cash_after REAL NOT NULL,
        bar_ts INTEGER NOT NULL,
        ts INTEGER NOT NULL
    );
    CREATE INDEX fills_account ON fills(account, ts);

    CREATE TABLE cursors (
        account TEXT NOT NULL,
        symbol TEXT NOT NULL,
        last_bar_ts INTEGER NOT NULL,             -- last fully processed bar (open time)
        PRIMARY KEY (account, symbol)
    );

    CREATE TABLE equity_snapshots (
        account TEXT NOT NULL,
        ts INTEGER NOT NULL,
        cash REAL NOT NULL,
        positions_value REAL NOT NULL,
        equity REAL NOT NULL,
        prices_json TEXT NOT NULL,
        PRIMARY KEY (account, ts)
    );

    CREATE TABLE benchmark (
        ts INTEGER PRIMARY KEY,
        value REAL NOT NULL                       -- equal-weight buy & hold of the universe
    );

    CREATE TABLE selections (
        account TEXT NOT NULL,
        step INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,                       -- 'primary' or 'random_shadow'
        method TEXT NOT NULL,
        symbol TEXT NOT NULL,
        rank INTEGER,
        score REAL,
        price REAL,
        selected INTEGER NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (account, step, kind, symbol)
    );
    CREATE INDEX selections_ts ON selections(ts);

    CREATE TABLE daily_state (
        account TEXT NOT NULL,
        day TEXT NOT NULL,
        start_equity REAL NOT NULL,
        halted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (account, day)
    );

    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,                      -- bot time (simulated time in backtests)
        wall_ts REAL NOT NULL,
        level TEXT NOT NULL,
        type TEXT NOT NULL,
        account TEXT,
        symbol TEXT,
        message TEXT NOT NULL,
        data_json TEXT,
        dedupe_key TEXT UNIQUE                    -- makes replayed decisions idempotent
    );
    CREATE INDEX events_ts ON events(ts);
    CREATE INDEX events_type ON events(type, ts);

    CREATE TABLE bot_status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state TEXT NOT NULL,                      -- running | stopped | recovering
        mode TEXT NOT NULL,                       -- live | backtest
        pid INTEGER,
        started_at REAL,
        heartbeat_at REAL,
        last_tick_ts INTEGER,
        clean_shutdown_at REAL,
        restarts INTEGER NOT NULL DEFAULT 0
    );
    """,
]


def migrate(db: "Database") -> int:
    """Apply pending migrations; returns the resulting schema version."""
    version = db.scalar("PRAGMA user_version", default=0)
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        db.conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in script.split(";"):
                if stmt.strip():
                    db.conn.execute(stmt)
            db.conn.execute(f"PRAGMA user_version = {i}")
            db.conn.execute("COMMIT")
        except BaseException:
            db.conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)
