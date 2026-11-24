"""Buy-and-hold benchmark: buy once, never sell."""

from __future__ import annotations

import pandas as pd
from pydantic import Field

from papertrader.models import Action, Position, Signal
from papertrader.strategies.base import STRATEGIES, Strategy, StrategyParams


class BuyHoldParams(StrategyParams):
    size_pct: float | None = Field(None, gt=0, le=1)  # fraction of equity per position (default: risk max)


@STRATEGIES.register("buy_and_hold")
class BuyAndHold(Strategy):
    """The benchmark: buys each symbol it is given once and holds it forever.

    Pair it with a per-account ``risk`` override that disables stop-loss and
    take-profit to get a true passive benchmark.
    """

    Params = BuyHoldParams

    @property
    def warmup_bars(self) -> int:
        return 1

    def on_bar(self, history: pd.DataFrame, position: Position | None) -> Signal:
        if position is None:
            return Signal(Action.BUY, "buy and hold: initial entry", size_pct=self.p.size_pct, strength=0.5)
        return Signal.hold("buy and hold: holding", 0.5)
