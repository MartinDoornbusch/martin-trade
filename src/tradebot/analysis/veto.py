"""Counterfactual-analyse van de LLM-veto-gate.

Kernvraag: voorkomt de LLM-veto verlies, of snijdt hij winst weg? Voor elke
gevetoode buy-kandidaat wordt gereconstrueerd wat de trade zou hebben gedaan
als hij WEL was uitgevoerd, met exact de mechanische exits van de bot zelf
(ATR-stop/target uit `decision`) en de echte round-trip kosten (2x taker fee +
slippage-buffer uit `fees`). Twee exit-modellen naast elkaar:

  * vaste horizon : slotkoers N candles later.
  * TP/SL         : intrabar (high/low) tot stop of target raakt, anders sluit
                    op de laatste close binnen het venster.

Uitkomst per veto:
  net < 0  -> veto voorkwam verlies   (correct geblokkeerd)
  net > 0  -> veto sneed winst weg    (gemiste kans)

De module is read-only en voert nooit orders uit. Hij hergebruikt de
strategie- en kostenlogica van de bot zodat de counterfactual overeenkomt met
wat de bot daadwerkelijk zou hebben gedaan.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from ..exchange import Candle, ExchangeAdapter
from ..strategy import build_snapshot

# Indicator-warmup: EMA-slow (26) + MACD-signaal (9) + marge. Onder deze index
# is een snapshot niet betrouwbaar.
WARMUP_CANDLES = 60

# Redenen die richting-technisch verdacht zijn als grond om een BUY te vetoen.
# De strategie scoort "koers bij onderste Bollinger-band" juist als koopreden
# (strategy.evaluate_buy). Een veto die datzelfde feit als "overextension"
# aanvoert, is intern tegenstrijdig met de strategie.
_SUSPECT_SUBSTRINGS = ("lower bollinger", "lower band", "near lower", "onderband")

_CONF_BUCKETS = [(0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]

# Categorisatie van veto-redenen. Doel: mean-reversion-vetoes (die een
# momentum-instap omkeren) los kunnen beoordelen van mechanische redenen die de
# strategie zelf niet dekt (spread, data, volatiliteit). Eerste match wint.
_REASON_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("mean-reversion", ("bollinger", "lower band", "upper band", "onderband", "bovenband",
                        "overextension", "overextended", "overbought", "oversold",
                        "mean revert", "mean-revert", "stretched", "parabolic",
                        "too far", "extended", "pullback", "reversal")),
    ("volatiliteit", ("volatile", "volatilit", "atr", "whipsaw", "choppy", "erratic",
                      "wild swing")),
    ("liquiditeit/spread", ("spread", "illiquid", "thin", "low volume", "lage volume",
                            "liquidity", "liquiditeit", "slippage")),
    ("data-integriteit", ("stale", "missing data", "insufficient data", "no data",
                          "geen data", "onvoldoende data", "corrupt", "gap in data")),
]

# Doelaantal afgewikkelde trades voor een schone precisiemeting op een config.
TARGET_RESOLVED = 20


@dataclass
class VetoOutcome:
    ts: str
    market: str
    confidence: float
    reasoning: str
    entry_price: float
    net_fixed_pct: float | None      # vaste-horizon rendement, netto na kosten
    net_tpsl_pct: float | None       # TP/SL rendement, netto na kosten
    tpsl_exit: str                   # "stop" | "target" | "timeout"
    suspect_reason: bool             # reden is richting-technisch verdacht
    category: str = "overig"         # veto-reden-categorie
    real_net_pct: float | None = None  # ECHTE shadow-trade uitkomst, netto na fees
    real_exit: str = ""              # "target" | "stop" | "overig" (uit trade-reason)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RoundTrip:
    """Een afgewikkelde paper-trade (buy gekoppeld aan zijn sluitende sell)."""
    market: str
    buy_ms: int
    sell_ms: int
    net_pct: float                   # gerealiseerde pnl / inleg * 100 (pnl is al netto fees)
    exit: str                        # "target" | "stop" | "overig"


@dataclass
class VetoParams:
    interval: str
    cost_pct: float                  # round-trip kosten in % (2x taker + slippage)
    atr_stop_multiplier: float
    reward_risk_ratio: float
    position_size_eur: float
    horizon_candles: int = 6
    tpsl_max_candles: int = 48
    same_candle_stop_first: bool = True


def params_from_config(cfg, horizon_candles: int = 6,
                       tpsl_max_candles: int = 48) -> VetoParams:
    """Bouw analyse-parameters uit de app-config (fees, decision, risk)."""
    taker = float(cfg.fees["taker_pct"])
    slippage = float(cfg.fees.get("slippage_buffer_pct", 0.0))
    pos = (float(cfg.risk["paper_start_eur"])
           * float(cfg.risk["max_position_pct"]) / 100.0)
    return VetoParams(
        interval=cfg.schedule["candle_interval"],
        cost_pct=2 * taker + slippage,
        atr_stop_multiplier=float(cfg.decision["atr_stop_multiplier"]),
        reward_risk_ratio=float(cfg.decision["reward_risk_ratio"]),
        position_size_eur=pos,
        horizon_candles=horizon_candles,
        tpsl_max_candles=tpsl_max_candles,
    )


# --- tijd-helpers ----------------------------------------------------------

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def interval_seconds(interval: str) -> int:
    """'4h' -> 14400, '15m' -> 900, '1d' -> 86400."""
    interval = interval.strip().lower()
    unit = interval[-1]
    if unit not in _UNIT_SECONDS:
        raise ValueError(f"onbekend candle-interval: {interval}")
    return int(interval[:-1]) * _UNIT_SECONDS[unit]


def _to_ms(ts) -> int:
    """Timestamp (ISO-string, datetime of numeriek) naar unix-milliseconden."""
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)
    if isinstance(ts, int | float):
        return int(ts if ts > 1e12 else ts * 1000)
    s = str(ts).strip().replace("Z", "+00:00")
    from datetime import datetime
    return int(datetime.fromisoformat(s).timestamp() * 1000)


# --- candle-indexering -----------------------------------------------------

def _entry_index(candles: list[Candle], veto_ms: int) -> int | None:
    """Index van de candle die op/voor het veto-moment sloot (bisect op ts)."""
    import bisect
    ts_list = [c.ts for c in candles]
    idx = bisect.bisect_right(ts_list, veto_ms) - 1
    if idx < WARMUP_CANDLES or idx >= len(candles):
        return None
    return idx


# --- exit-modellen ---------------------------------------------------------

def _fixed_horizon(candles: list[Candle], idx: int, entry: float,
                   p: VetoParams) -> float | None:
    exit_idx = min(idx + p.horizon_candles, len(candles) - 1)
    if exit_idx <= idx:
        return None
    gross = candles[exit_idx].close / entry - 1.0
    return gross * 100 - p.cost_pct


def _tp_sl(candles: list[Candle], idx: int, entry: float, atr: float,
           p: VetoParams) -> tuple[float | None, str]:
    stop = entry - atr * p.atr_stop_multiplier
    target = entry + atr * p.atr_stop_multiplier * p.reward_risk_ratio
    last = min(idx + p.tpsl_max_candles, len(candles) - 1)
    for i in range(idx + 1, last + 1):
        hit_stop = candles[i].low <= stop
        hit_target = candles[i].high >= target
        if hit_stop and hit_target:
            if p.same_candle_stop_first:
                return (stop / entry - 1.0) * 100 - p.cost_pct, "stop"
            return (target / entry - 1.0) * 100 - p.cost_pct, "target"
        if hit_stop:
            return (stop / entry - 1.0) * 100 - p.cost_pct, "stop"
        if hit_target:
            return (target / entry - 1.0) * 100 - p.cost_pct, "target"
    if last <= idx:
        return None, "timeout"
    gross = candles[last].close / entry - 1.0
    return gross * 100 - p.cost_pct, "timeout"


def _is_suspect(reasoning: str) -> bool:
    t = (reasoning or "").lower()
    return any(sub in t for sub in _SUSPECT_SUBSTRINGS)


def _categorize(reasoning: str) -> str:
    """Wijs een veto-reden toe aan een categorie (eerste match wint)."""
    t = (reasoning or "").lower()
    for cat, subs in _REASON_CATEGORIES:
        if any(s in t for s in subs):
            return cat
    return "overig"


def _classify_exit(reason: str) -> str:
    """Leid uit de trade-reden af of de exit een target of stop was."""
    t = (reason or "").lower()
    if "take profit" in t or "take-profit" in t or "target" in t:
        return "target"
    if "stop" in t:
        return "stop"
    return "overig"


# --- echte shadow-uitkomst (round-trips uit de trades-tabel) ----------------

def build_roundtrips(trades: list[dict]) -> list[RoundTrip]:
    """Koppel buys aan hun sluitende sell per markt, chronologisch. Steunt op de
    invariant "één positie per markt": de eerstvolgende sell sluit de open buy.
    """
    by_market: dict[str, list[dict]] = {}
    for t in sorted(trades, key=lambda x: _to_ms(x["ts"])):
        by_market.setdefault(t["market"], []).append(t)
    out: list[RoundTrip] = []
    for market, seq in by_market.items():
        open_buy: dict | None = None
        for t in seq:
            side = (t.get("side") or "").lower()
            if side == "buy" and open_buy is None:
                open_buy = t
            elif side == "sell" and open_buy is not None:
                notional = float(open_buy["amount"]) * float(open_buy["price"])
                net = (float(t.get("pnl_eur") or 0.0) / notional * 100) if notional > 0 else 0.0
                out.append(RoundTrip(market, _to_ms(open_buy["ts"]), _to_ms(t["ts"]),
                                     round(net, 4), _classify_exit(t.get("reason", ""))))
                open_buy = None
    return out


def match_real_outcome(veto_ms: int, market: str, roundtrips: list[RoundTrip],
                       window_ms: int) -> RoundTrip | None:
    """Koppel een veto aan de trade die er direct uit volgde: de vroegste buy op
    dezelfde markt op/na het veto-moment, binnen `window_ms`.
    """
    best: RoundTrip | None = None
    for rt in roundtrips:
        if rt.market != market or rt.buy_ms < veto_ms or rt.buy_ms - veto_ms > window_ms:
            continue
        if best is None or rt.buy_ms < best.buy_ms:
            best = rt
    return best


def _wilson_half_width(k: int, n: int, z: float = 1.96) -> float:
    """95%-Wilson-halfbreedte (in procentpunten) voor een aandeel k/n. Bij kleine
    n eerlijker dan de normale benadering; geeft 0 terug bij n=0.
    """
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(half * 100, 1)


# --- kern ------------------------------------------------------------------

def evaluate_vetos(vetos: list[dict], candles_by_market: dict[str, list[Candle]],
                   strategy_cfg: dict, p: VetoParams,
                   roundtrips: list[RoundTrip] | None = None
                   ) -> tuple[list[VetoOutcome], dict]:
    """Reken elke veto door tegen de meegegeven candles. Puur en testbaar:
    geen DB, geen netwerk. `vetos` = [{ts, market, confidence, reasoning}, ...].

    Met `roundtrips` wordt elke veto ook gekoppeld aan de echte paper-trade die
    er (in shadow-mode) uit volgde, binnen twee candle-intervallen.
    """
    outcomes: list[VetoOutcome] = []
    skipped: dict[str, int] = {}
    window_ms = 2 * interval_seconds(p.interval) * 1000

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for v in vetos:
        market = v["market"]
        candles = candles_by_market.get(market)
        if not candles:
            skip("geen_candles_voor_markt")
            continue
        try:
            veto_ms = _to_ms(v["ts"])
        except (ValueError, TypeError):
            skip("ongeldige_timestamp")
            continue
        idx = _entry_index(candles, veto_ms)
        if idx is None:
            skip("geen_candle_op_ts_of_te_vroeg")
            continue
        snap = build_snapshot(market, candles[: idx + 1], strategy_cfg)
        entry = snap.price
        if entry <= 0 or snap.atr <= 0:
            skip("ongeldige_entry_of_atr")
            continue
        net_fixed = _fixed_horizon(candles, idx, entry, p)
        net_tpsl, exit_reason = _tp_sl(candles, idx, entry, snap.atr, p)
        if net_fixed is None and net_tpsl is None:
            skip("geen_forward_data")
            continue
        reasoning = v.get("reasoning") or ""
        real = (match_real_outcome(veto_ms, market, roundtrips, window_ms)
                if roundtrips else None)
        outcomes.append(VetoOutcome(
            ts=str(v["ts"]),
            market=market,
            confidence=float(v.get("confidence") or 0.0),
            reasoning=reasoning,
            entry_price=round(entry, 8),
            net_fixed_pct=None if net_fixed is None else round(net_fixed, 4),
            net_tpsl_pct=None if net_tpsl is None else round(net_tpsl, 4),
            tpsl_exit=exit_reason,
            suspect_reason=_is_suspect(reasoning),
            category=_categorize(reasoning),
            real_net_pct=None if real is None else real.net_pct,
            real_exit="" if real is None else real.exit,
        ))
    return outcomes, skipped


# --- aggregatie ------------------------------------------------------------

def _summ(values: list[float], pos_size: float) -> dict | None:
    n = len(values)
    if n == 0:
        return None
    avoided = [x for x in values if x < 0]
    missed = [x for x in values if x > 0]
    avoided_eur = -sum(avoided) / 100 * pos_size
    missed_eur = sum(missed) / 100 * pos_size
    return {
        "n": n,
        "veto_precision_pct": round(len(avoided) / n * 100, 1),
        "precision_margin_pp": _wilson_half_width(len(avoided), n),
        "n_avoided": len(avoided),
        "n_missed": len(missed),
        "avoided_eur": round(avoided_eur, 2),
        "missed_eur": round(missed_eur, 2),
        "net_gate_eur": round(avoided_eur - missed_eur, 2),
        "avg_net_pct": round(sum(values) / n, 3),
    }


def _breakdown(outcomes: list[VetoOutcome], key_fn, model: str,
               pos_size: float) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for o in outcomes:
        val = getattr(o, model)
        if val is None:
            continue
        groups.setdefault(key_fn(o), []).append(val)
    rows = []
    for k, vals in groups.items():
        s = _summ(vals, pos_size)
        if s:
            rows.append({"group": k, **s})
    rows.sort(key=lambda r: r["net_gate_eur"])
    return rows


def _conf_bucket(conf: float) -> str:
    for lo, hi in _CONF_BUCKETS:
        if lo <= conf < hi:
            return f"{lo:.1f}-{min(hi, 1.0):.1f}"
    return "onbekend"


def summarize(outcomes: list[VetoOutcome], skipped: dict, p: VetoParams,
              config_hash: str | None = None) -> dict:
    """Bouw de JSON-vriendelijke samenvatting voor dashboard/CLI."""
    fixed_vals = [o.net_fixed_pct for o in outcomes if o.net_fixed_pct is not None]
    tpsl_vals = [o.net_tpsl_pct for o in outcomes if o.net_tpsl_pct is not None]
    real_vals = [o.real_net_pct for o in outcomes if o.real_net_pct is not None]
    suspect = [o for o in outcomes if o.suspect_reason]
    return {
        "n_vetos": len(outcomes),
        "config_hash": config_hash,
        "config_scope": "current" if config_hash else "all",
        "n_real_matched": len(real_vals),
        "target_resolved": TARGET_RESOLVED,
        "position_size_eur": round(p.position_size_eur, 2),
        "cost_pct": round(p.cost_pct, 3),
        "params": {
            "interval": p.interval,
            "horizon_candles": p.horizon_candles,
            "atr_stop_multiplier": p.atr_stop_multiplier,
            "reward_risk_ratio": p.reward_risk_ratio,
        },
        "fixed_horizon": _summ(fixed_vals, p.position_size_eur),
        "tpsl": _summ(tpsl_vals, p.position_size_eur),
        "real_outcome": _summ(real_vals, p.position_size_eur),
        "by_reason": _breakdown(outcomes, lambda o: o.category, "net_fixed_pct",
                                p.position_size_eur),
        "by_market": _breakdown(outcomes, lambda o: o.market, "net_fixed_pct",
                                p.position_size_eur),
        "by_confidence": _breakdown(outcomes, lambda o: _conf_bucket(o.confidence),
                                    "net_fixed_pct", p.position_size_eur),
        "suspect_reason_count": len(suspect),
        "suspect_examples": [o.as_dict() for o in suspect[:10]],
        "skipped": skipped,
    }


# --- toplevel: DB + Bitvavo ------------------------------------------------

def _load_vetos_from_db(config_hash: str | None = None) -> list[dict]:
    from sqlalchemy import select

    from ..db import LLMCallRow, session
    with session() as s:
        stmt = select(LLMCallRow).order_by(LLMCallRow.ts.asc())
        if config_hash is not None:
            stmt = stmt.where(LLMCallRow.config_hash == config_hash)
        rows = s.execute(stmt).scalars().all()
    return [{"ts": r.ts, "market": r.market, "confidence": r.confidence,
             "reasoning": r.reasoning}
            for r in rows if r.verdict == "veto" and r.market]


def _load_roundtrips_from_db() -> list[dict]:
    from sqlalchemy import select

    from ..db import TradeRow, session
    with session() as s:
        rows = s.execute(select(TradeRow).where(TradeRow.mode == "paper")
                         .order_by(TradeRow.ts.asc())).scalars().all()
    return [{"ts": r.ts, "market": r.market, "side": r.side, "amount": r.amount,
             "price": r.price, "pnl_eur": r.pnl_eur, "reason": r.reason} for r in rows]


def _fetch_candles(adapter: ExchangeAdapter, markets: list[str], min_ms: int,
                   p: VetoParams) -> dict[str, list[Candle]]:
    sec = interval_seconds(p.interval)
    import time
    span = max(0, int(time.time()) - min_ms // 1000)
    needed = WARMUP_CANDLES + span // sec + p.tpsl_max_candles + 5
    needed = max(int(needed), 200)
    out: dict[str, list[Candle]] = {}
    for m in markets:
        try:
            out[m] = adapter.get_candles_history(m, p.interval, needed)
        except Exception:  # noqa: BLE001 - één markt mag de analyse niet breken
            out[m] = []
    return out


def analyze_vetos(adapter: ExchangeAdapter, cfg, *, vetos: list[dict] | None = None,
                  candles_by_market: dict[str, list[Candle]] | None = None,
                  trades: list[dict] | None = None, config_hash: str | None = None,
                  horizon_candles: int = 6, tpsl_max_candles: int = 48) -> dict:
    """Toplevel: laad vetos (DB), haal candles (Bitvavo), reken door, vat samen.

    `config_hash` beperkt de meting tot vetoes van één configuratie (schone
    meting op de nieuwe config). Injecteer `vetos`, `candles_by_market` en/of
    `trades` om DB/netwerk te omzeilen (gebruikt in tests).
    """
    p = params_from_config(cfg, horizon_candles, tpsl_max_candles)
    if vetos is None:
        vetos = _load_vetos_from_db(config_hash)
    if not vetos:
        return summarize([], {"geen_vetos": 1}, p, config_hash)
    if trades is None:
        # candles_by_market == None duidt een echte (online) run aan; alleen dan
        # de DB raadplegen. Bij geinjecteerde candles (offline tests) geen DB.
        trades = _load_roundtrips_from_db() if candles_by_market is None else []
    roundtrips = build_roundtrips(trades)
    if candles_by_market is None:
        markets = sorted({v["market"] for v in vetos})
        min_ms = min(_to_ms(v["ts"]) for v in vetos)
        candles_by_market = _fetch_candles(adapter, markets, min_ms, p)
    outcomes, skipped = evaluate_vetos(vetos, candles_by_market, cfg.strategy, p, roundtrips)
    return summarize(outcomes, skipped, p, config_hash)


def main() -> None:
    """CLI: python -m tradebot.analysis.veto [--all]

    Standaard alleen de huidige config (schone meting). `--all` meet elke veto
    ooit, ongeacht configuratie (de vervuilde totaalmeting).
    """
    import json
    import sys

    from ..config import config_fingerprint, get_config, get_secrets
    from ..db import init_db
    from ..exchange import BitvavoClient
    cfg = get_config()
    secrets = get_secrets()
    init_db(secrets.database_url)
    feed = BitvavoClient(secrets.bitvavo_api_key, secrets.bitvavo_api_secret,
                         cfg.fees["maker_pct"], cfg.fees["taker_pct"])
    scope_all = "--all" in sys.argv[1:]
    config_hash = None if scope_all else config_fingerprint(cfg)
    result = analyze_vetos(feed, cfg, config_hash=config_hash)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
