"""Selector base class: score the universe, filter illiquid symbols, keep the top N."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pandas as pd

from papertrader.models import TickContext
from papertrader.registry import Registry
from papertrader.strategies.base import Strategy


@dataclass
class ScoredSymbol:
    """One symbol's score and its explanation."""

    symbol: str
    score: float
    detail: str
    price: float
    rank: int | None = None
    selected: bool = False


@dataclass
class SelectionResult:
    """Ranked symbols (best first), the chosen ones, and why others were dropped."""

    method: str
    ranked: list[ScoredSymbol]
    selected: list[str]
    filtered: dict[str, str] = field(default_factory=dict)

    def reason_for(self, symbol: str) -> str:
        """Human-readable reason a symbol was or wasn't selected."""
        for s in self.ranked:
            if s.symbol == symbol:
                verdict = "selected" if s.selected else "not selected"
                return f"{verdict}: {self.method} rank #{s.rank}, {s.detail}"
        return f"filtered: {self.filtered.get(symbol, 'no data')}"


class Selector:
    """Base class for selectors.

    Args:
        lookback_bars: bars of history used for scoring.
        top_n: how many symbols to select.
        min_avg_volume: minimum average dollar volume per bar over the lookback.
        strategy: the account's strategy (used by ``signal_strength``).
    """

    name: ClassVar[str] = "base"

    def __init__(self, lookback_bars: int, top_n: int, min_avg_volume: float, strategy: Strategy | None = None):
        self.lookback = lookback_bars
        self.top_n = top_n
        self.min_avg_volume = min_avg_volume
        self.strategy = strategy

    def score(self, symbol: str, window: pd.DataFrame, history: pd.DataFrame, ctx: TickContext) -> tuple[float, str]:
        """Return ``(score, detail)`` for one symbol; higher is better.

        Args:
            window: the last ``lookback_bars + 1`` closed bars.
            history: all closed bars available (for selectors needing more context).
        """
        raise NotImplementedError

    def select(self, histories: dict[str, pd.DataFrame], ctx: TickContext) -> SelectionResult:
        """Filter, score and rank ``histories`` (each holds only closed bars up to ``ctx.now``)."""
        filtered: dict[str, str] = {}
        scored: list[ScoredSymbol] = []
        for sym in sorted(histories):
            h = histories[sym]
            if len(h) < self.lookback + 1:
                filtered[sym] = f"insufficient history ({len(h)}/{self.lookback + 1} bars)"
                continue
            w = h.iloc[-(self.lookback + 1) :]
            dollar_vol = float(np.mean(w["volume"].to_numpy() * w["close"].to_numpy()))
            if dollar_vol < self.min_avg_volume:
                filtered[sym] = f"illiquid: avg dollar volume {dollar_vol:,.0f} < {self.min_avg_volume:,.0f}"
                continue
            s, detail = self.score(sym, w, h, ctx)
            if s is None or not math.isfinite(s):
                filtered[sym] = f"no score ({detail})"
                continue
            scored.append(ScoredSymbol(sym, float(s), detail, float(w["close"].iloc[-1])))
        scored.sort(key=lambda x: (-x.score, x.symbol))
        for i, s in enumerate(scored, start=1):
            s.rank = i
            s.selected = i <= self.top_n
        return SelectionResult(self.name, scored, [s.symbol for s in scored if s.selected], filtered)


SELECTORS: Registry[Selector] = Registry("selector")
