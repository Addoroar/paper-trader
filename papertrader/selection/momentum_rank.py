"""Rank symbols by their return over the lookback window."""

from __future__ import annotations

import pandas as pd

from papertrader.models import TickContext
from papertrader.selection.base import SELECTORS, Selector


@SELECTORS.register("momentum_rank")
class MomentumRank(Selector):
    """Highest trailing return first."""

    def score(self, symbol: str, window: pd.DataFrame, history: pd.DataFrame, ctx: TickContext) -> tuple[float, str]:
        c = window["close"].to_numpy()
        r = c[-1] / c[0] - 1
        return r, f"{r * 100:+.1f}% over {len(c) - 1} bars"
