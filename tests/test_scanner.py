from tradebot.scanner import liquidity_filter


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
