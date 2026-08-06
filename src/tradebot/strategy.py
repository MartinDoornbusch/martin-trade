"""Deterministic swing strategy: produces candidate signals from indicators.

The LLM never generates signals; it may only confirm or veto a BUY candidate.
Exits zijn 100% mechanisch en bestaan uit precies drie regels:

* stop loss / take profit (`check_exit`, ook per minuut door de position guard),
* time-stop (`time_stop_hit`): een positie die na N candles nog geen TP/SL raakte
  en per saldo niet boven break-even staat, geeft zijn slot terug,
* breakeven-stop (`breakeven_stop_hit`): een positie die ver genoeg in de winst
  heeft gestaan mag niet meer als verlies eindigen.

Er is bewust géén trend-break-exit meer. De oude regel (EMA-cross-down samen met
een overbought RSI) eiste twee vrijwel disjuncte condities en is in v0.19.0
geschrapt in plaats van herschreven.

Correctie op v0.19.0: daar stond "heeft in productie nooit gevuurd". Dat was een
overclaim. Het bewijs was tweeledig — coverage liet zien dat de TESTSUITE die
regel nooit raakte, en de twee condities zijn bijna disjunct — en geen van beide
is een productiemeting. Er is nooit geteld hoe vaak hij live vuurde. De
attributierun (`tradebot.calibrate`, stap "- trend-break-exit") meet het effect
alsnog op historische data; tot die uitkomst er is, blijft dit een aanname.

Asymmetrie sinds v0.20.0: ENTRIES worden beoordeeld op afgesloten candles
(`drop_unclosed`), EXITS op de live prijs. Zie de docstring van `drop_unclosed`
voor het waarom.
"""
from __future__ import annotations

import time
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


def build_snapshots(market: str, candles: list[Candle],
                    cfg: dict) -> list[MarketSnapshot | None]:
    """Snapshot per index, in één pass over de reeks.

    Exact equivalent aan `build_snapshot(market, candles[: i + 1], cfg)` voor elke
    i, maar O(n) in plaats van O(n²). Dat mag omdat elke gebruikte indicator
    prefix-stabiel is: EMA, RSI (Wilder) en ATR zijn recursief vanaf het begin van
    de reeks en Bollinger is een rollend venster, dus geen van alle kijkt vooruit.
    Bedoeld voor de backtester en de optimizer, die anders per bar de hele historie
    opnieuw doorrekenen. `tests/test_backtest.py` pint de gelijkheid vast, zodat
    deze functie niet stilletjes van `build_snapshot` af kan drijven.

    Geeft None terug voor indices waar nog geen twee candles beschikbaar zijn.
    """
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    ef = ema(closes, int(cfg["ema_fast"]))
    es = ema(closes, int(cfg["ema_slow"]))
    r = rsi(closes, int(cfg["rsi_period"]))
    _, _, hist = macd(closes)
    a = atr(highs, lows, closes, int(cfg["atr_period"]))
    bb_mid, _, bb_low = bollinger(closes)
    out: list[MarketSnapshot | None] = []
    for i in range(len(candles)):
        if i < 1:
            out.append(None)
            continue
        out.append(MarketSnapshot(
            market=market,
            price=closes[i],
            ema_fast=float(ef[i]),
            ema_slow=float(es[i]),
            rsi=float(r[i]),
            macd_hist=float(hist[i]),
            macd_hist_prev=float(hist[i - 1]),
            atr=float(a[i]),
            bb_lower=float(bb_low[i]),
            bb_mid=float(bb_mid[i]),
            change_24c_pct=(closes[i] / closes[i - 6] - 1) * 100 if i >= 6 else 0.0,
        ))
    return out


def chase_too_far(signal_price: float, live_price: float, atr_value: float,
                  max_chase_atr: float) -> tuple[bool, str]:
    """Staat de live prijs te ver van de signaalclose af om nog in te stappen?

    Bijwerking van `signal_on_closed_candles` (v0.20.0): het signaal komt van de
    afgesloten candle, de fill van de live prijs. Is de koers sinds die close al
    doorgelopen, dan stap je later in met dezelfde targetafstand. Het RISICO klopt
    nog (stop en target hangen aan de fill, dus 2x ATR blijft 2x ATR), maar de EDGE
    niet: een deel van de verwachte beweging is al gemaakt voordat je binnen bent.
    Vóór v0.20.0 viel dat samen, want signaal en prijs kwamen uit dezelfde bar.

    Puur en symmetrisch: een koers die is weggezakt telt evengoed als drift, want
    dan is de signaalclose niet meer de situatie waarop de score is gebaseerd.
    `max_chase_atr <= 0` schakelt de guard uit.
    """
    if max_chase_atr <= 0 or atr_value <= 0 or signal_price <= 0:
        return False, ""
    drift = abs(live_price - signal_price)
    if drift <= max_chase_atr * atr_value:
        return False, ""
    return True, (f"chase-guard: live {live_price:.6g} staat "
                  f"{drift / atr_value:.2f}x ATR van de signaalclose "
                  f"{signal_price:.6g} (max {max_chase_atr:.2f}x)")


def intrabar_exit(candle: Candle, stop: float, target: float,
                  stop_first: bool = True) -> str | None:
    """Raakte deze candle de stop of het target BINNEN de bar? -> "stop"|"target"|None.

    Vergelijken met de slotkoers is niet goed genoeg: een candle die met zijn low
    door de stop ging maar erboven sloot, houdt de positie in zo'n model open,
    terwijl de position guard live binnen de minuut uitstopt. Dat tilt de win-rate
    kunstmatig op.

    Raakt één candle stop én target, dan wint standaard de stop. De volgorde binnen
    de bar is uit OHLC niet af te leiden, dus dit is de conservatieve aanname; ze
    stond al zo in de veto-analyse en is nu één gedeelde bron voor backtester en
    analyse.
    """
    hit_stop = candle.low <= stop
    hit_target = candle.high >= target
    if hit_stop and hit_target:
        return "stop" if stop_first else "target"
    if hit_stop:
        return "stop"
    if hit_target:
        return "target"
    return None


def drop_unclosed(candles: list[Candle], now_ms: int | None = None) -> list[Candle]:
    """Knip de lopende, nog niet gesloten candle van een oplopende reeks af.

    Bitvavo levert de bar die op dit moment loopt mee als nieuwste element. Een
    indicatorwaarde op die bar beweegt dus nog: bij een uurcyclus op 4h-candles
    wordt dezelfde bar drie tot vier keer beoordeeld en pakt de bot de eerste,
    vluchtigste realisatie van een flip-conditie. Voor ENTRIES is dat schadelijk
    (meer trades dan de backtest voorspelt bij hetzelfde signaal); voor EXITS is
    actualiteit juist gewenst, dus die blijven op de live prijs.

    Pure functie. `now_ms` is injecteerbaar zodat een test niet van de klok
    afhangt. Het interval wordt uit de afstand tussen de laatste twee candles
    afgeleid: zo werkt de functie op elk candle-interval zonder config-plumbing,
    en laat hij een reeks die al alleen afgesloten candles bevat ongemoeid (dan
    zou blind `[:-1]` een geldige bar weggooien en de bot een candle achterlopen).
    """
    if len(candles) < 2:
        return list(candles)
    interval_ms = candles[-1].ts - candles[-2].ts
    if interval_ms <= 0:
        return list(candles)
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if candles[-1].ts + interval_ms > now_ms:
        return list(candles[:-1])
    return list(candles)


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

    Bewust verschil met `time_stop_hit`: die knipt de lopende candle eraf, deze
    telt hem juist mee. De time-stop TELT candles, dus een bar die nog loopt is
    nog geen verstreken bar en zou de stop een candle te vroeg laten vuren. De
    breakeven-stop MEET een piek, en een high die zojuist in de lopende bar is
    gezet is een even echte piek als een high van gisteren; hem negeren zou de
    stop juist te laat bewapenen.
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
                  min_net_pct: float = 0.0, now_ms: int | None = None) -> tuple[bool, str]:
    """Time-stop: sluit een positie die te lang stilstaat zonder TP/SL te raken.

    Puur en testbaar, geen AI, geen nieuws. De positie wordt geëxit als er sinds
    entry minstens `n_candles` candles VERSTREKEN zijn EN de nettowinst bij nu
    verkopen (koersverschil minus round-trip fees) op of onder `min_net_pct` ligt.
    Zo maak je een slot plus kapitaal vrij dat anders eindeloos zijwaarts hangt,
    zonder winnaars vroeg te knippen (die staan boven de drempel).

    "Verstreken" betekent afgesloten: de lopende bar wordt via `drop_unclosed`
    weggelaten. Hem meetellen liet de stop ongeveer een candle te vroeg vuren, en
    binnen dezelfde bar zelfs meerdere uurcycli lang. Dit staat los van de
    breakeven-stop, die de lopende bar juist wél meeneemt (zie daar).
    """
    if n_candles <= 0 or entry_price <= 0:
        return False, ""
    opened_ms = int(opened_at.timestamp() * 1000)
    elapsed = sum(1 for c in drop_unclosed(candles, now_ms) if c.ts > opened_ms)
    if elapsed < n_candles:
        return False, ""
    net_pct = (current_price / entry_price - 1) * 100 - round_trip_pct
    if net_pct <= min_net_pct:
        return True, (f"time-stop: {elapsed} candles zonder TP/SL, "
                      f"netto {net_pct:.2f}% <= {min_net_pct:.2f}%")
    return False, ""
