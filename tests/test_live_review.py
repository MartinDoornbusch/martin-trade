"""Scenario's uit de onafhankelijke review van het v0.25.0-orderpad (2026-10-17).

Elk scenario werd tegen de eerste versie gereproduceerd. Hier staat de VEILIGE
uitkomst vast. Rode draad: het account kan coins van de gebruiker zelf bevatten
(`set_balance("BTC", 0.01)`), dus een tweede verkoop of stop op basis van een
verkeerde aanname verkoopt zijn coins in plaats van te falen op error 216.

| Review | Scenario | Test |
|--------|----------|------|
| 1 | lookup-403 na timeout gold als "nooit geplaatst" | `test_r6b_entry_met_403_bij_opvraag_wordt_alsnog_geboekt` |
| 1 | 404 vlak na een timeout, order landt later | `test_r2_vertraagde_order_wordt_alsnog_geboekt` |
| 1 | 403 tijdens een guard-run wiste de stopkoppeling | `test_r6_403_op_stop_plaatst_geen_tweede_stop` |
| 2 | exit met onbekende stand, guard verkoopt opnieuw | `test_r1_onbekende_exit_geen_tweede_verkoop` |
| 2 | timeout tijdens het pollen van een exit | `test_r5_poll_timeout_geen_tweede_verkoop` |
| 2 | deels gevulde exit met onbekende stand | `test_r1b_restant_krijgt_stop_zodra_stand_bekend_is` |
| 3 | crash tussen versturen en koppelen van een stop | `test_r7_losse_stop_wordt_opgeruimd` |
| 3 | late entry-fill in bestaande positie gaf twee stops | `test_r4_late_entry_fill_vervangt_de_stop` |
| 4 | vals tekort na stop die vuurde tijdens downtime | `test_r3_geen_vals_tekort_na_offline_stop` |
| 5 | entry bleef op het boek als het pollen faalde | `test_entry_met_falende_poll_geeft_onbekend_en_boekt_fills` |
| 6 | stof afgeboekt terwijl een stop nog liep | `test_stof_wacht_op_lopende_stop` |
| 8 | late entry kreeg de boekingstijd als openingstijd | `test_late_entry_houdt_ordertijd` |
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from tests.fake_bitvavo import FakeBitvavo, api_error
from tradebot.db import OrderRow, session
from tradebot.decision import FeeModel
from tradebot.live import LiveBroker, OrderStateUnknownError

FEES = FeeModel(0.15, 0.25, 0.10, entry_is_maker=True)
EIGEN = Decimal("0.01")          # eigen BTC van de gebruiker op hetzelfde account


def broker_for(ex: FakeBitvavo, ghost_age_s: float = 300.0) -> LiveBroker:
    return LiveBroker(ex, FEES, 100.0, entry_timeout_s=0.05, poll_interval_s=0.0,
                      exit_timeout_s=0.05, ghost_age_s=ghost_age_s)


def btc(ex: FakeBitvavo) -> Decimal:
    b = ex.balances["BTC"]
    return b["available"] + b["inOrder"]


def gedeeld():
    ex = FakeBitvavo()
    ex.set_balance("BTC", float(EIGEN))
    return ex


def eenmalig_falende_opvraag(ex: FakeBitvavo, order_type: str, fout: Exception,
                             vul_eerst: bool = False):
    """Eerste get_order op een order van `order_type` faalt (eventueel nadat hij
    op de exchange gevuld is), daarna gedraagt de nabootsing zich normaal."""
    echt = ex.get_order
    staat = {"armed": True}

    def get(market, order_id=None, client_order_id=None):
        o = ex.orders.get(client_order_id)
        if o and o["orderType"] == order_type and staat["armed"]:
            staat["armed"] = False
            if vul_eerst:
                ex._market_fill(o)
            raise fout
        return echt(market, order_id, client_order_id)

    ex.get_order = get


@pytest.fixture()
def b_btc(memory_db):
    ex = gedeeld()
    b = broker_for(ex)
    b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    return b, ex


# --- review 2: geen nieuwe exit naast een exit met onbekende stand -------------
def test_r1_onbekende_exit_geen_tweede_verkoop(b_btc):
    b, ex = b_btc
    ex.set_price("BTC-EUR", 53500.0)
    ex.timeout_on_create = {"market"}                     # geplaatst en gevuld, antwoord kwijt
    eenmalig_falende_opvraag(ex, "market", httpx.ReadTimeout("lookup"))
    with pytest.raises(OrderStateUnknownError):
        b.sell("BTC-EUR", "tp")
    assert ex.orders_of("stopLoss", "awaitingTrigger") == []   # geen stop op eigen coins
    b.sell("BTC-EUR", "tp")                               # guard, een minuut later
    assert len(ex.orders_of("market")) == 1
    assert btc(ex) == EIGEN                                # eigen coins onaangeroerd
    assert b.open_positions() == []


def test_r5_poll_timeout_geen_tweede_verkoop(b_btc):
    b, ex = b_btc
    ex.market_answers_new_first = True
    eenmalig_falende_opvraag(ex, "market", httpx.ReadTimeout("poll"), vul_eerst=True)
    with pytest.raises(OrderStateUnknownError):
        b.sell("BTC-EUR", "tp")
    b.sell("BTC-EUR", "tp")
    assert len(ex.orders_of("market")) == 1
    assert btc(ex) == EIGEN and b.open_positions() == []


def test_r1b_restant_krijgt_stop_zodra_stand_bekend_is(memory_db):
    ex = FakeBitvavo()                                    # niet gedeeld
    b = broker_for(ex)
    b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    ex.market_fill_fraction = 0.6
    ex.timeout_on_create = {"market"}
    eenmalig_falende_opvraag(ex, "market", httpx.ReadTimeout("lookup"))
    with pytest.raises(OrderStateUnknownError):
        b.sell("BTC-EUR", "tp")
    b.ensure_protection()                                 # guard: stand nu wel op te vragen
    pos = b.open_positions()[0]
    stops = ex.orders_of("stopLoss", "awaitingTrigger")
    assert len(stops) == 1 and float(stops[0]["amount"]) == pytest.approx(pos.amount)
    assert float(btc(ex)) == pytest.approx(pos.amount)


# --- review 1: een mislukte opvraag is geen bewijs van "niet geplaatst" ---------
def test_r6b_entry_met_403_bij_opvraag_wordt_alsnog_geboekt(memory_db):
    ex = FakeBitvavo()
    ex.timeout_on_create = {"limit"}
    ex.lookup_errors = [api_error(403, 304, "not within access window")]
    b = broker_for(ex)
    with pytest.raises(OrderStateUnknownError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    problemen, notities = b.reconcile()
    assert problemen == [] and any("alsnog geboekt" in n for n in notities)
    assert len(b.open_positions()) == 1
    assert len(ex.orders_of("stopLoss", "awaitingTrigger")) == 1


def test_r2_vertraagde_order_wordt_alsnog_geboekt(memory_db):
    ex = FakeBitvavo()
    ex.timeout_on_create = {"limit"}
    ex.timeout_registers = False                          # 404 bij de eerste opvraag
    b = broker_for(ex)
    with pytest.raises(OrderStateUnknownError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    problemen, _ = b.reconcile()                          # nog te jong om op te geven
    assert any("nog niet afgeronde" in p for p in problemen)
    ex.register_delayed()                                 # hij landt toch, en vult
    problemen, notities = b.reconcile()
    assert problemen == [] and any("alsnog geboekt" in n for n in notities)
    assert len(b.open_positions()) == 1


def test_r6_403_op_stop_plaatst_geen_tweede_stop(b_btc):
    b, ex = b_btc
    ex.lookup_errors = [api_error(403, 304, "not within access window")]
    events = b.ensure_protection()
    assert events and "error" in events[0]
    assert len(ex.orders_of("stopLoss", "awaitingTrigger")) == 1
    ex.set_price("BTC-EUR", 47900.0)
    assert btc(ex) == EIGEN                                # alleen de botcoins verkocht


# --- review 3: één stop per positie, losse stops worden opgeruimd ----------------
def test_r7_losse_stop_wordt_opgeruimd(b_btc):
    b, ex = b_btc
    eerste = b.open_positions()[0].stop_order_cid
    with session() as s:                                  # oude crash: stop zonder koppeling
        from tradebot.db import PositionRow
        s.execute(select(PositionRow)).scalar_one().stop_order_cid = ""
        s.commit()
    problemen, notities = b.reconcile()
    assert problemen == [] and any("losse stop" in n for n in notities)
    actief = ex.orders_of("stopLoss", "awaitingTrigger")
    assert len(actief) == 1 and actief[0]["clientOrderId"] != eerste
    ex.set_price("BTC-EUR", 47900.0)
    assert btc(ex) == EIGEN


def test_r4_late_entry_fill_vervangt_de_stop(memory_db):
    ex = gedeeld()
    ex.limit_fill_fraction = 0.5
    b = broker_for(ex)
    echte_cancel = ex.cancel_order

    def cancel_timeout(*a, **k):
        raise httpx.ReadTimeout("cancel")

    ex.cancel_order = cancel_timeout
    with pytest.raises(OrderStateUnknownError):          # half gevuld, cancel onzeker
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    half = b.open_positions()[0].amount
    assert len(ex.orders_of("stopLoss", "awaitingTrigger")) == 1
    ex.cancel_order = echte_cancel
    entry = ex.orders_of("limit")[0]["clientOrderId"]
    ex.orders[entry]["status"] = "new"                    # de rest wordt alsnog gevuld
    ex.limit_fill_fraction = 1.0
    problemen, notities = b.reconcile()
    assert problemen == [] and any("alsnog geboekt" in n for n in notities)
    pos = b.open_positions()[0]
    actief = ex.orders_of("stopLoss", "awaitingTrigger")
    assert pos.amount == pytest.approx(2 * half, rel=1e-6)
    assert len(actief) == 1 and float(actief[0]["amount"]) == pytest.approx(pos.amount)
    ex.set_price("BTC-EUR", 47900.0)
    assert btc(ex) == EIGEN                                # precies de botcoins verkocht


def test_stop_koppelen_gebeurt_voor_versturen(memory_db, monkeypatch):
    ex = gedeeld()
    b = broker_for(ex)
    verstuurd: list[str] = []
    echt = ex.create_order

    def spion(body):
        if body["orderType"] == "stopLoss":
            verstuurd.append(b._linked_stop("BTC-EUR"))   # koppeling al aanwezig?
        return echt(body)

    ex.create_order = spion
    b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    assert verstuurd and verstuurd[0]


# --- review 4: reconciliatie boekt eerst, vergelijkt dan --------------------------
def test_r3_geen_vals_tekort_na_offline_stop(b_btc):
    b, ex = b_btc
    ex.set_price("BTC-EUR", 47900.0)                       # stop vuurt terwijl de bot plat ligt
    problemen, notities = b.reconcile()
    assert problemen == []
    assert any("exchange-stop geraakt" in n for n in notities)
    assert b.open_positions() == []


# --- review 5, 6, 8 ---------------------------------------------------------------
def test_entry_met_falende_poll_geeft_onbekend_en_boekt_fills(memory_db):
    ex = FakeBitvavo()
    ex.limit_fill_fraction = 0.5
    b = broker_for(ex)
    echt = ex.get_order
    calls = {"n": 0}

    def get(market, order_id=None, client_order_id=None):
        calls["n"] += 1
        resp = echt(market, order_id, client_order_id)    # deelvulling gebeurt hier
        if calls["n"] == 2:
            raise httpx.ReadTimeout("poll")
        return resp

    ex.get_order = get
    with pytest.raises(OrderStateUnknownError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    assert len(b.open_positions()) == 1                   # wat gevuld is, staat erin
    assert len(ex.orders_of("stopLoss", "awaitingTrigger")) == 1


def test_stof_wacht_op_lopende_stop(memory_db):
    ex = FakeBitvavo()
    b = broker_for(ex)
    b.buy("ALT-EUR", 50.0, 1.8, 2.4, "t")
    cid = b.open_positions()[0].stop_order_cid
    with session() as s:                                  # stop deels gevuld, loopt nog
        o = s.execute(select(OrderRow).where(OrderRow.client_order_id == cid)).scalar_one()
        o.status = "partiallyFilled"
        pos_amount = s.execute(select(OrderRow).where(OrderRow.purpose == "entry")
                               ).scalar_one().filled_amount
        o.filled_amount = pos_amount - 0.5                 # restant 0,5 ALT < minimum 1
        o.filled_quote = o.filled_amount * 1.8
        s.commit()
    b._book_exit(cid, ex.get_market_info("ALT-EUR"), "exchange-stop geraakt")
    assert len(b.open_positions()) == 1                    # niet afgeboekt zolang de stop loopt


def test_late_entry_houdt_ordertijd(memory_db):
    ex = FakeBitvavo()
    ex.timeout_on_create = {"limit"}
    ex.lookup_fails = True
    b = broker_for(ex)
    with pytest.raises(OrderStateUnknownError):
        b.buy("BTC-EUR", 50.0, 48000.0, 53000.0, "t")
    oud = datetime.now(timezone.utc) - timedelta(minutes=40)
    with session() as s:
        s.execute(select(OrderRow).where(OrderRow.purpose == "entry")).scalar_one().ts = oud
        s.commit()
    ex.lookup_fails = False
    b.reconcile()
    assert abs((b.open_positions()[0].opened_at - oud).total_seconds()) < 1
