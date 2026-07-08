import pytest

from tests.test_paper import FakeFeed
from tradebot.decision import FeeModel
from tradebot.paper import PaperBroker

FEES = FeeModel(0.15, 0.25, 0.10)


class GuardHarness:
    """Minimale nabootsing van TradingCycle.check_exits_fast op de echte broker."""

    def __init__(self, broker):
        self.broker = broker

    def run(self):
        closed = 0
        for pos in self.broker.open_positions():
            price = self.broker.feed.get_price(pos.market)
            if price <= pos.stop_loss or price >= pos.take_profit:
                self.broker.sell(pos.market, "guard test")
                closed += 1
        return closed


@pytest.fixture()
def broker(memory_db):
    return PaperBroker(FakeFeed(price=100.0), FEES, start_eur=1000.0)


def test_guard_closes_on_stop_loss(broker):
    broker.buy("BTC-EUR", 200.0, stop_loss=95.0, take_profit=120.0, reason="test")
    broker.feed.price = 94.0
    assert GuardHarness(broker).run() == 1
    assert broker.open_positions() == []
    assert broker.daily_pnl_eur() < 0  # verlies gerealiseerd, maar begrensd


def test_guard_closes_on_take_profit(broker):
    broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")
    broker.feed.price = 121.0
    assert GuardHarness(broker).run() == 1
    assert broker.daily_pnl_eur() > 0


def test_guard_leaves_position_in_range(broker):
    broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")
    broker.feed.price = 105.0
    assert GuardHarness(broker).run() == 0
    assert len(broker.open_positions()) == 1
