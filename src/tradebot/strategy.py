"""Deterministic swing strategy: produces candidate signals from indicators.

The LLM never generates signals; it may only confirm or veto a BUY candidate.
Exits zijn 100% mechanisch en bestaan uit precies drie regels:

* stop loss / take profit (`check_exit`, ook per minuut door de position guard),
* time-stop (`time_stop_hit`): een positie die na N candles nog geen TP/SL raakte
  en per saldo niet boven break-even staat, geeft zijn slot terug,
* breakeven-stop (`breakeven_stop_hit`): een positie die ver genoeg in de winst
  heeft gestaan mag niet meer als verlies eindigen.

Er is bewust géén trend-break-exit meer. De oude regel (EMA-cross-down samen met
een overbought RSI) eiste twee vrijwel disjuncte condities en heeft in productie
nooit gevuurd; hij is in v0.19.0 geschrapt in plaats van herschreven.
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
    # Koopzone expliciet in config: eerder stond hier `rsi_oversold + 10` met een
    # hardcoded ondergrens 25, waardoor config 35 zei en de werkelijke bovengrens 45
    # was. De grenzen staan nu waar ze horen; de defaults zijn de oude effectieve
    # waarden, zodat oudere config-dicts hetzelfde blijven doen.
    zone_min = float(cfg.get("rsi_buy_zone_min", 25))
    zone_max = float(cfg.get("rsi_buy_zone_max", 45))
    if zone_min < snap.rsi < zone_max:
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
    """Harde prijs-exits: stop loss en take profit. Bewust geen LLM.

    Alleen prijs vs. niveau, zodat de position guard (per minuut, zonder candles)
    exact dezelfde regels hanteert als de analysecyclus. Tijd- en trendgebonden
    exits zitten in `time_stop_hit` en `breakeven_stop_hit`.
    """
    if snap.price <= stop_loss:
        return True, f"stop loss hit ({snap.price:.2f} <= {stop_loss:.2f})"
    if snap.price >= take_profit:
        return True, f"take profit hit ({snap.price:.2f} >= {take_profit:.2f})"
    return False, ""


def breakeven_stop_hit(candles: list, opened_at, entry_price: float, current_price: float,
                       atr_value: float, trigger_atr: float,
                       offset_pct: float) -> tuple[bool, str]:
    """Breakeven-stop: wat ver genoeg in de winst stond, mag geen verlies worden.

    Puur en testbaar, geen AI, geen nieuws. Twee stappen:

    1. Bewapenen. De hoogste high sinds entry moet minstens `trigger_atr` x ATR
       boven de entryprijs hebben gelegen. Zo telt alleen winst die groter was dan
       de normale ruis van de markt; een positie die nooit echt in de plus stond
       wordt niet vervroegd geknipt en houdt zijn volle ATR-stop.
    2. Vuren. De koers is daarna teruggezakt tot op of onder
       `entry * (1 + offset_pct/100)`. Die offset dekt de round-trip fees (0,50%
       taker), zodat de exit netto rond break-even uitkomt in plaats van op een
       verlies na kosten.

    Vervangt de geschrapte trend-break-exit: dezelfde bedoeling (een omkering na
    entry afvangen) maar op prijs en gerealiseerde winst in plaats van op twee
    indicatorcondities die elkaar in de praktijk uitsluiten.
    """
    if entry_price <= 0 or atr_value <= 0 or trigger_atr <= 0:
        return False, ""
    opened_ms = int(opened_at.timestamp() * 1000)
    highs = [c.high for c in candles if c.ts > opened_ms]
    if not highs:
        return False, ""
    peak = max(highs)
    if peak < entry_price + trigger_atr * atr_value:
        return False, ""
    level = entry_price * (1 + offset_pct / 100)
    if current_price > level:
        return False, ""
    return True, (f"breakeven-stop: piek stond {(peak / entry_price - 1) * 100:.2f}% "
                  f"in de winst (>= {trigger_atr:.2f}x ATR), koers terug op "
                  f"{(current_price / entry_price - 1) * 100:.2f}% "
                  f"(drempel {offset_pct:.2f}%)")


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
