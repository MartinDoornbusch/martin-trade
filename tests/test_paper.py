import pytest

from tradebot.decision import FeeModel
from tradebot.exchange import Candle, ExchangeAdapter, OrderResult
from tradebot.paper import PaperBroker


class FakeFeed(ExchangeAdapter):
    def __init__(self, price=100.0):
        self.price = price

    def get_candles(self, market, interval, limit) -> list[Candle]:
        return []

    def get_price(self, market) -> float:
        return self.price

    def get_balances(self):
        return {}

    def place_market_order(self, market, side, amount_quote) -> OrderResult:
        raise NotImplementedError

    def get_fees_pct(self):
        return 0.15, 0.25


FEES = FeeModel(0.15, 0.25, 0.10)


@pytest.fixture()
def broker(memory_db):
    return PaperBroker(FakeFeed(), FEES, start_eur=1000.0)


def test_buy_deducts_cash_and_fee(broker):
    broker.buy("BTC-EUR", 200.0, stop_loss=90, take_profit=120, reason="test")
    assert broker.cash_eur() == 800.0
    pos = broker.open_positions()[0]
    # fee = 0.25% of 200 = 0.50 EUR; amount = 199.50/100
    assert abs(pos.amount - 1.995) < 1e-9
    assert abs(pos.fees_paid_eur - 0.5) < 1e-9


def test_sell_realizes_pnl_net_of_both_fees(broker):
    broker.buy("BTC-EUR", 200.0, 90, 120, "test")
    broker.feed.price = 110.0  # +10%
    broker.sell("BTC-EUR", "take profit")
    assert broker.open_positions() == []
    # gross = 1.995*110 = 219.45; sell fee = 0.5486; net = 218.90
    assert abs(broker.cash_eur() - (800 + 219.45 - 219.45 * 0.0025)) < 1e-6
    # pnl = net - (cost basis 199.5 + buy fee 0.5) = 18.90
    assert broker.daily_pnl_eur() == pytest.approx(219.45 * 0.9975 - 200.0, abs=1e-6)


def test_sell_without_position_raises(broker):
    with pytest.raises(ValueError):
        broker.sell("ETH-EUR", "no pos")


def test_buy_insufficient_cash_raises(broker):
    with pytest.raises(ValueError):
        broker.buy("BTC-EUR", 2000.0, 90, 120, "too big")


def _add_live_trade(**kw):
    """Schrijf een live-trade rechtstreeks in de DB (LiveBroker zelf blijft vergrendeld)."""
    from tradebot.db import TradeRow, session
    with session() as s:
        s.add(TradeRow(mode="live", **kw))
        s.commit()


def test_daily_pnl_ignores_live_trades(broker):
    """Regressie op bug 1.4: een live-verlies mag de paper-dagwinst niet vervuilen."""
    _add_live_trade(market="BTC-EUR", side="sell", amount=1.0, price=100.0,
                    fee_eur=0.25, pnl_eur=-500.0)
    assert broker.daily_pnl_eur() == 0.0


def test_last_trade_at_ignores_live_trades(broker):
    """Regressie op bug 1.4: een live-trade mag geen paper-cooldown zetten."""
    _add_live_trade(market="BTC-EUR", side="buy", amount=1.0, price=100.0, fee_eur=0.25)
    assert broker.last_trade_at("BTC-EUR") is None
    broker.buy("BTC-EUR", 200.0, 90, 120, "paper")
    assert broker.last_trade_at("BTC-EUR") is not None


def test_open_positions_ignores_live_positions(broker):
    from tradebot.db import PositionRow, session
    with session() as s:
        s.add(PositionRow(market="ETH-EUR", mode="live", amount=1.0, entry_price=100.0,
                          stop_loss=90.0, take_profit=120.0))
        s.commit()
    assert broker.open_positions() == []


def test_flat_price_round_trip_loses_exactly_fees(broker):
    """Core fee-awareness check: flat market round trip must cost ~2x taker fee."""
    broker.buy("BTC-EUR", 400.0, 90, 120, "test")
    broker.sell("BTC-EUR", "flat exit")
    loss = 1000.0 - broker.cash_eur()
    expected_loss = 400 * 0.0025 + (400 - 1.0) * 0.0025  # buy fee + sell fee on remainder
    assert loss == pytest.approx(expected_loss, rel=0.01)
