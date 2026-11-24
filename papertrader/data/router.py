"""Routes each symbol to the right data source."""

from __future__ import annotations

import pandas as pd

from papertrader.data.base import DataFeed
from papertrader.models import is_crypto


class RoutedFeed(DataFeed):
    """Stocks go to ``stocks``, crypto pairs (``BASE/QUOTE``) go to ``crypto``."""

    name = "routed"

    def __init__(self, stocks: DataFeed, crypto: DataFeed) -> None:
        super().__init__()
        self.stocks = stocks
        self.crypto = crypto

    def _pick(self, symbol: str) -> DataFeed:
        feed = self.crypto if is_crypto(symbol) else self.stocks
        feed.on_event = self.on_event
        return feed

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> pd.DataFrame:
        return self._pick(symbol).fetch_bars(symbol, timeframe, start, end)

    def get_bars(self, symbol: str, timeframe: str, now: int, limit: int) -> pd.DataFrame:
        return self._pick(symbol).get_bars(symbol, timeframe, now, limit)
