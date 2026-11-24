"""Single-instance guard: an exclusive ``flock`` on ``<db>.lock``.

The OS releases the lock automatically when the process dies (even on
``kill -9``), so a stale lock file never blocks a restart.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class LockError(RuntimeError):
    """Another instance holds the lock."""


class InstanceLock:
    """Context manager holding an exclusive lock next to the database."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(str(db_path) + ".lock")
        self._fh = None

    def acquire(self) -> "InstanceLock":
        """Take the lock or raise :class:`LockError`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            pid = fh.read().strip() or "?"
            fh.close()
            raise LockError(
                f"another papertrader instance (pid {pid}) is already running against {self.path.with_suffix('')}; "
                "stop it first (two instances would corrupt the simulated account state)"
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        """Release the lock."""
        if self._fh:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def is_locked(self) -> bool:
        """True if some process currently holds the lock (does not take it)."""
        if not self.path.exists():
            return False
        with open(self.path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
