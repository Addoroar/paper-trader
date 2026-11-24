"""Moving-average crossover: buy when the fast SMA crosses above the slow one."""

from __future__ import annotations

import pandas as pd
from pydantic import Field, model_validator

from papertrader.models import Action, Position, Signal
from papertrader.strategies.base import STRATEGIES, Strategy, StrategyParams
from papertrader.strategies.indicators import sma


class SmaParams(StrategyParams):
    fast: int = Field(10, ge=1)
    slow: int = Field(30, ge=2)
    require_cross: bool = True  # False: also enter whenever fast > slow while flat

    @model_validator(mode="after")
    def _order(self) -> "SmaParams":
        if self.fast >= self.slow:
            raise ValueError("fast must be smaller than slow")
        return self


@STRATEGIES.register("sma_crossover")
class SmaCrossover(Strategy):
    """BUY on a golden cross, SELL on a death cross."""

    Params = SmaParams

    @property
    def warmup_bars(self) -> int:
        return self.p.slow + 1

    def on_bar(self, history: pd.DataFrame, position: Position | None) -> Signal:
        c = history["close"].to_numpy()
        if len(c) < self.warmup_bars:
            return Signal.hold(f"warming up ({len(c)}/{self.warmup_bars} bars)")
        f, s = sma(c, self.p.fast), sma(c, self.p.slow)
        fn, sn, fp, sp = f[-1], s[-1], f[-2], s[-2]
        tag = f"SMA{self.p.fast} {fn:.2f} vs SMA{self.p.slow} {sn:.2f}"
        strength = max(-1.0, min(1.0, (fn / sn - 1) * 20))
        crossed_up = fp <= sp and fn > sn
        crossed_down = fp >= sp and fn < sn
        if position is None:
            if crossed_up:
                return Signal(Action.BUY, f"SMA{self.p.fast} crossed above SMA{self.p.slow} ({tag})", strength=strength)
            if not self.p.require_cross and fn > sn:
                return Signal(Action.BUY, f"SMA{self.p.fast} above SMA{self.p.slow} ({tag})", strength=strength)
            return Signal.hold(f"no entry: {tag}", strength)
        if crossed_down or (not self.p.require_cross and fn < sn):
            return Signal(Action.SELL, f"SMA{self.p.fast} crossed below SMA{self.p.slow} ({tag})", strength=strength)
        return Signal.hold(f"holding: {tag}", strength)
