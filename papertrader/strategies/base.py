"""Strategy base class.

A strategy looks at the closed-bar history of one symbol (never the future)
and the account's current position in that symbol, and returns a
:class:`~papertrader.models.Signal`. Strategies don't size orders in shares or
check cash: they may suggest a fraction of equity via ``Signal.size_pct`` and
the risk layer turns that into an order.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict

from papertrader.models import Position, Signal
from papertrader.registry import Registry


class StrategyParams(BaseModel):
    """Base class for strategy parameter models (unknown keys are rejected)."""

    model_config = ConfigDict(extra="forbid")


class Strategy:
    """Base class for all strategies.

    Subclasses set ``Params`` (a :class:`StrategyParams` model) and implement
    :meth:`on_bar`.
    """

    name: ClassVar[str] = "base"
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.p = self.Params(**(params or {}))

    @classmethod
    def validate_params(cls, params: dict[str, Any]) -> StrategyParams:
        """Validate a params dict (raises pydantic ``ValidationError``)."""
        return cls.Params(**params)

    @property
    def warmup_bars(self) -> int:
        """Minimum number of bars needed before the strategy can act."""
        return 2

    def on_bar(self, history: pd.DataFrame, position: Position | None) -> Signal:
        """Decide what to do on the latest bar of ``history``.

        Args:
            history: closed bars up to and including the current one, oldest
                first, columns ``open, high, low, close, volume``.
            position: the account's open position in this symbol, or None.
        """
        raise NotImplementedError

    def strength(self, history: pd.DataFrame) -> float:
        """How attractive a new entry looks, in [-1, 1] (used by ``signal_strength``)."""
        if len(history) < self.warmup_bars:
            return 0.0
        return float(self.on_bar(history, None).strength)


STRATEGIES: Registry[Strategy] = Registry("strategy")
