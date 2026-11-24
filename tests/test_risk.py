import pytest

from papertrader.broker.paper import PaperBroker
from papertrader.config import RiskConfig
from papertrader.models import Action, Position, Signal
from papertrader.risk import RiskManager


@pytest.fixture
def rm(db, events):
    return RiskManager(RiskConfig(max_position_pct=20, stop_loss_pct=5, take_profit_pct=10, max_open_positions=2,
                                  max_daily_loss_pct=3), PaperBroker(db, events, 0.1, 0.05))


POS = Position("a", "X", 10, 100.0, 0)


def test_stop_loss_on_low(rm):
    ex = rm.check_exit(POS, 99, 101, 94)
    assert ex.kind == "stop_loss" and ex.price == pytest.approx(95)


def test_stop_loss_gap_fills_at_open(rm):
    ex = rm.check_exit(POS, 90, 92, 89)
    assert ex.kind == "stop_loss" and ex.price == 90 and "gapped" in ex.reason


def test_take_profit(rm):
    ex = rm.check_exit(POS, 105, 111, 104)
    assert ex.kind == "take_profit" and ex.price == pytest.approx(110)


def test_both_touched_assumes_stop_first(rm):
    assert rm.check_exit(POS, 100, 115, 90).kind == "stop_loss"


def test_no_exit_inside_band(rm):
    assert rm.check_exit(POS, 100, 104, 96) is None


def test_disabled_stops(db, events):
    rm = RiskManager(RiskConfig(stop_loss_pct=0, take_profit_pct=0), PaperBroker(db, events, 0, 0))
    assert rm.check_exit(POS, 50, 500, 1) is None


def test_sizing_caps_to_max_position_pct(rm):
    d = rm.size_buy("X", Signal(Action.BUY, "r"), 100, equity=10_000, cash=10_000, position=None, open_positions=0)
    assert d.qty == 19  # 20% of 10k = 2000 / (100.05 * 1.001)
    assert "20% of equity" in d.reason


def test_sizing_respects_signal_size_and_cash(rm):
    d = rm.size_buy("X", Signal(Action.BUY, "r", size_pct=0.05), 100, 10_000, 10_000, None, 0)
    assert d.qty == 4
    d = rm.size_buy("X", Signal(Action.BUY, "r"), 100, 10_000, 500, None, 0)
    assert d.qty == 4 and "capped by cash" in d.reason
    d = rm.size_buy("X", Signal(Action.BUY, "r"), 100, 10_000, 50, None, 0)
    assert d.qty == 0 and "insufficient cash" in d.reason


def test_max_open_positions(rm):
    d = rm.size_buy("X", Signal(Action.BUY, "r"), 100, 10_000, 10_000, None, open_positions=2)
    assert d.qty == 0 and "max_open_positions" in d.reason


def test_already_at_target(rm):
    pos = Position("a", "X", 20, 100, 0)
    d = rm.size_buy("X", Signal(Action.BUY, "r"), 100, 10_000, 8000, pos, 1)
    assert d.qty == 0 and "already at target" in d.reason


def test_daily_loss_cap(rm):
    assert not rm.daily_loss_breached(10_000, 9_800)
    assert rm.daily_loss_breached(10_000, 9_700)
