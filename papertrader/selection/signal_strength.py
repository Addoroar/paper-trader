"""Rank symbols by how strongly the account's own strategy wants to buy them."""

from __future__ import annotations

import pandas as pd

from papertrader.models import TickContext
from papertrader.selection.base import SELECTORS, Selector


@SELECTORS.register("signal_strength")
class SignalStrength(Selector):
    """Uses ``Strategy.strength`` on the full available history."""

    def score(self, symbol: str, window: pd.DataFrame, history: pd.DataFrame, ctx: TickContext) -> tuple[float, str]:
        if self.strategy is None:
            return float("nan"), "no strategy"
        s = self.strategy.strength(history)
        return s, f"{self.strategy.name} signal strength {s:+.2f}"
