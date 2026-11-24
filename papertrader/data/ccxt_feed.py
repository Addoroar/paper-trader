"""Crypto via ccxt public endpoints (no API keys, read-only market data)."""

from __future__ import annotations

import pandas as pd

from papertrader.data.base import DataFeed, empty_bars, normalize
from papertrader.models import TIMEFRAME_SECONDS


class CcxtFeed(DataFeed):
    """OHLCV from a ccxt exchange, falling back to other exchanges if unreachable.

    Only public methods (``load_markets``, ``fetch_ohlcv``) are used; no
    credentials are configured, so trading endpoints could not work even by
    accident.
    """

    name = "ccxt"

    def __init__(self, exchange: str = "binance", fallbacks: list[str] | None = None) -> None:
        super().__init__()
        self.candidates = [exchange, *(fallbacks or [])]
        self._ex = None

    def _exchange(self):
        if self._ex is not None:
            return self._ex
        import ccxt

        errors = []
        for ex_id in self.candidates:
            try:
                ex = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 20000})
                ex.load_markets()
                self._ex = ex
                if ex_id != self.candidates[0]:
                    self.emit("data_fetch_error", f"crypto exchange {self.candidates[0]} unreachable; using {ex_id}",
                              level="WARNING")
                return ex
            except Exception as e:  # noqa: BLE001
                errors.append(f"{ex_id}: {type(e).__name__}: {str(e)[:120]}")
        raise ConnectionError("no crypto exchange reachable: " + "; ".join(errors))

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        ex = self._exchange()
        tf = TIMEFRAME_SECONDS[timeframe]
        rows: list[list[float]] = []
        since = start * 1000
        while since <= end * 1000:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            nxt = int(batch[-1][0]) + tf * 1000
            if nxt <= since:
                break
            since = nxt
            if len(batch) < 2:
                break
        if not rows:
            return empty_bars()
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = (df["ts"] // 1000).astype("int64")
        df = normalize(df.set_index("ts"))
        return df[(df.index >= start) & (df.index <= end)]
