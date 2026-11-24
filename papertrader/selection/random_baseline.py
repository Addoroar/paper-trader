"""Random selection: the baseline that smart selectors must beat."""

from __future__ import annotations

import hashlib
import random

import pandas as pd

from papertrader.models import TickContext
from papertrader.selection.base import SELECTORS, Selector


def _rng(ctx: TickContext) -> random.Random:
    raw = f"{ctx.seed}|{ctx.account}|{ctx.step}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(raw).digest()[:8], "big"))


@SELECTORS.register("random_baseline")
class RandomBaseline(Selector):
    """Uniform random scores, deterministic for a given (seed, account, step)."""

    def select(self, histories, ctx):  # type: ignore[override]
        self._rng = _rng(ctx)
        return super().select(histories, ctx)

    def score(self, symbol: str, window: pd.DataFrame, history: pd.DataFrame, ctx: TickContext) -> tuple[float, str]:
        s = self._rng.random()
        return s, f"random draw {s:.3f} (seed {ctx.seed})"
