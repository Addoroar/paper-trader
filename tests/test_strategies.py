import numpy as np
import pytest

from papertrader.models import Action, Position
from papertrader.strategies import STRATEGIES
from papertrader.strategies.indicators import rsi, sma

from conftest import make_frame

POS = Position("a", "X", 1, 100, 0)


def test_all_required_strategies_registered():
    for name in ["sma_crossover", "rsi_mean_reversion", "bollinger_bands", "momentum", "buy_and_hold"]:
        assert name in STRATEGIES


def test_indicators():
    x = np.arange(1, 11, dtype=float)
    assert sma(x, 3)[-1] == pytest.approx(9)
    assert np.isnan(sma(x, 3)[1])
    assert rsi(x, 5)[-1] == 100.0
    assert rsi(x[::-1], 5)[-1] == pytest.approx(0.0)


def test_sma_crossover_buy_then_sell():
    s = STRATEGIES.get("sma_crossover")({"fast": 3, "slow": 6})
    down = list(np.linspace(110, 100, 10))
    up = down + [104, 108]
    sig = s.on_bar(make_frame(up), None)
    assert sig.action == Action.BUY and "crossed above" in sig.reason
    later = up + [112, 116, 118]
    assert s.on_bar(make_frame(later), POS).action == Action.HOLD
    crash = later + [100, 90]
    assert s.on_bar(make_frame(crash), POS).action == Action.SELL
    assert s.on_bar(make_frame(down[:4]), None).action == Action.HOLD  # warm-up


def test_rsi_mean_reversion():
    s = STRATEGIES.get("rsi_mean_reversion")({"period": 5, "oversold": 30, "overbought": 70})
    falling = list(np.linspace(100, 80, 15))
    assert s.on_bar(make_frame(falling), None).action == Action.BUY
    rising = list(np.linspace(80, 100, 15))
    assert s.on_bar(make_frame(rising), POS).action == Action.SELL
    assert s.on_bar(make_frame(rising), None).action == Action.HOLD


def test_bollinger():
    s = STRATEGIES.get("bollinger_bands")({"period": 10, "num_std": 2})
    flat = [100 + (i % 2) * 0.5 for i in range(20)]
    assert s.on_bar(make_frame(flat + [95]), None).action == Action.BUY
    assert s.on_bar(make_frame(flat + [101]), POS).action == Action.SELL


def test_momentum():
    s = STRATEGIES.get("momentum")({"lookback": 5, "entry_pct": 2, "exit_pct": 0})
    assert s.on_bar(make_frame([100, 100, 100, 100, 100, 101, 105]), None).action == Action.BUY
    assert s.on_bar(make_frame([105, 105, 105, 105, 105, 104, 100]), POS).action == Action.SELL


def test_buy_and_hold():
    s = STRATEGIES.get("buy_and_hold")({})
    assert s.on_bar(make_frame([1]), None).action == Action.BUY
    assert s.on_bar(make_frame([1, 2]), POS).action == Action.HOLD
