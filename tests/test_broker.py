import pytest

from papertrader.broker.paper import PaperBroker, make_order_id, round_qty
from papertrader.models import OrderStatus, Side
from papertrader.storage import repo

from conftest import T0


@pytest.fixture
def broker(db, events):
    with db.tx():
        repo.ensure_account(db, "a", "buy_and_hold", {}, 10_000)
    return PaperBroker(db, events, fees_pct=0.1, slippage_pct=0.05)


def test_buy_applies_slippage_and_fees(broker, db):
    order, fill = broker.execute("a", "AAPL", Side.BUY, 10, 100.0, T0, kind="entry", reason="t", now=T0)
    assert order.status == OrderStatus.FILLED
    assert fill.price == pytest.approx(100.05)
    assert fill.fee == pytest.approx(10 * 100.05 * 0.001)
    assert repo.get_cash(db, "a") == pytest.approx(10_000 - 1000.5 - 1.0005)
    pos = repo.get_position(db, "a", "AAPL")
    assert pos.qty == 10
    assert pos.avg_cost == pytest.approx((1000.5 + 1.0005) / 10)  # fees included in cost basis


def test_sell_realized_pnl_and_close(broker, db):
    broker.execute("a", "AAPL", Side.BUY, 10, 100.0, T0, kind="entry", reason="t", now=T0)
    avg = repo.get_position(db, "a", "AAPL").avg_cost
    _, fill = broker.execute("a", "AAPL", Side.SELL, 10, 110.0, T0 + 3600, kind="signal_exit", reason="t", now=T0)
    px = 110 * (1 - 0.0005)
    assert fill.price == pytest.approx(px)
    assert fill.realized_pnl == pytest.approx(10 * px * 0.999 - avg * 10)
    assert repo.get_position(db, "a", "AAPL") is None
    # cash reconciles exactly to the allocation plus realized P&L
    assert repo.get_cash(db, "a") == pytest.approx(10_000 + fill.realized_pnl)


def test_average_cost_on_add(broker, db):
    broker.execute("a", "X", Side.BUY, 10, 100.0, T0, kind="entry", reason="t", now=T0)
    broker.execute("a", "X", Side.BUY, 10, 200.0, T0 + 3600, kind="entry", reason="t", now=T0)
    pos = repo.get_position(db, "a", "X")
    assert pos.qty == 20
    expected = (10 * 100.05 * 1.001 + 10 * 200.1 * 1.001) / 20
    assert pos.avg_cost == pytest.approx(expected)


def test_insufficient_funds_rejected(broker, db):
    order, fill = broker.execute("a", "AAPL", Side.BUY, 100, 100.0, T0, kind="entry", reason="t", now=T0)
    assert fill is None
    assert order.status == OrderStatus.REJECTED
    assert "insufficient funds" in order.reject_reason
    assert repo.get_cash(db, "a") == 10_000
    assert db.scalar("SELECT count(*) FROM events WHERE type='order_rejected'") == 1


def test_no_shorting(broker):
    order, fill = broker.execute("a", "AAPL", Side.SELL, 1, 100.0, T0, kind="signal_exit", reason="t", now=T0)
    assert fill is None and order.status == OrderStatus.REJECTED
    assert "long-only" in order.reject_reason


def test_order_ids_deterministic_and_idempotent(broker, db):
    oid = make_order_id("a", "AAPL", T0, Side.BUY)
    assert oid == make_order_id("a", "AAPL", T0, "BUY")
    assert oid != make_order_id("a", "AAPL", T0, Side.SELL)
    o1, created1 = broker.place_order("a", "AAPL", Side.BUY, 5, 100.0, T0, kind="entry", reason="t", now=T0)
    o2, created2 = broker.place_order("a", "AAPL", Side.BUY, 7, 101.0, T0, kind="entry", reason="t", now=T0)
    assert created1 and not created2 and o2.qty == 5
    f1 = broker.fill_order(oid, T0)
    f2 = broker.fill_order(oid, T0)
    assert f1.price == f2.price
    assert db.scalar("SELECT count(*) FROM fills") == 1
    assert db.scalar("SELECT count(*) FROM orders") == 1


def test_round_qty():
    assert round_qty("AAPL", 12.9999) == 12
    assert round_qty("BTC/USDT", 0.12345678) == 0.123456


def test_max_affordable(broker):
    q = broker.max_affordable_qty("AAPL", 1000, 100)
    assert q == 9  # 10 would cost 1001.5
