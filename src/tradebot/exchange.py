"""Exchange adapters. Bitvavo now; the ABC keeps stock brokers (Alpaca/IBKR) pluggable later.

Bitvavo API v2: HMAC-SHA256 signed requests, weight-based rate limit (1000/min),
mandatory int64 operatorId on trading endpoints.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import httpx

log = logging.getLogger(__name__)

BITVAVO_URL = "https://api.bitvavo.com/v2"
OPERATOR_ID = 1001  # identifies this bot within the account (Bitvavo requirement)


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def parse_end_ms(waarde: str | None) -> int | None:
    """CLI-waarde naar unix-milliseconden: ISO-8601 of een epoch in ms.

    Nodig omdat `--limit N` de N NIEUWSTE candles ophaalt. Twee runs op
    verschillende momenten beslaan dus een ander venster, en dan vergelijk je
    stilzwijgend appels met peren. Voor de anker-check is dat fataal: precies het
    stille verschil dat die check moet uitsluiten.
    """
    if waarde is None or not str(waarde).strip():
        return None
    tekst = str(waarde).strip()
    if tekst.isdigit():
        return int(tekst)
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(tekst.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def candle_window(candles: list[Candle]) -> str:
    """Leesbaar venster van een reeks, om in elke run-uitvoer te tonen."""
    if not candles:
        return "leeg"
    from datetime import datetime, timezone

    def fmt(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"{fmt(candles[0].ts)} t/m {fmt(candles[-1].ts)} UTC"


class BitvavoApiError(httpx.HTTPStatusError):
    """HTTP-fout van Bitvavo met de `errorCode` en tekst uit de body.

    `raise_for_status` gooide alleen de statusregel weg; de reden (error 429
    "too many decimal places", 216 "insufficient balance") stond in de body.
    Subklasse van `HTTPStatusError`, dus bestaande `except httpx.HTTPError`-paden
    blijven werken.
    """

    def __init__(self, response: httpx.Response):
        try:
            body = response.json()
        except ValueError:
            body = {}
        self.error_code = int(body.get("errorCode", 0) or 0) if isinstance(body, dict) else 0
        self.error_text = str(body.get("error", "")) if isinstance(body, dict) else ""
        super().__init__(
            f"Bitvavo {response.status_code} errorCode {self.error_code}: "
            f"{self.error_text or response.text[:200]}",
            request=response.request, response=response)

    @property
    def definitive(self) -> bool:
        """Op een PLAATSING: 4xx anders dan rate limit betekent zeker niet geplaatst.

        Niet gebruiken op een OPVRAAG. Een 403 (errorCode 304, buiten het
        toegangsvenster) of een 404 vlak na een timeout zegt niets over het
        bestaan van de order; zie `not_found` en `LiveBroker._confirm_missing`.
        """
        status = self.response.status_code
        return 400 <= status < 500 and status != 429

    @property
    def not_found(self) -> bool:
        """errorCode 240: de order bestaat niet of is niet meer actief (HTTP 404)."""
        return self.error_code == 240 or (self.response.status_code == 404
                                          and self.error_code == 0)


# Bitvavo-statussen waarna een order niet meer verandert.
FINAL_STATUSES = frozenset({"filled", "canceled", "cancelled", "expired", "rejected"})


@dataclass(frozen=True)
class MarketInfo:
    """Handelsregels van één markt uit `GET /markets` (N3, v0.25.0).

    `quantity_decimals` en `tick_size` zijn de huidige velden; `price_precision`
    (significante cijfers) is deprecated maar dient als terugval als `tickSize`
    ontbreekt.
    """

    market: str
    status: str
    base: str
    quote: str
    quantity_decimals: int
    tick_size: Decimal | None
    price_precision: int
    min_base: Decimal
    min_quote: Decimal

    @classmethod
    def from_api(cls, raw: dict) -> MarketInfo:
        tick = raw.get("tickSize")
        return cls(
            market=str(raw["market"]), status=str(raw.get("status", "")),
            base=str(raw.get("base", "")), quote=str(raw.get("quote", "")),
            quantity_decimals=int(raw.get("quantityDecimals", 8)),
            tick_size=Decimal(str(tick)) if tick not in (None, "", "0") else None,
            price_precision=int(raw.get("pricePrecision", 5) or 5),
            min_base=Decimal(str(raw.get("minOrderInBaseAsset", "0") or "0")),
            min_quote=Decimal(str(raw.get("minOrderInQuoteAsset", "0") or "0")),
        )

    def amount(self, value: float) -> Decimal:
        """Hoeveelheid naar BENEDEN op het toegestane aantal decimalen: nooit meer
        verkopen dan er staat en nooit meer kopen dan het budget."""
        step = Decimal(1).scaleb(-self.quantity_decimals)
        return Decimal(str(value)).quantize(step, rounding=ROUND_FLOOR)

    def price(self, value: float, up: bool = False) -> Decimal:
        """Prijs op een veelvoud van de tick size (of op significante cijfers als
        terugval). Standaard naar beneden: een postOnly-bied blijft dan maker."""
        rounding = ROUND_CEILING if up else ROUND_FLOOR
        d = Decimal(str(value))
        if self.tick_size:
            return ((d / self.tick_size).to_integral_value(rounding=rounding)
                    * self.tick_size).normalize()
        if d == 0:
            return d
        exp = d.adjusted() - self.price_precision + 1
        return d.quantize(Decimal(1).scaleb(exp), rounding=rounding).normalize()

    def below_minimum(self, amount: Decimal, price: float) -> str | None:
        """Reden als de order onder het minimum van de markt valt, anders None."""
        if amount <= 0:
            return "hoeveelheid is nul na afronding"
        if amount < self.min_base:
            return f"{amount} < minimum {self.min_base} {self.base}"
        if amount * Decimal(str(price)) < self.min_quote:
            return f"waarde < minimum {self.min_quote} {self.quote}"
        return None


def fmt_decimal(d: Decimal) -> str:
    """Decimal als string zonder exponent (Bitvavo weigert '1E-7')."""
    return format(d, "f")


@dataclass
class OrderResult:
    order_id: str
    market: str
    side: str
    amount: float          # base asset amount
    price: float           # avg fill price
    fee_eur: float
    raw: dict | None = None


class ExchangeAdapter(ABC):
    """Interface for any trading venue (crypto exchange or stock broker)."""

    @abstractmethod
    def get_candles(self, market: str, interval: str, limit: int,
                    end_ms: int | None = None) -> list[Candle]: ...

    def get_candles_history(self, market: str, interval: str, total: int,
                            end_ms: int | None = None) -> list[Candle]:
        """Default: één get_candles-call; BitvavoClient overridet met paginatie."""
        return self.get_candles(market, interval, total, end_ms)

    @abstractmethod
    def get_price(self, market: str) -> float: ...

    @abstractmethod
    def get_balances(self) -> dict[str, float]: ...

    @abstractmethod
    def place_market_order(self, market: str, side: str, amount_quote: float) -> OrderResult: ...

    @abstractmethod
    def get_fees_pct(self) -> tuple[float, float]:
        """Return (maker_pct, taker_pct)."""


class BitvavoClient(ExchangeAdapter):
    def __init__(self, api_key: str = "", api_secret: str = "",
                 fallback_maker: float = 0.15, fallback_taker: float = 0.25):
        self.api_key = api_key
        self.api_secret = api_secret
        self._fallback_fees = (fallback_maker, fallback_taker)
        self._client = httpx.Client(base_url=BITVAVO_URL, timeout=15)
        self._rate_remaining = 1000
        self._markets: dict[str, tuple[float, MarketInfo]] = {}

    # --- internals -------------------------------------------------------
    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method}/v2{path}{body}"
        return hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, body: dict | None = None,
                 auth: bool = False) -> dict | list:
        if self._rate_remaining < 50:
            log.warning("Bitvavo rate limit low (%s), sleeping 60s", self._rate_remaining)
            time.sleep(60)
        headers = {}
        body_str = json.dumps(body) if body else ""
        if auth:
            ts = str(int(time.time() * 1000))
            headers = {
                "bitvavo-access-key": self.api_key,
                "bitvavo-access-signature": self._sign(ts, method, path, body_str),
                "bitvavo-access-timestamp": ts,
                "bitvavo-access-window": "10000",
            }
        resp = self._client.request(method, path, content=body_str or None, headers={
            **headers, **({"content-type": "application/json"} if body_str else {})})
        self._rate_remaining = int(resp.headers.get("bitvavo-ratelimit-remaining",
                                                    self._rate_remaining))
        if resp.is_error:
            raise BitvavoApiError(resp)
        return resp.json()

    # --- public data ------------------------------------------------------
    def get_candles(self, market: str, interval: str, limit: int,
                    end_ms: int | None = None) -> list[Candle]:
        pad = f"/{market}/candles?interval={interval}&limit={limit}"
        if end_ms is not None:
            pad += f"&end={end_ms}"
        raw = self._request("GET", pad)
        # Bitvavo returns newest first; we want oldest first for indicator math.
        candles = [Candle(int(c[0]), float(c[1]), float(c[2]), float(c[3]),
                          float(c[4]), float(c[5])) for c in raw]
        return sorted(candles, key=lambda c: c.ts)

    def get_candles_history(self, market: str, interval: str, total: int,
                            end_ms: int | None = None) -> list[Candle]:
        """Haalt meer dan 1440 candles op via paginatie met de end-parameter.

        `end_ms` pint het EINDE van het venster, zodat twee runs op verschillende
        momenten exact dezelfde candles zien.
        """
        out: list[Candle] = []
        while len(out) < total:
            batch = min(1440, total - len(out))
            path = f"/{market}/candles?interval={interval}&limit={batch}"
            if end_ms is not None:
                path += f"&end={end_ms}"
            raw = self._request("GET", path)
            if not raw:
                break
            chunk = sorted((Candle(int(c[0]), float(c[1]), float(c[2]), float(c[3]),
                                   float(c[4]), float(c[5])) for c in raw),
                           key=lambda c: c.ts)
            out = chunk + out
            end_ms = chunk[0].ts - 1
            if len(raw) < batch:
                break
        return out[-total:]

    def get_price(self, market: str) -> float:
        raw = self._request("GET", f"/ticker/price?market={market}")
        return float(raw["price"])

    def get_ticker_24h(self) -> list[dict]:
        """Alle markten in één call: volume, bid, ask (weight 25)."""
        return self._request("GET", "/ticker/24h")

    def get_book_ticker(self, market: str) -> dict:
        """Beste bied/laat: {'bid': str, 'ask': str, ...}."""
        return self._request("GET", f"/ticker/book?market={market}")

    MARKET_INFO_TTL_S = 6 * 3600

    def get_market_info(self, market: str) -> MarketInfo:
        """Handelsregels van één markt, zes uur gecachet (weight 1 per call)."""
        hit = self._markets.get(market)
        if hit and time.monotonic() - hit[0] < self.MARKET_INFO_TTL_S:
            return hit[1]
        raw = self._request("GET", f"/markets?market={market}")
        if isinstance(raw, list):
            raw = raw[0]
        info = MarketInfo.from_api(raw)
        self._markets[market] = (time.monotonic(), info)
        return info

    def create_order(self, body: dict) -> dict:
        """POST /order met `operatorId`. De aanroeper zet `clientOrderId` (een
        UUID), zodat een order na een timeout terug te vinden is (N4)."""
        return self._request("POST", "/order", body={**body, "operatorId": OPERATOR_ID},
                             auth=True)

    def place_limit_order(self, market: str, side: str, amount: str, price: str,
                          post_only: bool = True, client_order_id: str | None = None) -> dict:
        body = {"market": market, "side": side, "orderType": "limit",
                "amount": amount, "price": price, "postOnly": post_only}
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self.create_order(body)

    @staticmethod
    def _order_query(market: str, order_id: str | None, client_order_id: str | None) -> str:
        if client_order_id:
            return f"market={market}&clientOrderId={client_order_id}"
        if order_id:
            return f"market={market}&orderId={order_id}"
        raise ValueError("orderId of clientOrderId vereist")

    def get_order(self, market: str, order_id: str | None = None,
                  client_order_id: str | None = None) -> dict:
        q = self._order_query(market, order_id, client_order_id)
        return self._request("GET", f"/order?{q}", auth=True)

    def cancel_order(self, market: str, order_id: str | None = None,
                     client_order_id: str | None = None) -> dict:
        # operatorId is verplicht op cancel; tot v0.25.0 ontbrak hij hier, dus
        # elke annulering van een live entry zou door Bitvavo geweigerd zijn.
        q = self._order_query(market, order_id, client_order_id)
        return self._request("DELETE", f"/order?{q}&operatorId={OPERATOR_ID}", auth=True)

    def get_open_orders(self, market: str | None = None) -> list[dict]:
        path = "/ordersOpen" + (f"?market={market}" if market else "")
        return self._request("GET", path, auth=True)

    # --- authenticated -----------------------------------------------------
    def get_balances(self, available_only: bool = False) -> dict[str, float]:
        """Saldo per asset. Standaard available + inOrder (wat je bezit);
        `available_only` voor wat je nu kunt uitgeven (N8: EUR in een open
        buy-order is wel van jou maar niet besteedbaar)."""
        raw = self._request("GET", "/balance", auth=True)
        if available_only:
            return {b["symbol"]: float(b["available"]) for b in raw}
        return {b["symbol"]: float(b["available"]) + float(b.get("inOrder", 0)) for b in raw}

    def get_fees_pct(self) -> tuple[float, float]:
        try:
            raw = self._request("GET", "/account", auth=True)
            fees = raw.get("fees", {})
            return float(fees["maker"]) * 100, float(fees["taker"]) * 100
        except Exception:  # noqa: BLE001 - fallback is safe and logged
            log.warning("Could not fetch account fees, using config fallback")
            return self._fallback_fees

    def place_market_order(self, market: str, side: str, amount_quote: float) -> OrderResult:
        """Legacy-pad voor de ExchangeAdapter-interface. `LiveBroker` gebruikt
        sinds v0.25.0 `create_order` met afronding en clientOrderId."""
        body = {
            "market": market,
            "side": side,
            "orderType": "market",
        }
        if side == "buy":
            body["amountQuote"] = f"{amount_quote:.2f}"
        else:
            body["amount"] = f"{amount_quote:.8f}"  # for sell: base amount
        raw = self.create_order(body)
        fills = raw.get("fills", [])
        filled = sum(float(f["amount"]) for f in fills) or float(raw.get("filledAmount", 0))
        avg = (sum(float(f["amount"]) * float(f["price"]) for f in fills) / filled) \
            if filled else 0.0
        fee = sum(float(f.get("fee", 0)) for f in fills)
        return OrderResult(raw["orderId"], market, side, filled, avg, fee, raw)
