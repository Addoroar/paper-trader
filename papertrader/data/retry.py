"""Retry with exponential backoff and jitter."""

from __future__ import annotations

import random
import time
from typing import Callable

from papertrader.logging_setup import DATA_ERROR, DATA_RETRY


class DataFetchError(RuntimeError):
    """Raised when a fetch keeps failing after all retries."""


def with_retry[T](
    fn: Callable[[], T],
    *,
    what: str,
    retries: int,
    base_delay: float,
    emit: Callable[..., None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    symbol: str | None = None,
) -> T:
    """Call ``fn``; on exception wait ``base_delay * 2**attempt`` (+jitter) and retry.

    Each failure is reported via ``emit`` as a ``data_fetch_retry`` event; the
    final failure as ``data_fetch_error`` and re-raised as :class:`DataFetchError`.
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - any network/library error is retryable
            if attempt >= retries:
                if emit:
                    emit(DATA_ERROR, f"{what} failed after {retries + 1} attempts: {type(e).__name__}: {e}",
                         level="ERROR", symbol=symbol)
                raise DataFetchError(f"{what}: {e}") from e
            delay = base_delay * (2**attempt) * (1 + random.random() * 0.25)
            if emit:
                emit(DATA_RETRY, f"{what} failed ({type(e).__name__}: {str(e)[:200]}); retry "
                     f"{attempt + 1}/{retries} in {delay:.1f}s", level="WARNING", symbol=symbol)
            sleep(delay)
    raise AssertionError("unreachable")
