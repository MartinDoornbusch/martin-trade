"""LiveBroker tegen een strenge Bitvavo-nabootsing (tests/fake_bitvavo.py).

| Bevinding (review v0.24.0) | Test |
|----------------------------|------|
| L1/N2 deelvulling boekte de hele positie weg | `test_deelvulling_exit_verkleint_positie_en_boekt_eerlijk` |
| L1/N2 lege fills boekten de hele inleg als verlies | `test_exit_zonder_fill_laat_positie_staan` |
| L2 geen stop op de exchange | `test_entry_zet_stop_op_de_exchange`, `test_stop_die_vuurde_wordt_geboekt` |
| L2 exit mag de coins van een gevuurde stop niet nog eens verkopen | `test_exit_na_gevuurde_stop_verkoopt_niet_dubbel` |
| L3 dubbele sell vanuit twee threads | `test_twee_threads_een_market_order` |
| L4 reconciliatie: entry gevuld zonder positie | `test_reconcile_boekt_gevulde_entry_na_onbekende_status` |
| L4 reconciliatie: tekort op de exchange | `test_reconcile_meldt_tekort` |
| N3 afronding op quantityDecimals en tickSize | `test_afronding_volgt_marktregels` |
| N3 onder het marktminimum | `test_entry_onder_minimum_gaat_niet_de_deur_uit` |
| N4 timeout na plaatsing: opvragen, niet opnieuw plaatsen | `test_timeout_na_plaatsing_vindt_order_terug` |
| N4 timeout voor plaatsing: zeker niet geplaatst | `test_timeout_voor_plaatsing_is_afgewezen` |
| N8 cash zonder EUR in open orders | `test_cash_telt_inorder_niet_mee` |
"""
import threading

import pytest
from sqlalchemy import select

from tests.fake_bitvavo import FakeBitvavo
from tradebot.db import OrderRow, PositionRow, TradeRow, session
from tradebot.decision import FeeModel, PositionGone
from tradebot.live import (
    ExitNotFilledError,
    LiveBroker,
    OrderRejectedError,
    OrderStateUnknownError,
    PartialExitError,
)

FEES = FeeModel(0.15, 0.25, 0.10, entry_is_maker=True)


def broker_for(ex: FakeBitvavo, cap: float = 100.0) -> LiveBroker:
    return LiveBroker(ex, FEES, cap, entry_timeout_s=0.05, poll_interval_s=0.0,
                      exit_timeout_s=0.05)


def trades(side: str) -> list[TradeRow]:
    with session() as s:
        return list(s.execute(select(TradeRow).where(TradeRow.side == side)).scalars().all())


def orders(purpose: str) -> list[OrderRow]:
    with session() as s:
        return list(s.execute(select(OrderRow).where(OrderRow.purpose == purpose))
                    .scalars().all())


@pytest.fixture()
def ex():
    return FakeBitvavo()


@pytest.fixture()
def open_btc(memory_db, ex):
    b = broker_for(ex)
    b.buy("BTC-EUR", 50.0, stop_loss=48000.0, take_profit=53000.0, reason="test")
    return b


# --- entries ---------------------------------------------------------------
def test_entry_maker_fill_maakt_positie(open_btc):
    pos = open_btc.open_positions()
    assert len(pos) == 1
    assert pos[0].entry_price == pytest.approx(50000.0)
    assert 49.0 < open_btc.exposure_eur() <= 50.0


def test_entry_zet_stop_op_de_exchange(open_btc, ex):
    stops = ex.orders_of("stopLoss", "awaitingTrigger")
    pos = open_btc.open_positions()[0]
    assert len(stops) == 1
    assert float(stops[0]["amount"]) == pytest.approx(pos.amount)
    assert stops[0]["triggerAmount"] == "48000"
    assert pos.stop_order_cid == stops[0]["clientOrderId"]


def test_entry_timeout_annuleert_en_geeft_reservering_vrij(memory_db, ex):
    ex.limit_never_fills = True
    b = broker_for(ex)
    with pytest.raises(TimeoutError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    assert b.open_positions() == []
    assert ex.balances["EUR"]["inOrder"] == 0
    assert orders("entry")[0].status == "canceled"


def test_postonly_weigering_wacht_niet_tot_de_timeout(memory_db, ex):
    ex.postonly_reject = True
    b = LiveBroker(ex, FEES, 100.0, entry_timeout_s=30.0, poll_interval_s=0.0)
    with pytest.raises(TimeoutError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")   # zou 30 s duren als hij pollt
    assert len(ex.created) == 1


def test_gedeeltelijke_entry_wordt_positie_van_wat_gevuld_is(memory_db, ex):
    ex.limit_fill_fraction = 0.5
    ex.limit_fill_after_polls = 0
    b = broker_for(ex)
    b.buy("ALT-EUR", 50.0, 1.8, 2.4, "test")
    pos = b.open_positions()[0]
    sent = float(orders("entry")[0].amount)
    assert pos.amount == pytest.approx(round(sent * 0.5, 2))
    assert float(ex.orders_of("stopLoss")[0]["amount"]) == pytest.approx(pos.amount)


def test_exposure_plafond_blokkeert_entry(memory_db, ex):
    b = broker_for(ex, cap=60.0)
    b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    with pytest.raises(ValueError, match="exposure-plafond"):
        b.buy("ALT-EUR", 50.0, 1.8, 2.4, "test")


def test_tweede_entry_in_dezelfde_markt_geweigerd(open_btc):
    with pytest.raises(ValueError, match="al open"):
        open_btc.buy("BTC-EUR", 20.0, 48000.0, 53000.0, "test")


def test_afronding_volgt_marktregels(memory_db, ex):
    """ALT-EUR: 2 decimalen, tick 0.001. De nabootsing weigert alles daarbuiten."""
    ex.prices["ALT-EUR"] = 2.0137
    b = broker_for(ex)
    b.buy("ALT-EUR", 50.0, stop_loss=1.87654, take_profit=2.4, reason="test")
    entry, stop = ex.created[0], ex.created[1]
    assert entry["price"] == "2.013"                     # naar beneden: blijft maker
    assert len(entry["amount"].split(".")[-1]) <= 2
    assert stop["triggerAmount"] == "1.876"
    ex.set_price("ALT-EUR", 2.3)
    b.sell("ALT-EUR", "take profit")
    assert len(ex.created[-1]["amount"].split(".")[-1]) <= 2
    assert b.open_positions() == []


def test_entry_onder_minimum_gaat_niet_de_deur_uit(memory_db, ex):
    b = broker_for(ex)
    with pytest.raises(OrderRejectedError, match="minimum"):
        b.buy("ALT-EUR", 1.5, 1.8, 2.4, "test")          # 0,74 ALT < minimum 1 ALT
    assert ex.created == []


def test_markt_niet_in_trading_geen_entry(memory_db, ex):
    ex.markets["ALT-EUR"]["status"] = "halted"
    with pytest.raises(OrderRejectedError, match="halted"):
        broker_for(ex).buy("ALT-EUR", 50.0, 1.8, 2.4, "test")


# --- exits -----------------------------------------------------------------
def test_exit_annuleert_stop_en_boekt_winst(open_btc, ex):
    ex.set_price("BTC-EUR", 53000.0)
    open_btc.sell("BTC-EUR", "take profit")
    assert open_btc.open_positions() == []
    assert ex.orders_of("stopLoss")[0]["status"] == "canceled"
    assert trades("sell")[0].pnl_eur == pytest.approx(0.999 * 0.000998 * 3000, rel=0.15)
    assert ex.balances["BTC"]["available"] + ex.balances["BTC"]["inOrder"] == 0


def test_deelvulling_exit_verkleint_positie_en_boekt_eerlijk(open_btc, ex):
    """N2: 40% gevuld. Oud: positie weg en -60% als verlies geboekt."""
    voor = open_btc.open_positions()[0].amount
    ex.market_fill_fraction = 0.4
    with pytest.raises(PartialExitError):
        open_btc.sell("BTC-EUR", "stop")
    pos = open_btc.open_positions()[0]
    assert pos.amount == pytest.approx(voor - round(voor * 0.4, 8), abs=1e-8)
    sell = trades("sell")[0]
    assert sell.amount == pytest.approx(round(voor * 0.4, 8))
    assert abs(sell.pnl_eur) < 0.2                      # koers = entry: alleen fees
    nieuwe_stop = ex.orders_of("stopLoss", "awaitingTrigger")
    assert len(nieuwe_stop) == 1 and float(nieuwe_stop[0]["amount"]) == pytest.approx(pos.amount)


def test_exit_zonder_fill_laat_positie_staan(open_btc, ex):
    """N2: niets gevuld. Oud: positie gewist en de hele inleg als verlies."""
    voor = open_btc.open_positions()[0].amount
    ex.market_fill_fraction = 0.0
    with pytest.raises(ExitNotFilledError):
        open_btc.sell("BTC-EUR", "stop")
    assert open_btc.open_positions()[0].amount == pytest.approx(voor)
    assert trades("sell") == []
    assert len(ex.orders_of("stopLoss", "awaitingTrigger")) == 1   # weer beschermd


def test_market_order_die_eerst_new_antwoordt(open_btc, ex):
    ex.market_answers_new_first = True
    open_btc.sell("BTC-EUR", "tp")
    assert open_btc.open_positions() == []


def test_stop_die_vuurde_wordt_geboekt(open_btc, ex):
    ex.set_price("BTC-EUR", 47900.0)                      # stop vuurt op de exchange
    events = open_btc.ensure_protection()
    assert events and events[0]["market"] == "BTC-EUR" and events[0]["sold"] > 0
    assert open_btc.open_positions() == []
    assert trades("sell")[0].reason == "exchange-stop geraakt"


def test_exit_na_gevuurde_stop_verkoopt_niet_dubbel(open_btc, ex):
    ex.set_price("BTC-EUR", 47900.0)
    open_btc.sell("BTC-EUR", "guard: stop loss")
    assert ex.orders_of("market") == []                   # geen tweede verkoop
    assert len(trades("sell")) == 1
    assert open_btc.open_positions() == []


def test_ensure_protection_plaatst_ontbrekende_stop(open_btc, ex):
    cid = open_btc.open_positions()[0].stop_order_cid
    ex.cancel_order("BTC-EUR", client_order_id=cid)       # stop verdwenen (bijv. handmatig)
    open_btc.ensure_protection()
    actief = ex.orders_of("stopLoss", "awaitingTrigger")
    assert len(actief) == 1 and actief[0]["clientOrderId"] != cid


def test_sell_zonder_positie_is_position_gone(memory_db, ex):
    with pytest.raises(PositionGone):
        broker_for(ex).sell("BTC-EUR", "x")


def test_twee_threads_een_market_order(tmp_path, ex):
    from tradebot import db
    db._engine = db._Session = None
    db.init_db(f"sqlite:///{tmp_path}/live.db")
    b = broker_for(ex)
    b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    uitkomst: list[str] = []

    def verkoop():
        try:
            b.sell("BTC-EUR", "race")
            uitkomst.append("verkocht")
        except PositionGone:
            uitkomst.append("al weg")

    threads = [threading.Thread(target=verkoop) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db._engine = db._Session = None
    assert sorted(uitkomst) == ["al weg", "verkocht"]
    assert len(ex.orders_of("market")) == 1


# --- foutpaden rond plaatsen (N4) en reconciliatie (L4) ---------------------
def test_timeout_na_plaatsing_vindt_order_terug(memory_db, ex):
    ex.timeout_on_create = {"limit"}
    b = broker_for(ex)
    b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    assert len(ex.orders_of("limit")) == 1                # niet opnieuw geplaatst
    assert len(b.open_positions()) == 1


def test_timeout_voor_plaatsing_is_afgewezen(memory_db, ex):
    ex.timeout_on_create = {"limit"}
    ex.timeout_registers = False
    b = broker_for(ex)
    with pytest.raises(OrderRejectedError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    assert orders("entry")[0].status == "rejected"
    assert b.open_positions() == []


def test_definitieve_weigering_staat_in_het_orderboek(memory_db, ex):
    ex.set_balance("EUR", 10.0)
    with pytest.raises(OrderRejectedError, match="216"):
        broker_for(ex).buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    row = orders("entry")[0]
    assert row.status == "rejected" and "216" in row.error


def test_reconcile_boekt_gevulde_entry_na_onbekende_status(memory_db, ex):
    ex.timeout_on_create = {"limit"}
    ex.lookup_fails = True
    b = broker_for(ex)
    with pytest.raises(OrderStateUnknownError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "test")
    assert b.open_positions() == []
    ex.lookup_fails = False
    problemen, notities = b.reconcile()
    assert problemen == []
    assert any("alsnog geboekt" in n for n in notities)
    pos = b.open_positions()[0]
    assert (pos.stop_loss, pos.take_profit) == (48000.0, 53000.0)   # uit de outbox-meta
    assert len(ex.orders_of("stopLoss", "awaitingTrigger")) == 1


def test_reconcile_boekt_exit_na_crash_voor_het_boeken(open_btc, ex, monkeypatch):
    echt = open_btc._book_exit

    def crash(cid, info, reason):
        if reason == "tp":       # alleen de exit zelf, niet het afboeken van de stop
            raise RuntimeError("proces viel om tussen fill en boeking")
        return echt(cid, info, reason)

    monkeypatch.setattr(open_btc, "_book_exit", crash)
    with pytest.raises(RuntimeError):
        open_btc.sell("BTC-EUR", "tp")
    monkeypatch.undo()
    assert len(open_btc.open_positions()) == 1            # administratie loopt achter
    problemen, notities = open_btc.reconcile()
    assert problemen == [] and any("exit" in n for n in notities)
    assert open_btc.open_positions() == []


def test_reconcile_meldt_tekort(open_btc, ex):
    ex.balances["BTC"] = {"available": ex.balances["BTC"]["available"],
                          "inOrder": ex.balances["BTC"]["inOrder"] / 2}
    problemen, _ = open_btc.reconcile()
    assert any("tekort BTC" in p for p in problemen)


def test_reconcile_accepteert_overschot_van_eigen_coins(open_btc, ex):
    from decimal import Decimal
    ex.balances["BTC"]["available"] += Decimal("1.5")      # eigen coins van de gebruiker
    problemen, _ = open_btc.reconcile()
    assert problemen == []


def test_cash_telt_inorder_niet_mee(memory_db, ex):
    ex.set_balance("EUR", 40.0, in_order=400.0)
    assert broker_for(ex, cap=100.0).cash_eur() == pytest.approx(40.0)


def test_cash_respecteert_plafond(memory_db, ex):
    ex.set_balance("EUR", 5000.0)
    assert broker_for(ex, cap=100.0).cash_eur() == 100.0


def test_positierij_onthoudt_de_stop(open_btc):
    with session() as s:
        row = s.execute(select(PositionRow)).scalar_one()
    assert row.stop_order_cid
