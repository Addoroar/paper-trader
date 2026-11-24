"""SQLite connection wrapper with explicit, nestable transactions.

The database runs in WAL mode with ``synchronous=FULL`` so that a committed
transaction survives power loss. All state changes happen inside
:meth:`Database.tx`, which issues ``BEGIN IMMEDIATE`` / ``COMMIT`` (or
``ROLLBACK`` on error). Nested ``tx()`` blocks join the outer transaction.

Side effects that must only happen once a change is durable (e.g. writing a
log line that says "order filled") are registered with :meth:`on_commit`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


class Database:
    """Thin wrapper over :class:`sqlite3.Connection`."""

    def __init__(self, path: str | Path, *, readonly: bool = False, durable: bool = True) -> None:
        """Open (and for writers, migrate) the database at ``path``.

        Args:
            path: database file.
            readonly: open with ``mode=ro`` (used by the dashboard).
            durable: ``synchronous=FULL`` if True, ``NORMAL`` otherwise (backtests).
        """
        self.path = Path(path)
        self.readonly = readonly
        if readonly:
            uri = f"file:{self.path.resolve()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute(f"PRAGMA synchronous={'FULL' if durable else 'NORMAL'}")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.row_factory = sqlite3.Row
        self._depth = 0
        self._callbacks: list[Callable[[], None]] = []
        if not readonly:
            from papertrader.storage.migrations import migrate

            migrate(self)

    # -- transactions -------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator["Database"]:
        """Run the block in a single transaction (nested blocks join the outer one)."""
        if self._depth > 0:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self
        except BaseException:
            self._depth = 0
            self._callbacks.clear()
            self.conn.execute("ROLLBACK")
            raise
        self._depth = 0
        self.conn.execute("COMMIT")
        callbacks, self._callbacks = self._callbacks, []
        for cb in callbacks:
            cb()

    @property
    def in_tx(self) -> bool:
        """True while inside :meth:`tx`."""
        return self._depth > 0

    def on_commit(self, cb: Callable[[], None]) -> None:
        """Run ``cb`` after the current transaction commits (immediately if none)."""
        if self._depth > 0:
            self._callbacks.append(cb)
        else:
            cb()

    # -- helpers ------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        """Execute one statement."""
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        """Return all rows."""
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        """Return the first row or None."""
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = (), default: Any = None) -> Any:
        """Return the first column of the first row, or ``default``."""
        row = self.conn.execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Read a value from the ``meta`` key/value table."""
        return self.scalar("SELECT value FROM meta WHERE key=?", (key,), default)

    def set_meta(self, key: str, value: Any) -> None:
        """Write a value to the ``meta`` key/value table."""
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()
