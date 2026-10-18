"""Nabootsing van Bitvavo voor de live-broker (v0.25.0).

Stateful en streng op de punten waar de echte exchange een order weigert of
anders afhandelt dan de oude code aannam:

- hoeveelheid met meer decimalen dan `quantityDecimals`  -> error 429
- prijs of trigger die geen veelvoud van `tickSize` is   -> error 422
- onder `minOrderInBaseAsset` / `minOrderInQuoteAsset`   -> error 212 / 217
- onvoldoende saldo (ook coins die een stop reserveert)  -> error 216
- dubbele `clientOrderId`                                -> error 220
- annuleren van een order die al final is                -> error 240
- onbekende order                                        -> 404, error 240

Responsvormen volgen de Bitvavo-SDK: `status`, `filledAmount`,
`filledAmountQuote`, `feePaid`, `feeCurrency`, `fills`, `clientOrderId`.
Market orders vullen standaard volledig; `market_fill_fraction` laat de
slippage-bescherming het restant annuleren. Een stopLoss staat op
`awaitingTrigger` en vuurt als `set_price` de trigger raakt.

Foutinjectie: `timeout_on_create` (set met orderTypes, eenmalig) gooit een
`httpx.ReadTimeout` NA het registreren van de order als `timeout_registers`
waar is, anders ervoor; `lookup_fails` laat `get_order` met een timeout falen.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx

from tradebot.exchange import FINAL_STATUSES, BitvavoApiError, MarketInfo

MARKETS = {
    "BTC-EUR": {"market": "BTC-EUR", "status": "trading", "base": "BTC", "quote": "EUR",
                "quantityDecimals": 8, "tickSize": "1", "minOrderInBaseAsset": "0.00006",
                "minOrderInQuoteAsset": "5"},
    # Grove regels om afronding af te dwingen: 2 decimalen, tick 0.001.
    "ALT-EUR": {"market": "ALT-EUR", "status": "trading", "base": "ALT", "quote": "EUR",
                "quantityDecimals": 2, "tickSize": "0.001", "minOrderInBaseAsset": "1",
                "minOrderInQuoteAsset": "5"},
}


def api_error(status: int, code: int, text: str) -> BitvavoApiError:
    req = httpx.Request("POST", "https://api.bitvavo.com/v2/order")
    return BitvavoApiError(httpx.Response(status, json={"errorCode": code, "error": text},
                                          request=req))


def _decimals(value: str) -> int:
    d = Decimal(value)
    return max(0, -d.as_tuple().exponent)


class FakeBitvavo:
    MAKER, TAKER = Decimal("0.0015"), Decimal("0.0025")

    def __init__(self, prices: dict[str, float] | None = None, eur: float = 1000.0):
        self.markets = {m: dict(v) for m, v in MARKETS.items()}
        self.prices = {"BTC-EUR": 50000.0, "ALT-EUR": 2.0, **(prices or {})}
        self.balances: dict[str, dict[str, Decimal]] = {
            "EUR": {"available": Decimal(str(eur)), "inOrder": Decimal(0)}}
        self.orders: dict[str, dict] = {}
        self.created: list[dict] = []
        self.limit_fill_after_polls = 0      # aantal get_order-calls tot fill
        self.limit_fill_fraction = 1.0
        self.limit_never_fills = False
        self.postonly_reject = False
        self.market_fill_fraction = 1.0
        self.market_answers_new_first = False
        self.timeout_on_create: set[str] = set()
        self.timeout_registers = True
        self.lookup_fails = False
        self.lookup_errors: list[Exception] = []    # FIFO: volgende get_order-calls falen
        self.delayed: list[dict] = []               # verzonden maar nog niet geregistreerd
        self.market_info_calls = 0

    # --- helpers -------------------------------------------------------------
    def _bal(self, sym: str) -> dict[str, Decimal]:
        return self.balances.setdefault(sym, {"available": Decimal(0), "inOrder": Decimal(0)})

    def set_balance(self, sym: str, available: float, in_order: float = 0.0) -> None:
        self.balances[sym] = {"available": Decimal(str(available)),
                              "inOrder": Decimal(str(in_order))}

    def _info(self, market: str) -> MarketInfo:
        return MarketInfo.from_api(self.markets[market])

    def _view(self, o: dict) -> dict:
        out = {k: v for k, v in o.items() if not k.startswith("_")}
        for k in ("amount", "amountRemaining", "filledAmount", "filledAmountQuote", "feePaid",
                  "price", "triggerAmount"):
            if k in out and out[k] is not None:
                out[k] = format(Decimal(out[k]).normalize(), "f")
        return out

    def _execute(self, o: dict, amount: Decimal, price: Decimal, fee_rate: Decimal) -> None:
        """Vul `amount` tegen `price` en werk saldi bij (fee in EUR)."""
        if amount <= 0:
            return
        info = self._info(o["market"])
        quote = amount * price
        fee = quote * fee_rate
        base, eur = self._bal(info.base), self._bal("EUR")
        if o["side"] == "buy":
            # Alleen het gevulde deel van de reservering vrijgeven; de rest blijft
            # vastzitten zolang de order loopt, net als op de echte exchange.
            reserved = o.get("_reserved", Decimal(0))
            part = min(reserved, reserved * amount / (Decimal(o["amount"])
                                                       - Decimal(o["filledAmount"])))
            o["_reserved"] = reserved - part
            eur["inOrder"] -= part
            eur["available"] += part - quote - fee
            base["available"] += amount
        else:
            src = "inOrder" if o["_reserved_base"] else "available"
            base[src] -= amount
            eur["available"] += quote - fee
        o["filledAmount"] = Decimal(o["filledAmount"]) + amount
        o["filledAmountQuote"] = Decimal(o["filledAmountQuote"]) + quote
        o["feePaid"] = Decimal(o["feePaid"]) + fee
        o["amountRemaining"] = Decimal(o["amount"]) - o["filledAmount"]
        o["fills"].append({"amount": str(amount), "price": str(price), "fee": str(fee),
                           "feeCurrency": "EUR"})

    def _release(self, o: dict) -> None:
        info = self._info(o["market"])
        if o["side"] == "buy" and o.get("_reserved"):
            eur = self._bal("EUR")
            eur["inOrder"] -= o["_reserved"]
            eur["available"] += o["_reserved"]
            o["_reserved"] = Decimal(0)
        if o["side"] == "sell" and o["_reserved_base"]:
            rest = Decimal(o["amount"]) - Decimal(o["filledAmount"])
            base = self._bal(info.base)
            base["inOrder"] -= rest
            base["available"] += rest
            o["_reserved_base"] = False

    def _market_fill(self, o: dict) -> None:
        info = self._info(o["market"])
        price = Decimal(str(self.prices[o["market"]]))
        qty = (Decimal(o["amount"]) * Decimal(str(self.market_fill_fraction))).quantize(
            Decimal(1).scaleb(-info.quantity_decimals))
        self._execute(o, qty, price, self.TAKER)
        if o["_reserved_base"]:
            self._release(o)
        o["status"] = "filled" if Decimal(o["amountRemaining"]) <= 0 else (
            "canceled" if qty > 0 else "expired")

    # --- REST-oppervlak van BitvavoClient ---------------------------------------
    def get_market_info(self, market: str) -> MarketInfo:
        self.market_info_calls += 1
        return self._info(market)

    def get_book_ticker(self, market: str) -> dict:
        p = self.prices[market]
        return {"bid": str(p), "ask": str(p * 1.001)}

    def get_price(self, market: str) -> float:
        return self.prices[market]

    def get_balances(self, available_only: bool = False) -> dict[str, float]:
        if available_only:
            return {s: float(b["available"]) for s, b in self.balances.items()}
        return {s: float(b["available"] + b["inOrder"]) for s, b in self.balances.items()}

    def create_order(self, body: dict) -> dict:
        cid = body.get("clientOrderId")
        if not cid:
            raise api_error(400, 203, "clientOrderId ontbreekt in deze test-exchange")
        if cid in self.orders:
            raise api_error(400, 220, "A clientOrderId with this value already exists")
        market = body["market"]
        info = self._info(market)
        amount = body["amount"]
        if _decimals(amount) > info.quantity_decimals:
            raise api_error(400, 429, "amount has too many decimal places")
        for key in ("price", "triggerAmount"):
            if key in body and info.tick_size is not None \
                    and Decimal(body[key]) % info.tick_size != 0:
                raise api_error(400, 422, f"{key} does not align with tick size")
        qty = Decimal(amount)
        ref = Decimal(body.get("price") or body.get("triggerAmount") or str(self.prices[market]))
        if qty < info.min_base:
            raise api_error(400, 212, "amount below minOrderInBaseAsset")
        if qty * ref < info.min_quote:
            raise api_error(400, 217, "order value below minOrderInQuoteAsset")
        side, otype = body["side"], body["orderType"]
        timeout = otype in self.timeout_on_create
        if timeout:
            self.timeout_on_create.discard(otype)      # eenmalig
            if not self.timeout_registers:
                # Kan nog binnenkomen: `register_delayed` laat hem alsnog landen,
                # zoals bij een 503 errorCode 109 ("may or may not have succeeded").
                self.delayed.append(dict(body))
                raise httpx.ReadTimeout("timeout voor registratie")
        o = {"orderId": str(uuid.uuid4()), "clientOrderId": cid, "market": market,
             "side": side, "orderType": otype, "amount": qty, "amountRemaining": qty,
             "price": body.get("price"), "triggerAmount": body.get("triggerAmount"),
             "filledAmount": Decimal(0), "filledAmountQuote": Decimal(0),
             "feePaid": Decimal(0), "feeCurrency": "EUR", "fills": [],
             "postOnly": bool(body.get("postOnly", False)), "status": "new",
             "_polls": 0, "_reserved": Decimal(0), "_reserved_base": False}
        base, eur = self._bal(info.base), self._bal("EUR")
        if side == "sell":
            if base["available"] < qty:
                raise api_error(400, 216, "insufficient balance")
            if otype == "stopLoss":
                base["available"] -= qty
                base["inOrder"] += qty
                o["_reserved_base"] = True
                o["status"] = "awaitingTrigger"
        elif otype == "limit":
            need = qty * Decimal(body["price"]) * (1 + self.MAKER)
            if eur["available"] < need:
                raise api_error(400, 216, "insufficient balance")
            eur["available"] -= need
            eur["inOrder"] += need
            o["_reserved"] = need
        self.orders[cid] = o
        self.created.append(dict(body))
        if otype == "limit" and o["postOnly"] and self.postonly_reject:
            self._release(o)
            o["status"] = "canceled"
        elif otype == "market" and not self.market_answers_new_first:
            self._market_fill(o)
        if timeout:
            raise httpx.ReadTimeout("timeout na registratie")
        return self._view(o)

    def register_delayed(self) -> None:
        """Laat orders die 'onderweg' waren alsnog op de exchange landen."""
        bodies, self.delayed = self.delayed, []
        for body in bodies:
            self.create_order(body)

    def get_order(self, market: str, order_id: str | None = None,
                  client_order_id: str | None = None) -> dict:
        if self.lookup_fails:
            raise httpx.ReadTimeout("lookup timeout")
        if self.lookup_errors:
            raise self.lookup_errors.pop(0)
        o = self.orders.get(client_order_id) if client_order_id else next(
            (x for x in self.orders.values() if x["orderId"] == order_id), None)
        if o is None:
            req = httpx.Request("GET", "https://api.bitvavo.com/v2/order")
            raise BitvavoApiError(httpx.Response(404, json={"errorCode": 240,
                                                            "error": "No order found"},
                                                 request=req))
        o["_polls"] += 1
        if o["orderType"] == "market" and o["status"] == "new":
            self._market_fill(o)
        if (o["orderType"] == "limit" and o["status"] == "new" and not self.limit_never_fills
                and o["_polls"] > self.limit_fill_after_polls):
            info = self._info(market)
            qty = (Decimal(o["amount"]) * Decimal(str(self.limit_fill_fraction))).quantize(
                Decimal(1).scaleb(-info.quantity_decimals))
            self._execute(o, qty - Decimal(o["filledAmount"]), Decimal(o["price"]), self.MAKER)
            o["status"] = "filled" if Decimal(o["amountRemaining"]) <= 0 else "partiallyFilled"
        return self._view(o)

    def cancel_order(self, market: str, order_id: str | None = None,
                     client_order_id: str | None = None) -> dict:
        o = self.orders.get(client_order_id)
        if o is None or o["status"] in FINAL_STATUSES:
            raise api_error(400, 240, "No active order found")
        self._release(o)
        o["status"] = "canceled"
        return {"orderId": o["orderId"]}

    def get_open_orders(self, market: str | None = None) -> list[dict]:
        return [self._view(o) for o in self.orders.values()
                if o["status"] not in FINAL_STATUSES and (market is None or o["market"] == market)]

    # --- markt laten bewegen ---------------------------------------------------
    def set_price(self, market: str, price: float) -> None:
        """Nieuwe koers; een stopLoss op of boven die koers vuurt als market order."""
        self.prices[market] = price
        for o in self.orders.values():
            if (o["market"] == market and o["orderType"] == "stopLoss"
                    and o["status"] == "awaitingTrigger"
                    and Decimal(str(price)) <= Decimal(o["triggerAmount"])):
                self._market_fill(o)

    def orders_of(self, order_type: str, status: str | None = None) -> list[dict]:
        return [self._view(o) for o in self.orders.values()
                if o["orderType"] == order_type and (status is None or o["status"] == status)]
