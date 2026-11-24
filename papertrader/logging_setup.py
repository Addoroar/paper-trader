"""Structured logging: JSON-lines file, readable console, and the ``events`` table.

Every event goes to three places:

* ``logs/papertrader.jsonl`` - one JSON object per line, size-rotated;
* the console - a short human-readable line;
* the ``events`` table in SQLite - so the dashboard can search it.

Inside a database transaction the file/console output is deferred until the
transaction commits, so the log never claims something that was rolled back
(e.g. by a crash between "order placed" and "order filled").
"""

from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from papertrader.storage.db import Database

LOGGER_NAME = "papertrader"

# Event types (documented in the README)
TICK_STARTED = "tick_started"
TICK_FINISHED = "tick_finished"
SYMBOLS_SELECTED = "symbols_selected"
SIGNAL = "signal"
HOLD = "hold"
ORDER_PLACED = "order_placed"
ORDER_FILLED = "order_filled"
ORDER_REJECTED = "order_rejected"
ORDER_CANCELLED = "order_cancelled"
TRADE_SKIPPED = "trade_skipped"
RISK_TRIGGERED = "risk_triggered"
DATA_ERROR = "data_fetch_error"
DATA_RETRY = "data_fetch_retry"
STARTUP = "startup"
SHUTDOWN = "shutdown"
RECOVERY = "recovered_from_interruption"
RECONCILIATION = "reconciliation"
GAP_REPLAY = "gap_replay"
DAILY_SUMMARY = "daily_summary"
ERROR = "error"

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class JsonLineFormatter(logging.Formatter):
    """Format records whose ``event`` attribute holds a dict as a JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "event", None) or {"message": record.getMessage()}
        payload = {"logged_at": round(record.created, 3), "level": record.levelname, **payload}
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Short readable console lines."""

    def format(self, record: logging.LogRecord) -> str:
        ev = getattr(record, "event", None)
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        if not ev:
            return f"{t} {record.levelname:<7} {record.getMessage()}"
        who = "/".join(x for x in (ev.get("account"), ev.get("symbol")) if x)
        who = f" [{who}]" if who else ""
        return f"{t} {record.levelname:<7} {ev['type']}{who}: {ev['message']}"


def setup_logging(
    log_dir: str | Path,
    level: str = "INFO",
    *,
    filename: str = "papertrader.jsonl",
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    console_level: str | None = None,
) -> logging.Logger:
    """Configure the ``papertrader`` logger (idempotent: replaces old handlers)."""
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(Path(log_dir) / filename, maxBytes=max_bytes, backupCount=backup_count)
    fh.setLevel(LEVELS[level])
    fh.setFormatter(JsonLineFormatter())
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(LEVELS[console_level or level])
    ch.setFormatter(ConsoleFormatter())
    logger.addHandler(ch)
    return logger


class EventLogger:
    """Records events to SQLite and the Python logger.

    Args:
        db: state database.
        clock: callable returning the bot's current time (epoch seconds).
    """

    def __init__(self, db: Database, clock=lambda: int(time.time())) -> None:
        self.db = db
        self.clock = clock
        self.logger = logging.getLogger(LOGGER_NAME)

    def log(
        self,
        type_: str,
        message: str,
        *,
        level: str = "INFO",
        account: str | None = None,
        symbol: str | None = None,
        dedupe_key: str | None = None,
        ts: int | None = None,
        **data: Any,
    ) -> bool:
        """Record one event. Returns False if ``dedupe_key`` was already recorded.

        ``dedupe_key`` makes logging idempotent: when a tick is replayed after a
        crash, decisions that were already recorded are not duplicated.
        """
        ts = int(ts if ts is not None else self.clock())
        data_json = json.dumps(data, default=str) if data else None
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events(ts, wall_ts, level, type, account, symbol, message, data_json, dedupe_key)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, time.time(), level, type_, account, symbol, message, data_json, dedupe_key),
        )
        if cur.rowcount == 0:
            return False
        event = {"ts": ts, "type": type_, "account": account, "symbol": symbol, "message": message}
        if data:
            event["data"] = data
        lvl = LEVELS[level]
        self.db.on_commit(lambda: self.logger.log(lvl, message, extra={"event": event}))
        return True
