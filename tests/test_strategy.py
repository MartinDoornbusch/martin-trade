from datetime import datetime, timedelta, timezone

from tradebot.exchange import Candle
from tradebot.strategy import (
    MarketSnapshot,
    breakeven_stop_hit,
    check_exit,
    drop_unclosed,
    evaluate_buy,
    time_stop_hit,
)

CFG = {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "rsi_buy_zone_min": 25,
       "rsi_buy_zone_max": 45, "rsi_overbought": 70, "atr_period": 14,
       "min_signal_score": 3}

OPENED = datetime(2026, 8, 1, tzinfo=timezone.utc)


def candles_after_entry(highs: list[float]) -> list[Candle]:
    """Candles die ná de entry liggen, met instelbare highs."""
    base_ms = int(OPENED.timestamp() * 1000)
    step = int(timedelta(hours=4).total_seconds() * 1000)
    return [Candle(ts=base_ms + (i + 1) * step, open=100.0, high=h, low=99.0,
                   close=h, volume=1.0) for i, h in enumerate(highs)]


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


# --- 1.2: trend-break geschrapt -------------------------------------------------

def test_no_trend_break_exit_left():
    """Regressie op bug 1.2: de oude regel (EMA-cross-down + RSI > 70) is geschrapt.

    Deze combinatie moet nu binnen SL/TP gewoon blijven zitten; de exit-verantwoor-
    delijkheid ligt bij SL/TP, time-stop en breakeven-stop.
    """
    hit, why = check_exit(100, 95, 110, snap(price=105.0, ema_fast=99.0,
                                             ema_slow=101.0, rsi=85.0))
    assert not hit and why == ""


# --- 1.2: breakeven-stop --------------------------------------------------------

def test_breakeven_stop_fires_after_peak_and_giveback():
    """Piek stond 2x ATR in de winst, koers zakt terug tot onder entry+offset."""
    hit, why = breakeven_stop_hit(candles_after_entry([101.0, 102.0]), OPENED,
                                  entry_price=100.0, current_price=100.4,
                                  atr_value=1.0, trigger_atr=1.0, offset_pct=0.55)
    assert hit and "breakeven-stop" in why


def test_breakeven_stop_not_armed_without_enough_profit():
    """Winst bleef binnen de ruis (< 1x ATR): positie houdt zijn volle ATR-stop."""
    hit, _ = breakeven_stop_hit(candles_after_entry([100.5, 100.8]), OPENED,
                                entry_price=100.0, current_price=100.4,
                                atr_value=1.0, trigger_atr=1.0, offset_pct=0.55)
    assert not hit


def test_breakeven_stop_leaves_winner_running():
    """Bewapend, maar de koers staat nog ruim boven de drempel: niet knippen."""
    hit, _ = breakeven_stop_hit(candles_after_entry([103.0]), OPENED,
                                entry_price=100.0, current_price=102.5,
                                atr_value=1.0, trigger_atr=1.0, offset_pct=0.55)
    assert not hit


def test_breakeven_stop_ignores_candles_before_entry():
    """Alleen candles ná entry tellen mee voor de piek."""
    before = [Candle(ts=int(OPENED.timestamp() * 1000) - 1000, open=100.0, high=120.0,
                     low=99.0, close=100.0, volume=1.0)]
    hit, _ = breakeven_stop_hit(before, OPENED, entry_price=100.0, current_price=100.4,
                                atr_value=1.0, trigger_atr=1.0, offset_pct=0.55)
    assert not hit


def test_breakeven_stop_disabled_by_zero_trigger():
    hit, _ = breakeven_stop_hit(candles_after_entry([110.0]), OPENED, 100.0, 100.0,
                                atr_value=1.0, trigger_atr=0.0, offset_pct=0.55)
    assert not hit


# --- 1.3: expliciete RSI-koopzone ----------------------------------------------

def test_rsi_buy_zone_bounds_come_from_config():
    """Regressie op bug 1.3: de zone stond hardcoded als `rsi_oversold + 10` en 25."""
    cfg = {**CFG, "rsi_buy_zone_min": 30, "rsi_buy_zone_max": 40}
    inside = evaluate_buy(snap(rsi=35.0), cfg)
    outside = evaluate_buy(snap(rsi=44.0), cfg)
    assert any("buy zone" in r for r in inside.reasons)
    assert not any("buy zone" in r for r in outside.reasons)
    assert inside.score == outside.score + 1


def test_rsi_buy_zone_defaults_match_old_behaviour():
    """Zonder de nieuwe sleutels blijft de effectieve zone 25-45 (oud gedrag)."""
    legacy = {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "rsi_oversold": 35,
              "rsi_overbought": 70, "atr_period": 14, "min_signal_score": 3}
    assert any("buy zone" in r for r in evaluate_buy(snap(rsi=44.0), legacy).reasons)
    assert not any("buy zone" in r for r in evaluate_buy(snap(rsi=46.0), legacy).reasons)
    assert not any("buy zone" in r for r in evaluate_buy(snap(rsi=24.0), legacy).reasons)


# --- 1.1: lopende candle afknippen ---------------------------------------------

STEP_MS = 4 * 3600 * 1000


def series(n: int, last_ts: int) -> list[Candle]:
    return [Candle(ts=last_ts - (n - 1 - i) * STEP_MS, open=100.0, high=101.0,
                   low=99.0, close=100.0, volume=1.0) for i in range(n)]


def test_drop_unclosed_removes_the_running_candle():
    """Regressie op punt 1.1: de nieuwste 4h-candle van Bitvavo opent op het hele
    uur en sluit pas vier uur later; die bar beweegt dus nog."""
    last_ts = 1_785_945_600_000                    # candle opent, loopt nog
    now = last_ts + 2 * 3600 * 1000                # twee uur later
    candles = series(10, last_ts)
    out = drop_unclosed(candles, now_ms=now)
    assert len(out) == 9
    assert out[-1].ts == last_ts - STEP_MS


def test_drop_unclosed_keeps_a_series_that_is_already_closed():
    """Blind `[:-1]` zou hier een geldige bar weggooien en de bot een candle laten
    achterlopen; de functie moet de reeks ongemoeid laten."""
    last_ts = 1_785_945_600_000
    now = last_ts + STEP_MS + 1                    # bar is net gesloten
    candles = series(10, last_ts)
    assert len(drop_unclosed(candles, now_ms=now)) == 10


def test_drop_unclosed_handles_degenerate_input():
    assert drop_unclosed([]) == []
    one = series(1, 1_785_945_600_000)
    assert len(drop_unclosed(one, now_ms=1_785_945_600_000)) == 1



# --- 1.2: time-stop telt alleen afgesloten candles ------------------------------

def candles_from_entry(n: int) -> list[Candle]:
    """`n` candles ná OPENED; de laatste is de nog lopende bar."""
    base = int(OPENED.timestamp() * 1000)
    return [Candle(ts=base + (i + 1) * STEP_MS, open=100.0, high=100.0,
                   low=100.0, close=100.0, volume=1.0) for i in range(n)]


def _now_with_running_bar(candles: list[Candle]) -> int:
    """Klok halverwege de laatste candle: die bar loopt dus nog."""
    return candles[-1].ts + STEP_MS // 2


def test_time_stop_does_not_count_the_running_candle():
    """Regressie op punt 1.2: `elapsed` telde elke candle met `ts > opened_ms`,
    inclusief de bar die nog liep. Daardoor vuurde de time-stop ongeveer een candle
    te vroeg. Hier zijn er 12 candles ná entry, waarvan er één nog loopt: 11
    verstreken bars, dus onder de drempel van 12."""
    candles = candles_from_entry(12)
    hit, _ = time_stop_hit(candles, OPENED, entry_price=100.0, current_price=100.0,
                           round_trip_pct=0.5, n_candles=12,
                           now_ms=_now_with_running_bar(candles))
    assert not hit


def test_time_stop_fires_once_enough_candles_actually_closed():
    """Eén candle later zijn er wél 12 afgesloten bars: dan mag hij vuren."""
    candles = candles_from_entry(13)
    hit, why = time_stop_hit(candles, OPENED, entry_price=100.0, current_price=100.0,
                             round_trip_pct=0.5, n_candles=12,
                             now_ms=_now_with_running_bar(candles))
    assert hit and "12 candles" in why


def test_time_stop_leaves_a_winner_above_the_threshold_alone():
    candles = candles_from_entry(13)
    hit, _ = time_stop_hit(candles, OPENED, entry_price=100.0, current_price=105.0,
                           round_trip_pct=0.5, n_candles=12,
                           now_ms=_now_with_running_bar(candles))
    assert not hit


def test_breakeven_stop_still_counts_the_running_candle():
    """Tegenhanger van 1.2: de breakeven-stop MEET een piek in plaats van candles te
    tellen, dus een high die zojuist in de lopende bar is gezet telt terecht mee.
    Alleen die bar staat hier boven de trigger."""
    base = int(OPENED.timestamp() * 1000)
    candles = [Candle(ts=base + (i + 1) * STEP_MS, open=100.0, high=100.2,
                      low=99.0, close=100.0, volume=1.0) for i in range(3)]
    candles.append(Candle(ts=base + 4 * STEP_MS, open=100.0, high=102.0,
                          low=99.0, close=100.4, volume=1.0))
    hit, _ = breakeven_stop_hit(candles, OPENED, entry_price=100.0, current_price=100.4,
                                atr_value=1.0, trigger_atr=1.0, offset_pct=0.55)
    assert hit
