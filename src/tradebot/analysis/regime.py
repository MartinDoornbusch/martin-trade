"""Meting van de markt-brede regime-gate (shadow).

De regime-gate weert nieuwe entries als de proxy-markt (BTC) in down-trend
staat. In shadow-mode (`regime.binding=false`) blokkeert hij niet, dus elke
regime-down buy is een ECHTE paper-trade met bekende uitkomst. Daardoor is geen
counterfactual-reconstructie nodig zoals bij de LLM-veto: we lezen de
gerealiseerde round-trip-P&L direct.

Per regime-down entry:
  net < 0  -> gate zou verlies hebben voorkomen  (correct geweerd)
  net > 0  -> gate zou winst hebben weggesneden  (gemiste kans)

netto gate = vermeden verlies - gemiste winst. Positief = de gate voegt waarde
toe. Read-only: voert nooit orders uit.
"""
from __future__ import annotations

from .veto import (
    TARGET_RESOLVED,
    RoundTrip,
    _load_roundtrips_from_db,
    _summ,
    _to_ms,
    build_roundtrips,
    interval_seconds,
    params_from_config,
)


def _match_same_cycle(event_ms: int, market: str, roundtrips: list[RoundTrip],
                      window_ms: int) -> RoundTrip | None:
    """Koppel een regime-event aan de round-trip uit dezelfde cyclus.

    Anders dan bij de LLM-veto is het regime-event afgeleid van de SignalRow, die
    NA de trade-rij wordt weggeschreven. De buy ligt dus vlak vóór het event, niet
    erna. Daarom matchen we symmetrisch op de kleinste |buy - event| binnen het
    venster. De 12u-cooldown per markt garandeert dat er binnen het venster
    hooguit één entry ligt.
    """
    best: RoundTrip | None = None
    for rt in roundtrips:
        if rt.market != market or abs(rt.buy_ms - event_ms) > window_ms:
            continue
        if best is None or abs(rt.buy_ms - event_ms) < abs(best.buy_ms - event_ms):
            best = rt
    return best


def _load_regime_events_from_db() -> list[dict]:
    """Regime-down shadow-events: SignalRows met `details.shadow_regime`. Bindende
    regime-skips laten geen trade na en worden dus niet gemeten (terecht)."""
    from sqlalchemy import select

    from ..db import SignalRow, session
    with session() as s:
        rows = s.execute(select(SignalRow).order_by(SignalRow.ts.asc())).scalars().all()
    out: list[dict] = []
    for r in rows:
        details = r.details or {}
        if isinstance(details, dict) and details.get("shadow_regime"):
            out.append({"ts": r.ts, "market": r.market})
    return out


def analyze_regime(cfg, *, events: list[dict] | None = None,
                   trades: list[dict] | None = None,
                   match_window_candles: int = 1) -> dict:
    """Meet de netto gate-waarde van de regime-filter uit echte paper-uitkomsten.

    Injecteer `events` en/of `trades` om DB-toegang te omzeilen (tests).
    """
    p = params_from_config(cfg)
    if events is None:
        events = _load_regime_events_from_db()
    if trades is None:
        trades = _load_roundtrips_from_db()
    regime_cfg = getattr(cfg, "regime", {}) or {}
    base = {
        "error": None,
        "n_events": len(events),
        "n_resolved": 0,
        "n_unresolved": len(events),
        "target_resolved": TARGET_RESOLVED,
        "position_size_eur": round(p.position_size_eur, 2),
        "proxy_market": str(regime_cfg.get("proxy_market", "BTC-EUR")),
        "binding": bool(regime_cfg.get("binding", False)),
        "summary": None,
        "per_market": [],
    }
    if not events:
        return base

    roundtrips = build_roundtrips(trades)
    window_ms = interval_seconds(p.interval) * max(1, match_window_candles) * 1000
    matched: list[RoundTrip] = []
    for ev in events:
        rt = _match_same_cycle(_to_ms(ev["ts"]), ev["market"], roundtrips, window_ms)
        if rt is not None:
            matched.append(rt)

    base["n_resolved"] = len(matched)
    base["n_unresolved"] = len(events) - len(matched)
    if not matched:
        return base

    base["summary"] = _summ([rt.net_pct for rt in matched], p.position_size_eur)
    by_market: dict[str, list[float]] = {}
    for rt in matched:
        by_market.setdefault(rt.market, []).append(rt.net_pct)
    rows = []
    for market, vals in sorted(by_market.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        summ = _summ(vals, p.position_size_eur)
        if summ:
            rows.append({"group": market, **summ})
    base["per_market"] = rows
    return base


def main() -> None:  # pragma: no cover - CLI-gemak, niet in de testsuite
    import json

    from ..config import get_config
    print(json.dumps(analyze_regime(get_config()), indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
