"""N5 (v0.25.0): datetimes uit SQLite zijn naïef en `.timestamp()` rekent in de
lokale tijd van het proces. HA Supervisor zet `TZ` in de add-on, dus onder
Europe/Amsterdam schoof `opened_at` twee uur naar voren. Deze tests draaien
bewust onder die tijdzone; vóór de fix vielen ze om."""
import time
from datetime import datetime, timezone

import pytest

from tradebot.db import PositionRow, session
from tradebot.decision import FeeModel
from tradebot.exchange import Candle
from tradebot.paper import PaperBroker
from tradebot.strategy import breakeven_stop_hit, time_stop_hit

H = 4 * 3600 * 1000
ENTRY = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)   # midden in de 08:00-bar
BASE = int(datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture()
def amsterdam(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    time.tzset()
    if time.tzname[0] == "UTC":
        pytest.skip("geen zoneinfo in deze omgeving")
    yield
    monkeypatch.undo()
    time.tzset()


class _Feed:
    def get_price(self, market):
        return 100.0


def _positie_uit_db():
    with session() as s:
        s.add(PositionRow(market="X-EUR", mode="paper", amount=1, entry_price=100,
                          stop_loss=90, take_profit=120, opened_at=ENTRY))
        s.commit()
    return PaperBroker(_Feed(), FeeModel(0.15, 0.25, 0.1), 1000).open_positions()[0]


def test_opened_at_komt_als_utc_uit_de_db(memory_db, amsterdam):
    pos = _positie_uit_db()
    assert pos.opened_at.tzinfo is not None
    assert pos.opened_at == ENTRY


def test_breakeven_bewapent_niet_op_piek_van_voor_de_entry(memory_db, amsterdam):
    pos = _positie_uit_db()
    # De 08:00-bar (waarin de entry valt) had zijn high van 103 VOOR 09:30.
    cs = [Candle(BASE, 100, 103.0, 99, 100, 1), Candle(BASE + H, 100, 103.0, 99.5, 100, 1),
          Candle(BASE + 2 * H, 100, 100.4, 99.8, 100.2, 1)]
    hit, why = breakeven_stop_hit(cs, pos.opened_at, 100.0, 100.3, 2.0, 1.0, 0.55)
    assert not hit, why


def test_time_stop_telt_geen_extra_candle(memory_db, amsterdam):
    pos = _positie_uit_db()
    now = int(datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
    cs = [Candle(BASE + i * H, 100, 100, 100, 100, 1) for i in range(14)]
    hit, why = time_stop_hit(cs, pos.opened_at, 100, 99.9, 0.5, 12, 0.0, now_ms=now)
    assert hit and "12 candles" in why


def test_naieve_datetime_telt_als_utc_in_strategy(amsterdam):
    naief = ENTRY.replace(tzinfo=None)
    cs = [Candle(BASE + H, 100, 103.0, 99.5, 100, 1),
          Candle(BASE + 2 * H, 100, 100.4, 99.8, 100.2, 1)]
    hit, _ = breakeven_stop_hit(cs, naief, 100.0, 100.3, 2.0, 1.0, 0.55)
    assert not hit
