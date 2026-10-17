"""Engine en guard in live mode, met de LiveBroker op de Bitvavo-nabootsing.

| Bevinding | Test |
|-----------|------|
| L5 live start met open paper-positie | `test_live_geweigerd_bij_open_paper_positie` |
| L2 guard laat de exchange-stop zijn werk doen | `test_guard_verkoopt_niet_binnen_de_stopmarge` |
| L2 guard grijpt in als de exchange-stop niet vuurde | `test_guard_verkoopt_als_de_stop_niet_vuurde` |
| L2 gevuurde exchange-stop wordt gelogd als exit | `test_guard_boekt_gevuurde_exchange_stop` |
| L4 reconciliatieverschil pauzeert kopen | `test_reconciliatieverschil_pauzeert_kopen` |
"""
import dataclasses

import pytest
from sqlalchemy import select

from tests.fake_bitvavo import FakeBitvavo
from tests.test_engine_cycle import FakeFeed, flat, make_cfg
from tradebot.config import Secrets
from tradebot.db import PositionRow, SignalRow, session
from tradebot.decision import FeeModel
from tradebot.engine import LIVE_CONFIRM_PHRASE, TradingCycle
from tradebot.lists import is_paused, set_paused
from tradebot.live import LiveBroker

LIVE = Secrets(trading_mode="live", live_confirm=LIVE_CONFIRM_PHRASE)


def live_cycle(price: float = 50000.0):
    cycle = TradingCycle(make_cfg(["BTC-EUR"]), LIVE)
    ex = FakeBitvavo(prices={"BTC-EUR": price})
    cycle.feed = FakeFeed({"BTC-EUR": flat(price)}, prices={"BTC-EUR": price})
    cycle.broker = LiveBroker(ex, FeeModel(0.15, 0.25, 0.1, entry_is_maker=True), 100.0,
                              entry_timeout_s=0.05, poll_interval_s=0.0, exit_timeout_s=0.05)
    set_paused(False)
    return cycle, ex


def test_live_geweigerd_bij_open_paper_positie(memory_db):
    with session() as s:
        s.add(PositionRow(market="ETH-EUR", mode="paper", amount=1, entry_price=2000,
                          stop_loss=1900, take_profit=2200))
        s.commit()
    with pytest.raises(RuntimeError, match="ETH-EUR"):
        TradingCycle(make_cfg(["BTC-EUR"]), LIVE)


def test_guard_verkoopt_niet_binnen_de_stopmarge(memory_db):
    cycle, ex = live_cycle()
    cycle.broker.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    cycle.feed.prices["BTC-EUR"] = 47900.0          # onder de stop, binnen 1%
    assert cycle.check_exits_fast() == 0
    assert ex.orders_of("market") == []
    assert len(cycle.broker.open_positions()) == 1


def test_guard_verkoopt_als_de_stop_niet_vuurde(memory_db):
    cycle, ex = live_cycle()
    cycle.broker.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    cycle.feed.prices["BTC-EUR"] = 47000.0          # >1% onder de stop, stop staat nog
    assert cycle.check_exits_fast() == 1
    assert len(ex.orders_of("market")) == 1
    assert cycle.broker.open_positions() == []


def test_guard_boekt_gevuurde_exchange_stop(memory_db):
    cycle, ex = live_cycle()
    cycle.broker.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    ex.set_price("BTC-EUR", 47900.0)                 # vuurt op de exchange
    assert cycle.check_exits_fast() == 1
    with session() as s:
        rows = s.execute(select(SignalRow).where(SignalRow.decision == "executed")
                         ).scalars().all()
    assert [r.reason for r in rows] == ["exchange-stop geraakt"]
    assert ex.orders_of("market") == []


def test_reconciliatieverschil_pauzeert_kopen(memory_db):
    cycle, ex = live_cycle()
    cycle.broker.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    ex.balances["BTC"] = {k: v / 2 for k, v in ex.balances["BTC"].items()}   # tekort
    cycle._reconcile_if_due()
    assert is_paused()
    with session() as s:
        rij = s.execute(select(SignalRow).where(SignalRow.decision == "mismatch")
                        ).scalar_one()
    assert "tekort BTC" in rij.reason
    set_paused(False)
    cycle._reconcile_if_due()                        # binnen het uur: niet opnieuw
    assert not is_paused()


def test_live_positie_draagt_stop_cid_in_dataclass(memory_db):
    cycle, _ = live_cycle()
    cycle.broker.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    pos = cycle.broker.open_positions()[0]
    assert dataclasses.asdict(pos)["stop_order_cid"]


def test_equity_snapshot_draagt_de_mode(memory_db):
    from tradebot.analysis.alfa import load_equity_from_db
    from tradebot.db import EquityRow
    from tradebot.main import snapshot_equity
    cycle, _ = live_cycle()
    with session() as s:
        s.add(EquityRow(total_eur=1000.0, cash_eur=1000.0))          # oude paper-rij
        s.commit()
    snapshot_equity(cycle)
    with session() as s:
        modes = sorted(r.mode for r in s.execute(select(EquityRow)).scalars().all())
    assert modes == ["live", "paper"]
    assert len(load_equity_from_db("live")) == 1


def test_export_bevat_het_orderboek(memory_db):
    from tradebot.export import build_export
    cycle, _ = live_cycle()
    cycle.broker.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    data = build_export(make_cfg(["BTC-EUR"]), mode="live")["data"]
    assert {o["purpose"] for o in data["orders"]} == {"entry", "stop"}


def test_onbekende_entry_vervroegt_reconciliatie(memory_db, monkeypatch):
    from tradebot.live import OrderStateUnknownError
    cycle, _ = live_cycle()
    cycle._last_reconcile = 10**12                   # net gedaan
    monkeypatch.setattr(cycle.broker, "reconcile", lambda: ([], []))

    def onbekend(*a, **k):
        raise OrderStateUnknownError("timeout")

    monkeypatch.setattr(cycle.broker, "buy", onbekend)
    cycle.cfg.strategy["min_signal_score"] = 0       # elke markt is een kandidaat
    cycle.run_once()
    assert cycle._last_reconcile == float("-inf")
