"""Vectorised technical indicators (numpy). Each uses only past/current values."""

from __future__ import annotations

import numpy as np


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Simple moving average; NaN for the first ``n-1`` values."""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
        out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    """Rolling population standard deviation."""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.lib.stride_tricks.sliding_window_view(x.astype(float), n)
        out[n - 1 :] = w.std(axis=1)
    return out


def rsi(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RSI."""
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    d = np.diff(x.astype(float))
    gains, losses = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = gains[:n].mean(), losses[:n].mean()

    def val(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[n] = val(ag, al)
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = val(ag, al)
    return out


def pct_change(x: np.ndarray, n: int) -> float:
    """Return over the last ``n`` bars (``x[-1] / x[-1-n] - 1``); NaN if too short."""
    if len(x) <= n or x[-1 - n] == 0:
        return float("nan")
    return float(x[-1] / x[-1 - n] - 1.0)
