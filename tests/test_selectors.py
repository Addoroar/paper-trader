import numpy as np

from papertrader.models import TickContext
from papertrader.selection import SELECTORS
from papertrader.strategies import STRATEGIES

from conftest import make_frame


def universe():
    n = 60
    return {
        "UP": make_frame(np.linspace(100, 130, n)),
        "FLAT": make_frame(np.full(n, 100.0) + np.sin(np.arange(n))),
        "DOWN": make_frame(np.linspace(100, 80, n)),
        "TINY": make_frame(np.linspace(100, 200, n), volume=1),  # illiquid
        "SHORT": make_frame(np.linspace(100, 110, 10)),
    }


CTX = TickContext(account="a", step=0, now=0, seed=42)


def test_all_selectors_registered():
    for name in ["momentum_rank", "signal_strength", "volatility_adjusted", "random_baseline"]:
        assert name in SELECTORS


def test_momentum_rank_and_filters():
    sel = SELECTORS.get("momentum_rank")(lookback_bars=50, top_n=2, min_avg_volume=100_000)
    res = sel.select(universe(), CTX)
    assert res.selected == ["UP", "FLAT"]
    assert [s.symbol for s in res.ranked] == ["UP", "FLAT", "DOWN"]
    assert "illiquid" in res.filtered["TINY"]
    assert "insufficient history" in res.filtered["SHORT"]
    assert "rank #1" in res.reason_for("UP") and "%" in res.reason_for("UP")


def test_volatility_adjusted_prefers_smooth_uptrend():
    n = 60
    rng = np.random.default_rng(0)
    data = {
        "SMOOTH": make_frame(np.linspace(100, 110, n)),
        "NOISY": make_frame(np.linspace(100, 112, n) * (1 + rng.normal(0, 0.03, n))),
    }
    res = SELECTORS.get("volatility_adjusted")(50, 1, 0).select(data, CTX)
    assert res.selected == ["SMOOTH"]


def test_signal_strength_uses_strategy():
    strat = STRATEGIES.get("momentum")({"lookback": 10})
    res = SELECTORS.get("signal_strength")(50, 1, 100_000, strat).select(universe(), CTX)
    assert res.selected == ["UP"]


def test_random_baseline_seeded():
    sel = SELECTORS.get("random_baseline")(50, 2, 100_000)
    a = sel.select(universe(), CTX).selected
    b = sel.select(universe(), CTX).selected
    assert a == b
    picks = {tuple(sel.select(universe(), TickContext("a", step, 0, 42)).selected) for step in range(20)}
    assert len(picks) > 1  # varies across rebalances
    other_seed = [tuple(sel.select(universe(), TickContext("a", s, 0, 7)).selected) for s in range(20)]
    same_seed = [tuple(sel.select(universe(), TickContext("a", s, 0, 42)).selected) for s in range(20)]
    assert other_seed != same_seed
