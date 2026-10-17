"""v0.25.0: exits onder fouten en gelijktijdigheid.

| Bevinding | Test |
|-----------|------|
| N1 guard brak af na de eerste mislukte exit | `test_guard_gaat_door_na_mislukte_exit` |
| N1 alarm mag Telegram niet elke minuut vullen | `test_exit_alarm_is_begrensd` |
| N1 live: kopen stil na een mislukte exit, paper niet | `test_live_pauzeert_na_mislukte_exit`, `test_paper_pauzeert_niet` |
| L3 guard en cyclus sloten dezelfde positie dubbel | `test_twee_threads_verkopen_maar_een_keer` |
| L3 "al gesloten" is geen exit-fout | `test_cyclus_ziet_al_gesloten_positie_niet_als_fout` |
"""
import threading
import time

import pytest
from sqlalchemy import select

from tests.test_engine_cycle import FakeFeed, falling, flat, make_cfg, make_cycle
from tradebot.db import SignalRow, TradeRow, session
from tradebot.decision import PositionGone
from tradebot.lists import is_paused, set_paused
from tradebot.locks import MarketBusy, MarketLocks


def _signalen(decision: str) -> list:
    with session() as s:
        return list(s.execute(select(SignalRow).where(SignalRow.decision == decision))
                    .scalars().all())


@pytest.fixture()
def drie_posities(memory_db):
    markten = ["AAA-EUR", "BBB-EUR", "CCC-EUR"]
    feed = FakeFeed({m: flat(100.0) for m in markten}, prices={m: 100.0 for m in markten})
    cycle = make_cycle(make_cfg(markten), feed)
    for m in markten:
        cycle.broker.buy(m, 100.0, stop_loss=95.0, take_profit=120.0, reason="test")
    for m in markten:
        feed.prices[m] = 90.0          # alle drie onder hun stop
    echte_sell = cycle.broker.sell

    def sell(market, reason):
        if market == "AAA-EUR":
            raise RuntimeError("Bitvavo 400: amount has too many decimal places")
        return echte_sell(market, reason)

    cycle.broker.sell = sell
    set_paused(False)
    return cycle


def test_guard_gaat_door_na_mislukte_exit(drie_posities):
    assert drie_posities.check_exits_fast() == 2
    assert [p.market for p in drie_posities.broker.open_positions()] == ["AAA-EUR"]
    assert len(_signalen("failed")) == 1


def test_exit_alarm_is_begrensd(drie_posities):
    drie_posities.check_exits_fast()
    drie_posities.check_exits_fast()
    drie_posities.check_exits_fast()
    assert len(_signalen("failed")) == 1


def test_paper_pauzeert_niet(drie_posities):
    drie_posities.check_exits_fast()
    assert not is_paused()


def test_live_pauzeert_na_mislukte_exit(drie_posities, monkeypatch):
    monkeypatch.setattr(type(drie_posities.broker), "mode", "live")
    drie_posities.check_exits_fast()
    assert is_paused()


@pytest.fixture()
def file_db(tmp_path):
    """Threads krijgen elk een eigen verbinding; :memory: is dan per thread leeg."""
    from tradebot import db
    db._engine = None
    db._Session = None
    db.init_db(f"sqlite:///{tmp_path}/race.db")
    yield
    db._engine = None
    db._Session = None


def test_twee_threads_verkopen_maar_een_keer(file_db):
    feed = FakeFeed({"BTC-EUR": flat(100.0)}, prices={"BTC-EUR": 100.0})
    cycle = make_cycle(make_cfg(["BTC-EUR"]), feed)
    cycle.broker.buy("BTC-EUR", 200.0, 95.0, 120.0, "test")
    echte_prijs = feed.get_price

    def trage_prijs(market):          # verbreedt het race-venster
        time.sleep(0.05)
        return echte_prijs(market)

    feed.get_price = trage_prijs
    uitkomst: list = []

    def verkoop():
        try:
            cycle.broker.sell("BTC-EUR", "race")
            uitkomst.append("verkocht")
        except PositionGone:
            uitkomst.append("al weg")

    threads = [threading.Thread(target=verkoop) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(uitkomst) == ["al weg", "verkocht"]
    with session() as s:
        sells = s.execute(select(TradeRow).where(TradeRow.side == "sell")).scalars().all()
    assert len(sells) == 1


def test_cyclus_ziet_al_gesloten_positie_niet_als_fout(memory_db):
    feed = FakeFeed({"BTC-EUR": falling()}, prices={"BTC-EUR": 50.0})
    cycle = make_cycle(make_cfg(["BTC-EUR"]), feed)
    cycle.broker.buy("BTC-EUR", 200.0, stop_loss=95.0, take_profit=200.0, reason="test")

    def al_weg(market, reason):
        raise PositionGone(market)

    cycle.broker.sell = al_weg
    cycle.run_once()
    assert _signalen("failed") == []


def test_slot_geeft_market_busy_na_timeout():
    locks = MarketLocks()
    vast = threading.Event()
    los = threading.Event()

    def houd_vast():
        with locks.hold("X-EUR"):
            vast.set()
            los.wait(2)

    t = threading.Thread(target=houd_vast)
    t.start()
    vast.wait(2)
    with pytest.raises(MarketBusy), locks.hold("X-EUR", timeout=0.05):
        pass
    los.set()
    t.join()
    with locks.hold("X-EUR", timeout=0.05):   # weer vrij
        pass
