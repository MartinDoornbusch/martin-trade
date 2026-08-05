"""Tests voor de position guard: de echte `TradingCycle.check_exits_fast`.

Tot v0.19.0 stond hier een `GuardHarness` die de guard-logica in het testbestand
nabootste. Dat testte een kopie, niet de code die op de Pi draait: de guard zelf
had 0% coverage. Nu draait de echte methode tegen een fake feed en een echte
PaperBroker op een in-memory DB.
"""
import pytest

from tests.test_engine_cycle import FakeFeed, flat, make_cfg, make_cycle
from tradebot.db import SignalRow, session


@pytest.fixture()
def cycle(memory_db):
    """Cyclus met één markt op een vlakke koers van 100; prijs per test te zetten."""
    feed = FakeFeed({"BTC-EUR": flat(100.0)}, prices={"BTC-EUR": 100.0})
    return make_cycle(make_cfg(["BTC-EUR"]), feed)


def signals() -> list[SignalRow]:
    with session() as s:
        return list(s.execute(SignalRow.__table__.select()).all())


def test_guard_closes_on_stop_loss(cycle):
    cycle.broker.buy("BTC-EUR", 200.0, stop_loss=95.0, take_profit=120.0, reason="test")
    cycle.feed.prices["BTC-EUR"] = 94.0

    assert cycle.check_exits_fast() == 1
    assert cycle.broker.open_positions() == []
    assert cycle.broker.daily_pnl_eur() < 0  # verlies gerealiseerd, maar begrensd


def test_guard_closes_on_take_profit(cycle):
    cycle.broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")
    cycle.feed.prices["BTC-EUR"] = 121.0

    assert cycle.check_exits_fast() == 1
    assert cycle.broker.daily_pnl_eur() > 0


def test_guard_leaves_position_in_range(cycle):
    cycle.broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")
    cycle.feed.prices["BTC-EUR"] = 105.0

    assert cycle.check_exits_fast() == 0
    assert len(cycle.broker.open_positions()) == 1


def test_guard_logs_the_exit(cycle):
    """De guard moet zijn exit vastleggen, anders mist de historie een sell."""
    cycle.broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")
    cycle.feed.prices["BTC-EUR"] = 94.0
    cycle.check_exits_fast()

    rows = signals()
    assert len(rows) == 1
    assert rows[0].action == "sell" and rows[0].decision == "executed"
    assert "guard" in rows[0].reason


def test_guard_survives_a_price_error(cycle):
    """Een prijsfout in één markt mag de guard niet stoppen: de andere positie
    moet nog steeds gesloten worden."""
    cycle.feed.series["ETH-EUR"] = flat(100.0)
    cycle.broker.buy("ETH-EUR", 200.0, 95.0, 120.0, "test")
    cycle.broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")

    def prijs(market: str) -> float:
        if market == "ETH-EUR":
            raise RuntimeError("feed down")
        return 94.0

    cycle.feed.get_price = prijs

    assert cycle.check_exits_fast() == 1
    assert [p.market for p in cycle.broker.open_positions()] == ["ETH-EUR"]


def test_guard_is_not_blocked_by_the_kill_switch(cycle):
    """Kill-switch pauzeert kopen, nooit risicobeheersing."""
    from tradebot.lists import set_paused

    cycle.broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")
    set_paused(True)
    cycle.feed.prices["BTC-EUR"] = 94.0

    assert cycle.check_exits_fast() == 1
    assert cycle.broker.open_positions() == []


def test_guard_ignores_live_positions(cycle):
    """Mode-scheiding (review 1.4): de paper-guard raakt geen live-positie aan."""
    from tradebot.db import PositionRow

    # Live-positie die ruim onder haar stop loss staat: zonder mode-filter zou de
    # paper-guard hem sluiten.
    cycle.feed.series["ETH-EUR"] = flat(100.0)
    cycle.feed.prices["ETH-EUR"] = 50.0
    with session() as s:
        s.add(PositionRow(market="ETH-EUR", mode="live", amount=1.0, entry_price=100.0,
                          stop_loss=95.0, take_profit=120.0))
        s.commit()

    assert cycle.check_exits_fast() == 0
    with session() as s:
        rows = s.execute(PositionRow.__table__.select()).all()
    assert len(rows) == 1 and rows[0].mode == "live"
