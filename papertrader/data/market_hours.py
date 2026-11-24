"""Exchange hours: US stocks trade 09:30-16:00 America/New_York on NYSE trading days;
crypto trades 24/7.

Holidays are a hard-coded NYSE list for 2024-2027 (extend :data:`NYSE_HOLIDAYS`
for later years). Early-close days are treated as full days, a documented
simplification.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np

from papertrader.models import is_crypto

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
OPEN, CLOSE = time(9, 30), time(16, 0)

NYSE_HOLIDAYS: frozenset[date] = frozenset(
    date.fromisoformat(d)
    for d in [
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04",
        "2024-09-02", "2024-11-28", "2024-12-25",
        "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
        "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
        "2026-09-07", "2026-11-26", "2026-12-25",
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
        "2027-09-06", "2027-11-25", "2027-12-24",
    ]
)


def is_trading_day(d: date) -> bool:
    """True for NYSE trading days."""
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


@lru_cache(maxsize=4096)
def session_bounds(d: date) -> tuple[int, int]:
    """(open, close) epoch seconds of the NYSE session on date ``d``."""
    o = datetime.combine(d, OPEN, NY).timestamp()
    c = datetime.combine(d, CLOSE, NY).timestamp()
    return int(o), int(c)


def ny_date(ts: int) -> date:
    """Calendar date in New York of epoch ``ts``."""
    return datetime.fromtimestamp(ts, NY).date()


def ny_midnight(d: date) -> int:
    """Epoch seconds of midnight New York time on ``d`` (daily stock bars are stamped with this)."""
    return int(datetime.combine(d, time(0), NY).timestamp())


def is_market_open(symbol: str, now: int) -> bool:
    """Whether ``symbol`` trades at ``now``."""
    if is_crypto(symbol):
        return True
    d = ny_date(now)
    if not is_trading_day(d):
        return False
    o, c = session_bounds(d)
    return o <= now < c


def last_session_close(now: int) -> int:
    """Most recent NYSE session close at or before ``now``."""
    d = ny_date(now)
    for _ in range(15):
        if is_trading_day(d):
            c = session_bounds(d)[1]
            if c <= now:
                return c
        d -= timedelta(days=1)
    return 0


def bar_close_times(symbol: str, ts: np.ndarray, tf_seconds: int) -> np.ndarray:
    """Close time of each bar (open times ``ts``).

    Crypto bars close at ``open + timeframe``. Stock bars additionally close no
    later than the session close (the 15:30 hourly bar closes at 16:00), and
    daily stock bars close at that day's 16:00 New York.
    """
    ts = np.asarray(ts, dtype=np.int64)
    if is_crypto(symbol) or len(ts) == 0:
        return ts + tf_seconds
    out = np.empty_like(ts)
    cache: dict[date, int] = {}
    for i, t in enumerate(ts):
        d = ny_date(int(t))
        c = cache.get(d)
        if c is None:
            c = cache[d] = session_bounds(d)[1]
        out[i] = c if tf_seconds >= 86400 else min(int(t) + tf_seconds, c)
    return out


def stock_bar_grid(start: int, end: int, tf_seconds: int) -> list[int]:
    """Open times of all stock bars in [start, end), as yfinance would produce them."""
    out: list[int] = []
    d = ny_date(start)
    last = ny_date(end)
    while d <= last:
        if is_trading_day(d):
            if tf_seconds >= 86400:
                t = ny_midnight(d)
                if start <= t < end:
                    out.append(t)
            else:
                o, c = session_bounds(d)
                t = o
                while t < c:
                    if start <= t < end:
                        out.append(t)
                    t += tf_seconds
        d += timedelta(days=1)
    return out
