"""Tests voor de counterfactual veto-analyse.

De rekenkern (exit-modellen, aggregatie, richting-check) wordt deterministisch
getest met fixture-candles, zonder DB of netwerk. Eén los gemarkeerde test
(`live`) raakt de echte Bitvavo-API en slaat over zonder verbinding, zodat CI
niet afhankelijk wordt van externe uptime.
"""
from types import SimpleNamespace

import pytest

from tradebot.analysis import veto
from tradebot.config import config_fingerprint
from tradebot.exchange import Candle

STRATEGY = {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "rsi_oversold": 35,
            "rsi_overbought": 70, "atr_period": 14, "min_signal_score": 3}

STEP_MS = 4 * 3600 * 1000  # 4h


def make_cfg():
    return SimpleNamespace(
        strategy=STRATEGY,
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        decision={"atr_stop_multiplier": 2.0, "reward_risk_ratio": 2.0,
                  "min_profit_pct": 0.50},
        risk={"paper_start_eur": 1000.0, "max_position_pct": 25.0},
        schedule={"candle_interval": "4h", "candle_limit": 200},
    )


def candles(closes, spread=1.0, start_ms=1_700_000_000_000):
    """Bouw een candle-reeks; high/low rond close zodat ATR > 0 is."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(ts=start_ms + i * STEP_MS, open=prev,
                          high=max(prev, c) + spread / 2,
                          low=min(prev, c) - spread / 2, close=c, volume=1.0))
        prev = c
    return out


def default_params():
    return veto.params_from_config(make_cfg())


# --- helpers ---------------------------------------------------------------

def test_interval_seconds():
    assert veto.interval_seconds("4h") == 14400
    assert veto.interval_seconds("15m") == 900
    assert veto.interval_seconds("1d") == 86400
    with pytest.raises(ValueError):
        veto.interval_seconds("4x")


def test_params_from_config():
    p = default_params()
    # round-trip = 2*0.25 + 0.10 = 0.60 %
    assert p.cost_pct == pytest.approx(0.60)
    # positie = 1000 * 25% = 250
    assert p.position_size_eur == pytest.approx(250.0)


def test_suspect_detection():
    assert veto._is_suspect("Price near lower Bollinger band suggests overextension")
    assert veto._is_suspect("koers bij de onderband")
    assert not veto._is_suspect("Overbought RSI (72) suggests correction")


# --- exit-modellen (deterministisch) ---------------------------------------

def test_fixed_horizon_missed_profit():
    p = default_params()
    cs = candles([100.0] * 3 + [100, 102, 104, 106, 108, 110, 112])
    # entry op index 5 (close 104), 6 candles later = index 11 (close 112)
    net = veto._fixed_horizon(cs, idx=5, entry=104.0, p=p)
    assert net == pytest.approx(112 / 104 * 100 - 100 - 0.60, abs=1e-6)
    assert net > 0  # veto sneed winst weg


def test_fixed_horizon_avoided_loss():
    p = default_params()
    cs = candles([100, 100, 100, 100, 100, 100, 95, 90, 88, 86, 84, 82])
    net = veto._fixed_horizon(cs, idx=5, entry=100.0, p=p)
    assert net < 0  # veto voorkwam verlies


def test_tpsl_hits_target():
    p = default_params()
    # entry 100, atr 1 -> stop 98, target 104. Forward high raakt 104 eerst.
    cs = candles([100, 100, 101, 103, 105], spread=0.5)
    net, reason = veto._tp_sl(cs, idx=1, entry=100.0, atr=1.0, p=p)
    assert reason == "target"
    assert net == pytest.approx(4.0 - 0.60, abs=1e-6)


def test_tpsl_hits_stop():
    p = default_params()
    # entry 100, atr 1 -> stop 98. Forward low raakt 98.
    cs = candles([100, 100, 99, 97, 96], spread=0.5)
    net, reason = veto._tp_sl(cs, idx=1, entry=100.0, atr=1.0, p=p)
    assert reason == "stop"
    assert net == pytest.approx(-2.0 - 0.60, abs=1e-6)


def test_tpsl_same_candle_conservative_stop():
    p = default_params()
    # candle raakt zowel stop (98) als target (104): conservatief = stop.
    cs = [Candle(ts=i * STEP_MS, open=100, high=105, low=97, close=100, volume=1.0)
          for i in range(3)]
    net, reason = veto._tp_sl(cs, idx=0, entry=100.0, atr=1.0, p=p)
    assert reason == "stop"


def test_tpsl_timeout_closes_on_last():
    p = default_params()
    p.tpsl_max_candles = 2
    cs = candles([100, 100.5, 101.0], spread=0.2)  # raakt stop noch target
    net, reason = veto._tp_sl(cs, idx=0, entry=100.0, atr=1.0, p=p)
    assert reason == "timeout"


# --- integratie: evaluate_vetos + summarize --------------------------------

def test_evaluate_and_summarize_rising_market():
    p = default_params()
    closes = [100.0] * 70 + [102, 104, 106, 108, 110, 112, 114]
    cs = candles(closes)
    entry_ts = cs[70].ts  # eerste candle na warmup
    vetos = [{"ts": entry_ts, "market": "BTC-EUR", "confidence": 0.8,
              "reasoning": "Price near lower Bollinger band suggests overextension"}]
    outcomes, skipped = veto.evaluate_vetos(vetos, {"BTC-EUR": cs}, STRATEGY, p)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.net_fixed_pct > 0        # stijgende markt: veto miste winst
    assert o.suspect_reason is True
    summary = veto.summarize(outcomes, skipped, p)
    assert summary["n_vetos"] == 1
    assert summary["suspect_reason_count"] == 1
    assert summary["fixed_horizon"]["n_missed"] == 1
    assert summary["by_market"][0]["group"] == "BTC-EUR"


def test_entry_before_warmup_is_skipped():
    p = default_params()
    cs = candles([100.0] * 65)
    vetos = [{"ts": cs[10].ts, "market": "BTC-EUR", "confidence": 0.7,
              "reasoning": "x"}]
    outcomes, skipped = veto.evaluate_vetos(vetos, {"BTC-EUR": cs}, STRATEGY, p)
    assert outcomes == []
    assert skipped.get("geen_candle_op_ts_of_te_vroeg") == 1


def test_unknown_market_is_skipped():
    p = default_params()
    cs = candles([100.0] * 70)
    vetos = [{"ts": cs[65].ts, "market": "DOGE-EUR", "confidence": 0.7,
              "reasoning": "x"}]
    outcomes, skipped = veto.evaluate_vetos(vetos, {"BTC-EUR": cs}, STRATEGY, p)
    assert outcomes == []
    assert skipped.get("geen_candles_voor_markt") == 1


def test_analyze_vetos_no_vetos_returns_empty_summary():
    cfg = make_cfg()
    result = veto.analyze_vetos(adapter=None, cfg=cfg, vetos=[])
    assert result["n_vetos"] == 0
    assert result["skipped"] == {"geen_vetos": 1}


def test_analyze_vetos_with_injected_candles():
    cfg = make_cfg()
    closes = [100.0] * 70 + [98, 96, 94, 92, 90, 88, 86]
    cs = candles(closes)
    vetos = [{"ts": cs[70].ts, "market": "BTC-EUR", "confidence": 0.6,
              "reasoning": "Overbought RSI"}]
    result = veto.analyze_vetos(adapter=None, cfg=cfg, vetos=vetos,
                                candles_by_market={"BTC-EUR": cs})
    assert result["n_vetos"] == 1
    assert result["fixed_horizon"]["n_avoided"] == 1  # dalende markt: verlies vermeden


# --- config-fingerprint ----------------------------------------------------

def test_config_fingerprint_stable_and_sensitive():
    a = make_cfg()
    b = make_cfg()
    assert config_fingerprint(a) == config_fingerprint(b)  # zelfde config -> zelfde hash
    b.decision = {**b.decision, "atr_stop_multiplier": 3.0}
    assert config_fingerprint(a) != config_fingerprint(b)  # gewijzigde config -> andere hash
    assert len(config_fingerprint(a)) == 12


def test_config_scope_filter_db(memory_db):
    from tradebot.db import LLMCallRow, session
    with session() as s:
        s.add_all([
            LLMCallRow(provider="groq", model="m", market="BTC-EUR", verdict="veto",
                       confidence=0.7, reasoning="oud", config_hash="OUD"),
            LLMCallRow(provider="groq", model="m", market="ETH-EUR", verdict="veto",
                       confidence=0.7, reasoning="nieuw", config_hash="NIEUW"),
            LLMCallRow(provider="groq", model="m", market="ETH-EUR", verdict="agree",
                       confidence=0.9, reasoning="geen veto", config_hash="NIEUW"),
        ])
        s.commit()
    assert len(veto._load_vetos_from_db()) == 2               # alle vetoes
    scoped = veto._load_vetos_from_db("NIEUW")
    assert len(scoped) == 1 and scoped[0]["market"] == "ETH-EUR"


# --- categorisatie ---------------------------------------------------------

def test_categorize():
    assert veto._categorize("Price near lower Bollinger band") == "mean-reversion"
    assert veto._categorize("Overbought RSI suggests reversal") == "mean-reversion"
    assert veto._categorize("Spread too wide, thin liquidity") == "liquiditeit/spread"
    assert veto._categorize("Very volatile, high ATR whipsaw") == "volatiliteit"
    assert veto._categorize("Stale data, gap in data feed") == "data-integriteit"
    assert veto._categorize("Ik vind het geen goed idee") == "overig"


# --- Wilson-marge ----------------------------------------------------------

def test_wilson_margin_shrinks_with_n():
    wide = veto._wilson_half_width(3, 5)
    narrow = veto._wilson_half_width(30, 50)   # zelfde ratio, groter n
    assert wide > narrow
    assert veto._wilson_half_width(0, 0) == 0.0
    assert 0.0 <= narrow <= 100.0


# --- echte shadow-uitkomst -------------------------------------------------

def test_build_roundtrips_pairs_buy_and_sell():
    trades = [
        {"ts": 1000, "market": "BTC-EUR", "side": "buy", "amount": 1.0, "price": 100.0,
         "pnl_eur": 0.0, "reason": "koop"},
        {"ts": 2000, "market": "BTC-EUR", "side": "sell", "amount": 1.0, "price": 108.0,
         "pnl_eur": 8.0, "reason": "guard: take profit geraakt"},
    ]
    rts = veto.build_roundtrips(trades)
    assert len(rts) == 1
    assert rts[0].net_pct == pytest.approx(8.0)   # 8 / (1*100) * 100
    assert rts[0].exit == "target"


def test_match_real_outcome_window():
    rt = veto.RoundTrip("BTC-EUR", buy_ms=1500, sell_ms=3000, net_pct=-2.0, exit="stop")
    assert veto.match_real_outcome(1000, "BTC-EUR", [rt], window_ms=1000) is rt
    assert veto.match_real_outcome(1000, "BTC-EUR", [rt], window_ms=100) is None  # buiten venster
    assert veto.match_real_outcome(2000, "BTC-EUR", [rt], window_ms=1000) is None  # buy voor veto


def test_analyze_with_real_outcome_side_by_side():
    cfg = make_cfg()
    closes = [100.0] * 70 + [102, 104, 106, 108, 110, 112, 114]
    cs = candles(closes)
    veto_ts = cs[70].ts
    vetos = [{"ts": veto_ts, "market": "BTC-EUR", "confidence": 0.7,
              "reasoning": "Price near lower Bollinger band"}]
    trades = [
        {"ts": veto_ts, "market": "BTC-EUR", "side": "buy", "amount": 1.0, "price": 100.0,
         "pnl_eur": 0.0, "reason": "koop"},
        {"ts": veto_ts + STEP_MS, "market": "BTC-EUR", "side": "sell", "amount": 1.0,
         "price": 106.0, "pnl_eur": 6.0, "reason": "guard: take profit geraakt"},
    ]
    result = veto.analyze_vetos(adapter=None, cfg=cfg, vetos=vetos,
                                candles_by_market={"BTC-EUR": cs}, trades=trades,
                                config_hash="NIEUW")
    assert result["config_hash"] == "NIEUW"
    assert result["n_real_matched"] == 1
    assert result["real_outcome"]["n"] == 1
    assert result["real_outcome"]["n_missed"] == 1        # trade won -> veto sneed winst weg
    assert result["fixed_horizon"]["precision_margin_pp"] >= 0.0
    reasons = {r["group"] for r in result["by_reason"]}
    assert "mean-reversion" in reasons


# --- live (netwerk) --------------------------------------------------------

@pytest.mark.live
def test_live_bitvavo_fetch_and_analyze():
    from tradebot.exchange import BitvavoClient
    feed = BitvavoClient()
    try:
        real = feed.get_candles("BTC-EUR", "4h", 120)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"geen Bitvavo-verbinding: {exc}")
    if len(real) < 70:
        pytest.skip("te weinig candles van Bitvavo")
    cfg = make_cfg()
    veto_ts = real[-10].ts  # recent, met forward-candles beschikbaar
    vetos = [{"ts": veto_ts, "market": "BTC-EUR", "confidence": 0.8,
              "reasoning": "Price near lower Bollinger band"}]
    result = veto.analyze_vetos(feed, cfg, vetos=vetos)
    assert result["n_vetos"] == 1
    assert result["fixed_horizon"] is not None
