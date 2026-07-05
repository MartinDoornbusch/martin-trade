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
    def get_candles(self, market: str, interval: str, limit: int) -> list[Candle]: ...

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
        resp.raise_for_status()
        return resp.json()

    # --- public data ------------------------------------------------------
    def get_candles(self, market: str, interval: str, limit: int) -> list[Candle]:
        raw = self._request("GET", f"/{market}/candles?interval={interval}&limit={limit}")
        # Bitvavo returns newest first; we want oldest first for indicator math.
        candles = [Candle(int(c[0]), float(c[1]), float(c[2]), float(c[3]),
                          float(c[4]), float(c[5])) for c in raw]
        return sorted(candles, key=lambda c: c.ts)

    def get_price(self, market: str) -> float:
        raw = self._request("GET", f"/ticker/price?market={market}")
        return float(raw["price"])

    # --- authenticated -----------------------------------------------------
    def get_balances(self) -> dict[str, float]:
        raw = self._request("GET", "/balance", auth=True)
        return {b["symbol"]: float(b["available"]) for b in raw}

    def get_fees_pct(self) -> tuple[float, float]:
        try:
            raw = self._request("GET", "/account", auth=True)
            fees = raw.get("fees", {})
            return float(fees["maker"]) * 100, float(fees["taker"]) * 100
        except Exception:  # noqa: BLE001 - fallback is safe and logged
            log.warning("Could not fetch account fees, using config fallback")
            return self._fallback_fees

    def place_market_order(self, market: str, side: str, amount_quote: float) -> OrderResult:
        body = {
            "market": market,
            "side": side,
            "orderType": "market",
            "operatorId": OPERATOR_ID,
        }
        if side == "buy":
            body["amountQuote"] = f"{amount_quote:.2f}"
        else:
            body["amount"] = f"{amount_quote:.8f}"  # for sell: base amount
        raw = self._request("POST", "/order", body=body, auth=True)
        fills = raw.get("fills", [])
        filled = sum(float(f["amount"]) for f in fills) or float(raw.get("filledAmount", 0))
        avg = (sum(float(f["amount"]) * float(f["price"]) for f in fills) / filled) \
            if filled else 0.0
        fee = sum(float(f.get("fee", 0)) for f in fills)
        return OrderResult(raw["orderId"], market, side, filled, avg, fee, raw)
