from types import SimpleNamespace

from tradebot.exchange import Candle, ExchangeAdapter, OrderResult
from tradebot.scanner import liquidity_filter, scan, select_auto_fill


def ticker(market, volume, bid, ask):
    return {"market": market, "volumeQuote": str(volume), "bid": str(bid), "ask": str(ask)}


def test_filters_non_eur_markets():
    out = liquidity_filter([ticker("BTC-USDC", 1e9, 100, 100.1)])
    assert out == []


def test_filters_low_volume():
    out = liquidity_filter([ticker("ABC-EUR", 10_000, 100, 100.1)])
    assert out == []


def test_filters_wide_spread():
    # spread (101-100)/100.5 = ~1% > 0.60%
    out = liquidity_filter([ticker("ABC-EUR", 1_000_000, 100, 101)])
    assert out == []


def test_accepts_liquid_tight_market_and_sorts_by_volume():
    out = liquidity_filter([
        ticker("SOL-EUR", 2_000_000, 100, 100.2),
        ticker("BTC-EUR", 90_000_000, 50000, 50010),
    ])
    assert [r["market"] for r in out] == ["BTC-EUR", "SOL-EUR"]
    assert out[0]["spread_pct"] < 0.1


def test_handles_missing_or_invalid_fields():
    out = liquidity_filter([
        {"market": "X-EUR"},                                # geen velden
        ticker("Y-EUR", "abc", 1, 2) | {"volumeQuote": "abc"},  # onparseerbaar
        ticker("Z-EUR", 1_000_000, 100, 99),                # ask < bid
    ])
    assert out == []


# --- rookproef op de volledige scan ---------------------------------------------
#
# Aanleiding: bij de fee-model-wijziging van v0.20.0 kwam er een `get_secrets()`-
# aanroep in `scan()` zonder import. Geen enkele test raakte die regel, terwijl
# `scan()` sinds v0.17.0 in het koudepad van auto-fill zit en dus ELKE cyclus in
# productie draait. De unittests hierboven dekken alleen `liquidity_filter`.

STEP_MS = 4 * 3600 * 1000


class FakeScanFeed(ExchangeAdapter):
    def __init__(self, markten: list[str]):
        self.markten = markten

    def get_ticker_24h(self) -> list[dict]:
        return [{"market": m, "volumeQuote": "5000000", "bid": "100", "ask": "100.1"}
                for m in self.markten]

    def get_candles(self, market: str, interval: str, limit: int) -> list[Candle]:
        closes = [100.0 * (1.01 ** i) for i in range(80)]
        return [Candle(ts=1_700_000_000_000 + i * STEP_MS, open=c, high=c * 1.02,
                       low=c * 0.98, close=c, volume=1000.0)
                for i, c in enumerate(closes)]

    def get_price(self, market: str) -> float:
        return 100.0

    def get_balances(self) -> dict[str, float]:
        return {}

    def place_market_order(self, market, side, amount_quote) -> OrderResult:
        raise NotImplementedError

    def get_fees_pct(self):
        return 0.15, 0.25


def scan_cfg(**over) -> SimpleNamespace:
    base = dict(
        markets=["BTC-EUR"], watchlist=[], blocklist=[],
        schedule={"candle_interval": "4h", "candle_limit": 80},
        strategy={"ema_fast": 3, "ema_slow": 5, "rsi_period": 14, "atr_period": 14,
                  "rsi_buy_zone_min": 25, "rsi_buy_zone_max": 45,
                  "rsi_overbought": 70, "min_signal_score": 1},
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        decision={"min_profit_pct": 0.50, "atr_stop_multiplier": 2.0,
                  "reward_risk_ratio": 1.5},
        risk={}, universe={}, curation={}, llm={},
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_scan_runs_end_to_end(memory_db):
    """Rookproef die de hele functie doorloopt in plaats van alleen het filter.

    Zou zijn omgevallen op de ontbrekende `get_secrets`-import van v0.20.0, en vangt
    voortaan elke naam- of bedradingsfout in het pad dat elke cyclus draait.
    """
    feed = FakeScanFeed(["AAA-EUR", "BBB-EUR", "CCC-EUR"])
    resultaten, stats = scan(feed, scan_cfg(), top_n=2)

    assert stats["eur_markets"] == 3
    assert stats["liquid"] == 3
    assert len(resultaten) <= 2
    for r in resultaten:
        assert {"market", "score", "expected_move_pct", "required_pct", "fee_ok"} <= set(r)


def test_scan_honours_the_blocklist(memory_db):
    feed = FakeScanFeed(["AAA-EUR", "BBB-EUR"])
    resultaten, _ = scan(feed, scan_cfg(blocklist=["AAA-EUR"]), top_n=5)
    assert "AAA-EUR" not in {r["market"] for r in resultaten}


def test_scan_survives_one_broken_market(memory_db):
    """Eén markt zonder candles mag de scan niet breken; auto-fill draait hierop."""
    class HalfKapot(FakeScanFeed):
        def get_candles(self, market, interval, limit):
            if market == "BBB-EUR":
                raise RuntimeError("candles kapot")
            return super().get_candles(market, interval, limit)

    resultaten, _ = scan(HalfKapot(["AAA-EUR", "BBB-EUR"]), scan_cfg(), top_n=5)
    assert "BBB-EUR" not in {r["market"] for r in resultaten}


def test_select_auto_fill_skips_excluded_and_respects_the_limit():
    resultaten = [{"market": "AAA-EUR", "score": 3, "score_needed": 3, "fee_ok": True},
                  {"market": "BBB-EUR", "score": 3, "score_needed": 3, "fee_ok": True},
                  {"market": "CCC-EUR", "score": 1, "score_needed": 3, "fee_ok": True}]
    assert select_auto_fill(resultaten, {"AAA-EUR"}, 5) == ["BBB-EUR"]
    assert select_auto_fill(resultaten, set(), 1) == ["AAA-EUR"]
