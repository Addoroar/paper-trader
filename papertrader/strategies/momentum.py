"""Time-series momentum: buy when the trailing return exceeds a threshold."""

from __future__ import annotations

import pandas as pd
from pydantic import Field

from papertrader.models import Action, Position, Signal
from papertrader.strategies.base import STRATEGIES, Strategy, StrategyParams
from papertrader.strategies.indicators import pct_change


class MomentumParams(StrategyParams):
    lookback: int = Field(20, ge=1)
    entry_pct: float = Field(2.0, ge=0)  # buy if return over lookback > +entry_pct %
    exit_pct: float = Field(0.0)  # sell if return over lookback < exit_pct %


@STRATEGIES.register("momentum")
class Momentum(Strategy):
    """Ride trends: enter on strong trailing return, exit when it fades."""

    Params = MomentumParams

    @property
    def warmup_bars(self) -> int:
        return self.p.lookback + 1

    def on_bar(self, history: pd.DataFrame, position: Position | None) -> Signal:
        c = history["close"].to_numpy()
        if len(c) < self.warmup_bars:
            return Signal.hold(f"warming up ({len(c)}/{self.warmup_bars} bars)")
        r = pct_change(c, self.p.lookback) * 100
        strength = max(-1.0, min(1.0, r / 10))
        tag = f"{r:+.2f}% over {self.p.lookback} bars"
        if position is None and r > self.p.entry_pct:
            return Signal(Action.BUY, f"momentum {tag} > {self.p.entry_pct:g}%", strength=strength)
        if position is not None and r < self.p.exit_pct:
            return Signal(Action.SELL, f"momentum faded: {tag} < {self.p.exit_pct:g}%", strength=strength)
        return Signal.hold(f"momentum {tag}", strength)
