"""Tests voor de markt-brede regime-gate: de pure filterfunctie en de
shadow-uitkomstmeting. Deterministisch, zonder DB of netwerk (injectie)."""
from types import SimpleNamespace

from tradebot.analysis import regime
from tradebot.decision import Decision, apply_regime_filter

STEP_MS = 4 * 3600 * 1000
START = 1_700_000_000_000


def make_cfg(binding=False, proxy="BTC-EUR"):
    return SimpleNamespace(
        strategy={"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "rsi_oversold": 35,
                  "rsi_overbought": 70, "atr_period": 14, "min_signal_score": 3},
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        decision={"atr_stop_multiplier": 2.0, "reward_risk_ratio": 1.5,
                  "min_profit_pct": 0.50},
        risk={"paper_start_eur": 1000.0, "max_position_pct": 25.0},
        schedule={"candle_interval": "4h", "candle_limit": 200},
        regime={"enabled": True, "proxy_market": proxy, "binding": binding},
    )


def buy_decision():
    return Decision("ETH-EUR", "buy", "uptrend: EMA fast > slow",
                    amount_quote_eur=250.0, stop_loss=1.0, take_profit=2.0,
                    details={"score": 3})


# --- pure filter -----------------------------------------------------------

def test_regime_ok_leaves_buy_untouched():
    d = buy_decision()
    out = apply_regime_filter(d, regime_ok=True, proxy_market="BTC-EUR", binding=True)
    assert out.action == "buy"
    assert "SHADOW-REGIME" not in out.reason


def test_regime_down_binding_blocks():
    out = apply_regime_filter(buy_decision(), regime_ok=False,
                              proxy_market="BTC-EUR", binding=True)
    assert out.action == "skip"
    assert "regime gate" in out.reason
    assert "BTC-EUR" in out.reason


def test_regime_down_shadow_keeps_buy_annotated():
    out = apply_regime_filter(buy_decision(), regime_ok=False,
                              proxy_market="BTC-EUR", binding=False)
    assert out.action == "buy"
    assert out.amount_quote_eur == 250.0
    assert "SHADOW-REGIME genegeerd" in out.reason
    assert out.details["shadow_regime"].startswith("regime gate")
    assert out.details["score"] == 3  # oorspronkelijke details blijven


def test_non_buy_is_untouched():
    d = Decision("ETH-EUR", "skip", "no signal (score 0)")
    out = apply_regime_filter(d, regime_ok=False, proxy_market="BTC-EUR", binding=True)
    assert out.action == "skip"
    assert out.reason == "no signal (score 0)"


# --- meting ----------------------------------------------------------------

def _trade(ts, market, side, amount, price, pnl=0.0, reason=""):
    return {"ts": ts, "market": market, "side": side, "amount": amount,
            "price": price, "pnl_eur": pnl, "reason": reason}


def test_analyze_regime_net_gate():
    # MKT-A: regime-down entry die verlies maakte (gate voorkwam verlies).
    # MKT-B: regime-down entry die winst maakte (gate sneed winst weg).
    events = [
        {"ts": START, "market": "MKT-A"},
        {"ts": START + 10 * STEP_MS, "market": "MKT-B"},
    ]
    trades = [
        _trade(START, "MKT-A", "buy", 1.0, 100.0),
        _trade(START + STEP_MS, "MKT-A", "sell", 1.0, 90.0, pnl=-10.0, reason="stop loss"),
        _trade(START + 10 * STEP_MS, "MKT-B", "buy", 1.0, 100.0),
        _trade(START + 11 * STEP_MS, "MKT-B", "sell", 1.0, 105.0, pnl=5.0, reason="take profit"),
    ]
    d = regime.analyze_regime(make_cfg(), events=events, trades=trades)

    assert d["n_events"] == 2
    assert d["n_resolved"] == 2
    assert d["n_unresolved"] == 0
    assert d["position_size_eur"] == 250.0
    assert d["proxy_market"] == "BTC-EUR"
    assert d["binding"] is False
    s = d["summary"]
    # -10% van €250 = €25 vermeden; +5% van €250 = €12,50 gemist; netto €12,50.
    assert s["avoided_eur"] == 25.0
    assert s["missed_eur"] == 12.5
    assert s["net_gate_eur"] == 12.5
    assert s["veto_precision_pct"] == 50.0
    assert {r["group"] for r in d["per_market"]} == {"MKT-A", "MKT-B"}


def test_analyze_regime_unresolved_event():
    # Event zonder bijbehorende trade blijft onafgewikkeld.
    events = [{"ts": START, "market": "MKT-C"}]
    d = regime.analyze_regime(make_cfg(), events=events, trades=[])
    assert d["n_events"] == 1
    assert d["n_resolved"] == 0
    assert d["n_unresolved"] == 1
    assert d["summary"] is None


def test_analyze_regime_empty():
    d = regime.analyze_regime(make_cfg(), events=[], trades=[])
    assert d["n_events"] == 0
    assert d["n_resolved"] == 0
    assert d["summary"] is None
    assert d["per_market"] == []


def test_analyze_regime_binding_flag_passthrough():
    d = regime.analyze_regime(make_cfg(binding=True), events=[], trades=[])
    assert d["binding"] is True
