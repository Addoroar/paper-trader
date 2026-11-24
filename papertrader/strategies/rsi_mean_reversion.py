"""RSI mean reversion: buy oversold, sell overbought."""

from __future__ import annotations

import pandas as pd
from pydantic import Field, model_validator

from papertrader.models import Action, Position, Signal
from papertrader.strategies.base import STRATEGIES, Strategy, StrategyParams
from papertrader.strategies.indicators import rsi


class RsiParams(StrategyParams):
    period: int = Field(14, ge=2)
    oversold: float = Field(30, gt=0, lt=100)
    overbought: float = Field(70, gt=0, lt=100)

    @model_validator(mode="after")
    def _order(self) -> "RsiParams":
        if self.oversold >= self.overbought:
            raise ValueError("oversold must be below overbought")
        return self


@STRATEGIES.register("rsi_mean_reversion")
class RsiMeanReversion(Strategy):
    """BUY when RSI < oversold, SELL when RSI > overbought."""

    Params = RsiParams

    @property
    def warmup_bars(self) -> int:
        return self.p.period + 2

    def on_bar(self, history: pd.DataFrame, position: Position | None) -> Signal:
        c = history["close"].to_numpy()
        if len(c) < self.warmup_bars:
            return Signal.hold(f"warming up ({len(c)}/{self.warmup_bars} bars)")
        r = float(rsi(c, self.p.period)[-1])
        strength = max(-1.0, min(1.0, (50 - r) / 50))
        tag = f"RSI{self.p.period}={r:.1f}"
        if position is None and r < self.p.oversold:
            return Signal(Action.BUY, f"{tag} below oversold {self.p.oversold:g}", strength=strength)
        if position is not None and r > self.p.overbought:
            return Signal(Action.SELL, f"{tag} above overbought {self.p.overbought:g}", strength=strength)
        return Signal.hold(f"{tag} within [{self.p.oversold:g}, {self.p.overbought:g}]", strength)
