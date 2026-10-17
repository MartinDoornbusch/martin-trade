"""N3/N4 (v0.25.0): marktregels, foutafhandeling en verplichte velden richting Bitvavo."""
import json
from decimal import Decimal

import httpx
import pytest
import respx

from tradebot.exchange import BITVAVO_URL, BitvavoApiError, BitvavoClient, MarketInfo, fmt_decimal

ALT = MarketInfo.from_api({"market": "ALT-EUR", "status": "trading", "base": "ALT",
                           "quote": "EUR", "quantityDecimals": 2, "tickSize": "0.001",
                           "minOrderInBaseAsset": "1", "minOrderInQuoteAsset": "5"})
LEGACY = MarketInfo.from_api({"market": "OLD-EUR", "pricePrecision": 5,
                              "minOrderInQuoteAsset": "5"})


def test_hoeveelheid_altijd_naar_beneden():
    assert ALT.amount(24.999) == Decimal("24.99")
    assert ALT.amount(1.0) == Decimal("1.00")


def test_prijs_op_tick_size():
    assert fmt_decimal(ALT.price(2.0137)) == "2.013"
    assert fmt_decimal(ALT.price(2.0131, up=True)) == "2.014"


def test_kleine_tick_zonder_exponent():
    pepe = MarketInfo.from_api({"market": "PEPE-EUR", "tickSize": "0.00000001",
                                "quantityDecimals": 0})
    assert fmt_decimal(pepe.price(0.0000123456)) == "0.00001234"
    assert fmt_decimal(pepe.amount(1234567.9)) == "1234567"


def test_terugval_op_significante_cijfers():
    assert fmt_decimal(LEGACY.price(12345.678)) == "12345"
    assert fmt_decimal(LEGACY.price(0.000123456)) == "0.00012345"


def test_minimum_per_markt():
    assert ALT.below_minimum(Decimal("0.99"), 10.0)      # onder 1 ALT
    assert ALT.below_minimum(Decimal("2"), 2.0)          # 4 EUR < 5 EUR
    assert ALT.below_minimum(Decimal("3"), 2.0) is None


@respx.mock
def test_cancel_stuurt_operator_id_mee():
    route = respx.delete(url__startswith=f"{BITVAVO_URL}/order").mock(
        return_value=httpx.Response(200, json={"orderId": "x"}))
    BitvavoClient("k", "s").cancel_order("BTC-EUR", client_order_id="abc")
    assert "operatorId=1001" in str(route.calls.last.request.url)
    assert "clientOrderId=abc" in str(route.calls.last.request.url)


@respx.mock
def test_create_order_stuurt_operator_en_client_id():
    route = respx.post(f"{BITVAVO_URL}/order").mock(
        return_value=httpx.Response(200, json={"orderId": "x", "status": "new"}))
    BitvavoClient("k", "s").create_order({"market": "BTC-EUR", "side": "buy",
                                          "orderType": "limit", "amount": "0.001",
                                          "price": "50000", "clientOrderId": "c1"})
    body = json.loads(route.calls.last.request.content)
    assert body["operatorId"] == 1001 and body["clientOrderId"] == "c1"


@respx.mock
def test_foutbody_wordt_leesbaar_en_definitief():
    respx.post(f"{BITVAVO_URL}/order").mock(return_value=httpx.Response(
        400, json={"errorCode": 429, "error": "too many decimal places"}))
    with pytest.raises(BitvavoApiError) as e:
        BitvavoClient("k", "s").create_order({"market": "BTC-EUR"})
    assert e.value.error_code == 429 and e.value.definitive
    assert "too many decimal places" in str(e.value)


@respx.mock
def test_rate_limit_is_niet_definitief():
    respx.post(f"{BITVAVO_URL}/order").mock(return_value=httpx.Response(
        429, json={"errorCode": 110, "error": "rate limit"}))
    with pytest.raises(BitvavoApiError) as e:
        BitvavoClient("k", "s").create_order({"market": "BTC-EUR"})
    assert not e.value.definitive


@respx.mock
def test_marktinfo_wordt_gecachet():
    route = respx.get(url__startswith=f"{BITVAVO_URL}/markets").mock(
        return_value=httpx.Response(200, json=[{"market": "BTC-EUR", "quantityDecimals": 8,
                                                "tickSize": "1"}]))
    c = BitvavoClient()
    c.get_market_info("BTC-EUR")
    info = c.get_market_info("BTC-EUR")
    assert route.call_count == 1 and info.tick_size == Decimal("1")


@respx.mock
def test_saldo_beschikbaar_versus_totaal():
    respx.get(f"{BITVAVO_URL}/balance").mock(return_value=httpx.Response(
        200, json=[{"symbol": "EUR", "available": "40", "inOrder": "400"}]))
    c = BitvavoClient("k", "s")
    assert c.get_balances()["EUR"] == 440.0
    assert c.get_balances(available_only=True)["EUR"] == 40.0
