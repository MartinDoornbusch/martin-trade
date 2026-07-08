import pytest

from tradebot.decision import FeeModel
from tradebot.exchange import OrderResult
from tradebot.live import LiveBroker

FEES = FeeModel(0.15, 0.25, 0.10)


class FakeLiveClient:
    """Nabootsing van BitvavoClient voor live-order flows."""

    def __init__(self, fill_after_polls=0, never_fill=False, eur_balance=500.0):
        self.fill_after = fill_after_polls
        self.never_fill = never_fill
        self.eur = eur_balance
        self.polls = 0
        self.cancelled = False
        self.price = 100.0

    def get_book_ticker(self, market):
        return {"bid": "100.0", "ask": "100.1"}

    def place_limit_order(self, market, side, amount, price, post_only=True):
        self.placed = {"amount": float(amount), "price": float(price)}
        return {"orderId": "o1", "status": "new", "filledAmount": "0"}

    def get_order(self, market, order_id):
        self.polls += 1
        if self.never_fill or (self.cancelled and self.polls > 0 and self.never_fill):
            return {"orderId": order_id, "status": "canceled" if self.cancelled else "new",
                    "filledAmount": "0", "filledAmountQuote": "0", "feePaid": "0"}
        if self.polls > self.fill_after:
            amt = self.placed["amount"]
            return {"orderId": order_id, "status": "filled",
                    "filledAmount": str(amt), "filledAmountQuote": str(amt * 100.0),
                    "feePaid": str(amt * 100.0 * 0.0015)}
        return {"orderId": order_id, "status": "new", "filledAmount": "0",
                "filledAmountQuote": "0", "feePaid": "0"}

    def cancel_order(self, market, order_id):
        self.cancelled = True
        return {}

    def get_balances(self):
        return {"EUR": self.eur}

    def get_price(self, market):
        return self.price

    def place_market_order(self, market, side, amount_quote):
        fee = amount_quote * self.price * 0.0025
        return OrderResult("o2", market, side, amount_quote, self.price, fee)


def make_broker(client, cap=100.0):
    return LiveBroker(client, FEES, cap, entry_timeout_s=0.2, poll_interval_s=0.01)


def test_live_buy_maker_fill_creates_position(memory_db):
    client = FakeLiveClient(fill_after_polls=1)
    broker = make_broker(client)
    broker.buy("BTC-EUR", 50.0, stop_loss=90.0, take_profit=120.0, reason="test")
    positions = broker.open_positions()
    assert len(positions) == 1
    assert positions[0].entry_price == pytest.approx(100.0)
    assert broker.exposure_eur() > 49.0


def test_live_buy_timeout_cancels_and_raises(memory_db):
    client = FakeLiveClient(never_fill=True)
    broker = make_broker(client)
    with pytest.raises(TimeoutError):
        broker.buy("BTC-EUR", 50.0, 90.0, 120.0, "test")
    assert client.cancelled
    assert broker.open_positions() == []


def test_live_exposure_cap_blocks_buy(memory_db):
    client = FakeLiveClient(fill_after_polls=0)
    broker = make_broker(client, cap=60.0)
    broker.buy("BTC-EUR", 50.0, 90.0, 120.0, "test")
    with pytest.raises(ValueError, match="exposure-plafond"):
        broker.buy("ETH-EUR", 50.0, 90.0, 120.0, "test")


def test_live_cash_respects_cap(memory_db):
    broker = make_broker(FakeLiveClient(eur_balance=5000.0), cap=100.0)
    assert broker.cash_eur() == 100.0  # rekening heeft 5000, plafond wint


def test_live_sell_realizes_pnl(memory_db):
    client = FakeLiveClient(fill_after_polls=0)
    broker = make_broker(client)
    broker.buy("BTC-EUR", 50.0, 90.0, 120.0, "test")
    client.price = 110.0
    broker.sell("BTC-EUR", "take profit")
    assert broker.open_positions() == []
    assert broker.daily_pnl_eur() > 0
