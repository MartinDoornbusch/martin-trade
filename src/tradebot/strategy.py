"""Deterministic swing strategy: produces candidate signals from indicators.

The LLM never generates signals; it may only confirm or veto a BUY candidate.
Exits (stop/target/trend-break) are fully mechanical.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .exchange import Candle
from .indicators import atr, bollinger, ema, macd, rsi


@dataclass
class MarketSnapshot:
    market: str
    price: float
    ema_fast: float
    ema_slow: float
    rsi: float
    macd_hist: float
    macd_hist_prev: float
    atr: float
    bb_lower: float
    bb_mid: float
    change_24c_pct: float  # % change over last 6 candles (24h on 4h candles)


@dataclass
class Candidate:
    market: str
    action: str                      # "buy" | "hold"
    score: int
    reasons: list[str] = field(default_factory=list)
    snapshot: MarketSnapshot | None = None


def build_snapshot(market: str, candles: list[Candle], cfg: dict) -> MarketSnapshot:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    ef = ema(closes, int(cfg["ema_fast"]))
    es = ema(closes, int(cfg["ema_slow"]))
    r = rsi(closes, int(cfg["rsi_period"]))
    _, _, hist = macd(closes)
    a = atr(highs, lows, closes, int(cfg["atr_period"]))
    bb_mid, _, bb_low = bollinger(closes)
    return MarketSnapshot(
        market=market,
        price=closes[-1],
        ema_fast=float(ef[-1]),
        ema_slow=float(es[-1]),
        rsi=float(r[-1]),
        macd_hist=float(hist[-1]),
        macd_hist_prev=float(hist[-2]),
        atr=float(a[-1]),
        bb_lower=float(bb_low[-1]),
        bb_mid=float(bb_mid[-1]),
        change_24c_pct=(closes[-1] / closes[-7] - 1) * 100 if len(closes) >= 7 else 0.0,
    )


def evaluate_buy(snap: MarketSnapshot, cfg: dict) -> Candidate:
    """Score-based candidate generation. All conditions are cheap, deterministic checks."""
    score = 0
    reasons: list[str] = []

    if snap.ema_fast > snap.ema_slow:
        score += 1
        reasons.append("uptrend: EMA fast > slow")
    if snap.rsi < float(cfg["rsi_oversold"]) + 10 and snap.rsi > 25:
        score += 1
        reasons.append(f"RSI {snap.rsi:.0f} in buy zone (recovering, not free-falling)")
    if snap.macd_hist > 0 and snap.macd_hist_prev <= 0:
        score += 2
        reasons.append("MACD histogram flipped positive (fresh momentum)")
    elif snap.macd_hist > snap.macd_hist_prev > 0:
        score += 1
        reasons.append("MACD momentum increasing")
    if snap.price <= snap.bb_lower * 1.02:
        score += 1
        reasons.append("price near lower Bollinger band")

    action = "buy" if score >= int(cfg["min_signal_score"]) else "hold"
    return Candidate(market=snap.market, action=action, score=score, reasons=reasons, snapshot=snap)


def check_exit(entry_price: float, stop_loss: float, take_profit: float,
               snap: MarketSnapshot) -> tuple[bool, str]:
    """Mechanical exit rules. Deliberately no LLM involvement."""
    if snap.price <= stop_loss:
        return True, f"stop loss hit ({snap.price:.2f} <= {stop_loss:.2f})"
    if snap.price >= take_profit:
        return True, f"take profit hit ({snap.price:.2f} >= {take_profit:.2f})"
    if snap.ema_fast < snap.ema_slow and snap.rsi > float(70):
        return True, "trend break: EMA cross down with overbought RSI"
    return False, ""


def time_stop_hit(candles: list, opened_at, entry_price: float, current_price: float,
                  round_trip_pct: float, n_candles: int,
                  min_net_pct: float = 0.0) -> tuple[bool, str]:
    """Time-stop: sluit een positie die te lang stilstaat zonder TP/SL te raken.

    Puur en testbaar, geen AI, geen nieuws. De positie wordt geëxit als er sinds
    entry minstens `n_candles` candles verstreken zijn EN de nettowinst bij nu
    verkopen (koersverschil minus round-trip fees) op of onder `min_net_pct` ligt.
    Zo maak je een slot plus kapitaal vrij dat anders eindeloos zijwaarts hangt,
    zonder winnaars vroeg te knippen (die staan boven de drempel).
    """
    if n_candles <= 0 or entry_price <= 0:
        return False, ""
    opened_ms = int(opened_at.timestamp() * 1000)
    elapsed = sum(1 for c in candles if c.ts > opened_ms)
    if elapsed < n_candles:
        return False, ""
    net_pct = (current_price / entry_price - 1) * 100 - round_trip_pct
    if net_pct <= min_net_pct:
        return True, (f"time-stop: {elapsed} candles zonder TP/SL, "
                      f"netto {net_pct:.2f}% <= {min_net_pct:.2f}%")
    return False, ""
