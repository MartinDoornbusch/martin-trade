"""Marktscanner: screent alle Bitvavo EUR-markten op liquiditeit, spread en
signaalkwaliteit, fee-bewust (vereiste move = round-trip fees + spread + winstdrempel).

Bewuste grens (zie post-mortem in PROJECTPLAN): de scanner adviseert alleen.
Toevoegen aan markets/watchlist doet de gebruiker via de add-on-configuratie;
de bot handelt nooit zelf in een gescande markt.
"""
from __future__ import annotations

import logging

from .decision import FeeModel
from .strategy import build_snapshot, evaluate_buy

log = logging.getLogger(__name__)

MIN_VOLUME_EUR = 250_000   # 24h; daaronder is de spread/slippage onbetrouwbaar
MAX_SPREAD_PCT = 0.60      # boven deze spread vreet de onzichtbare kost elke edge op
CANDLE_TOP = 40            # alleen voor de grootste markten candles ophalen (rate limit)


def liquidity_filter(tickers: list[dict], min_volume: float = MIN_VOLUME_EUR,
                     max_spread: float = MAX_SPREAD_PCT) -> list[dict]:
    """Puur en testbaar: EUR-markten met genoeg volume en acceptabele spread."""
    out = []
    for t in tickers:
        market = t.get("market", "")
        if not market.endswith("-EUR"):
            continue
        try:
            volume = float(t.get("volumeQuote") or 0)
            bid = float(t.get("bid") or 0)
            ask = float(t.get("ask") or 0)
        except (TypeError, ValueError):
            continue
        if volume < min_volume or bid <= 0 or ask <= bid:
            continue
        spread = (ask - bid) / ((ask + bid) / 2) * 100
        if spread > max_spread:
            continue
        out.append({"market": market, "volume_eur": round(volume),
                    "spread_pct": round(spread, 3)})
    out.sort(key=lambda s: -s["volume_eur"])
    return out


def scan(feed, cfg, top_n: int = 15) -> list[dict]:
    """Volledige scan: liquiditeitsfilter + indicator-score voor de top-volume markten."""
    fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                         cfg.fees["slippage_buffer_pct"])
    min_profit = float(cfg.decision["min_profit_pct"])
    interval = cfg.schedule["candle_interval"]
    candidates = liquidity_filter(feed.get_ticker_24h())
    results = []
    for c in candidates[:CANDLE_TOP]:
        market = c["market"]
        try:
            candles = feed.get_candles(market, interval, 80)
            if len(candles) < 70:  # te jonge markt, indicatoren onbetrouwbaar
                continue
            snap = build_snapshot(market, candles, cfg.strategy)
            cand = evaluate_buy(snap, cfg.strategy)
            stop_dist = snap.atr * float(cfg.decision["atr_stop_multiplier"])
            expected = stop_dist * float(cfg.decision["reward_risk_ratio"]) / snap.price * 100
            # Fee-gate inclusief de werkelijke spread van deze markt:
            required = fee_model.round_trip_pct() + c["spread_pct"] + min_profit
            results.append({
                **c,
                "price": snap.price,
                "score": cand.score,
                "score_needed": int(cfg.strategy["min_signal_score"]),
                "trend": "up" if snap.ema_fast > snap.ema_slow else "down",
                "rsi": round(snap.rsi, 0),
                "expected_move_pct": round(expected, 2),
                "required_pct": round(required, 2),
                "fee_ok": expected >= required,
                "in_markets": market in cfg.markets,
                "in_watchlist": market in cfg.watchlist,
                "reasons": cand.reasons,
            })
        except Exception as exc:  # noqa: BLE001 - één markt mag de scan niet breken
            log.debug("scanner sloeg %s over: %s", market, exc)
    results.sort(key=lambda r: (-r["score"], -r["expected_move_pct"]))
    return results[:top_n]
