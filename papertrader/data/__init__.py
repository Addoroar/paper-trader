"""Market data: the ``DataFeed`` interface and its implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from papertrader.data.base import DataFeed, closed_only
from papertrader.data.historical import HistoricalFeed
from papertrader.data.synthetic import SyntheticFeed

if TYPE_CHECKING:
    from papertrader.config import Config

__all__ = ["DataFeed", "HistoricalFeed", "SyntheticFeed", "build_feed", "closed_only"]


def build_feed(cfg: "Config", feed: str | None = None) -> DataFeed:
    """Build the configured feed (``feed`` overrides ``cfg.data.feed``)."""
    kind = feed or cfg.data.feed
    if kind == "synthetic":
        return SyntheticFeed()
    from papertrader.data.cache import CachedFeed
    from papertrader.data.ccxt_feed import CcxtFeed
    from papertrader.data.router import RoutedFeed
    from papertrader.data.yfinance_feed import YFinanceFeed

    d = cfg.data
    return RoutedFeed(
        stocks=CachedFeed(YFinanceFeed(), d.cache_dir, d.max_retries, d.retry_base_delay),
        crypto=CachedFeed(CcxtFeed(d.crypto_exchange, d.crypto_exchange_fallbacks), d.cache_dir,
                          d.max_retries, d.retry_base_delay),
    )
