from tradebot.strategy import MarketSnapshot, check_exit, evaluate_buy

CFG = {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "rsi_oversold": 35,
       "rsi_overbought": 70, "atr_period": 14, "min_signal_score": 3}


def snap(**kw) -> MarketSnapshot:
    base = dict(market="BTC-EUR", price=100.0, ema_fast=101.0, ema_slow=100.0,
                rsi=40.0, macd_hist=0.5, macd_hist_prev=-0.1, atr=1.0,
                bb_lower=99.5, bb_mid=101.0, change_24c_pct=1.0)
    base.update(kw)
    return MarketSnapshot(**base)


def test_strong_confluence_generates_buy():
    c = evaluate_buy(snap(), CFG)
    # uptrend(1) + rsi zone(1) + macd flip(2) + near bb lower(1) = 5
    assert c.action == "buy"
    assert c.score >= 3


def test_weak_setup_holds():
    c = evaluate_buy(snap(ema_fast=99.0, macd_hist=-0.5, macd_hist_prev=-0.4,
                          rsi=55.0, bb_lower=90.0), CFG)
    assert c.action == "hold"


def test_exit_on_stop_loss():
    hit, why = check_exit(100, stop_loss=95, take_profit=110, snap=snap(price=94.0))
    assert hit and "stop loss" in why


def test_exit_on_take_profit():
    hit, why = check_exit(100, 95, 110, snap(price=111.0))
    assert hit and "take profit" in why


def test_no_exit_in_range():
    hit, _ = check_exit(100, 95, 110, snap(price=105.0))
    assert not hit
