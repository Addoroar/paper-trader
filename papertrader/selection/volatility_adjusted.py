"""Rank symbols by risk-adjusted momentum (mean return / volatility)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from papertrader.models import TickContext
from papertrader.selection.base import SELECTORS, Selector


@SELECTORS.register("volatility_adjusted")
class VolatilityAdjusted(Selector):
    """Trailing return divided by the standard deviation of bar returns."""

    def score(self, symbol: str, window: pd.DataFrame, history: pd.DataFrame, ctx: TickContext) -> tuple[float, str]:
        c = window["close"].to_numpy()
        rets = np.diff(c) / c[:-1]
        vol = float(rets.std())
        total = c[-1] / c[0] - 1
        if vol == 0:
            return float("nan"), "zero volatility"
        s = float(rets.mean() / vol * np.sqrt(len(rets)))
        return s, f"risk-adjusted score {s:.2f} (return {total * 100:+.1f}%, vol {vol * 100:.2f}%/bar)"
