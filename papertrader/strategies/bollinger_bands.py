"""Bollinger band reversion: buy below the lower band, exit at the middle/upper band."""

from __future__ import annotations

from typing import Literal

import pandas as pd
from pydantic import Field

from papertrader.models import Action, Position, Signal
from papertrader.strategies.base import STRATEGIES, Strategy, StrategyParams
from papertrader.strategies.indicators import rolling_std, sma


class BollingerParams(StrategyParams):
    period: int = Field(20, ge=2)
    num_std: float = Field(2.0, gt=0)
    exit_at: Literal["middle", "upper"] = "middle"


@STRATEGIES.register("bollinger_bands")
class BollingerBands(Strategy):
    """Mean reversion on Bollinger bands."""

    Params = BollingerParams

    @property
    def warmup_bars(self) -> int:
        return self.p.period

    def on_bar(self, history: pd.DataFrame, position: Position | None) -> Signal:
        c = history["close"].to_numpy()
        if len(c) < self.warmup_bars:
            return Signal.hold(f"warming up ({len(c)}/{self.warmup_bars} bars)")
        mid = float(sma(c, self.p.period)[-1])
        sd = float(rolling_std(c, self.p.period)[-1])
        lo, hi = mid - self.p.num_std * sd, mid + self.p.num_std * sd
        px = float(c[-1])
        pos_in_band = 0.0 if sd == 0 else (px - mid) / (self.p.num_std * sd)
        strength = max(-1.0, min(1.0, -pos_in_band))
        tag = f"close {px:.2f}, bands [{lo:.2f}, {mid:.2f}, {hi:.2f}]"
        if position is None and px < lo:
            return Signal(Action.BUY, f"close below lower band ({tag})", strength=strength)
        target = mid if self.p.exit_at == "middle" else hi
        if position is not None and px >= target:
            return Signal(Action.SELL, f"close reached {self.p.exit_at} band ({tag})", strength=strength)
        return Signal.hold(tag, strength)
