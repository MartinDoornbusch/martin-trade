"""Generieke meting van een shadow-gate uit echte paper/live-uitkomsten.

Er draaien vier shadow-gates (LLM-veto, regime, breakeven-stop, chase-guard) en
ze stelden dreigend vier keer bijna dezelfde vraag met vier bijna identieke
modules. De romp is inderdaad gedeeld: events laden, koppelen aan een
afgewikkelde round-trip, samenvatten met 95%-Wilson en uitsplitsen per markt.
Wat verschilt, is per gate expliciet gemaakt in een `GateSpec`:

* de VRAAG. Entry-gates (regime, chase) vragen "de gate had deze buy geblokkeerd,
  wat deed die buy?". De breakeven-stop vraagt "de gate had hier geëxit, wat deed
  de positie daarna?". Andere rekensom, dus een pluggable `outcome`.
* de KOPPELING. Een entry-event ligt in dezelfde cyclus als de buy; een
  breakeven-event ligt ergens TUSSEN buy en sell van een lopende positie.
* de DEDUPLICATIE. Bewust per gate vastgelegd in plaats van impliciet in de
  analyzer. De breakeven-stop vuurt elke cyclus opnieuw zolang de koers onder de
  drempel hangt en moet dus gededupliceerd worden; regime en chase leveren in
  shadow hooguit één event per werkelijke buy, omdat de koop gewoon doorgaat en
  de volgende cyclus al op "position already open" strandt. Dat laatste is geen
  aanname maar vastgelegd in `tests/test_engine_cycle.py`.

Tekenafspraak, gelijk voor alle gates: de uitkomst is wat de gate zou hebben
VOORKOMEN. Negatief = de gate zou verlies hebben voorkomen (goed), positief = hij
zou winst hebben weggesneden (slecht). Netto gate = vermeden verlies min gemiste
winst. Zo blijft `_summ` uit veto.py bruikbaar voor alle vier.

Read-only: voert nooit orders uit.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .veto import (
    TARGET_RESOLVED,
    RoundTrip,
    VetoParams,
    _summ,
    _to_ms,
    build_roundtrips,
    interval_seconds,
    load_roundtrips_from_db,
    params_from_config,
)


@dataclass(frozen=True)
class GateSpec:
    """Alles wat één shadow-gate onderscheidt van de andere drie."""
    name: str
    details_key: str                 # sleutel in SignalRow.details
    label: str                       # menselijke omschrijving voor dashboard/CLI
    outcome: Callable[[dict, RoundTrip, VetoParams], float | None]
    match: str = "same_cycle"        # "same_cycle" | "during_position"
    dedup: bool = False              # eerste treffer per round-trip


# --- uitkomstfuncties ------------------------------------------------------

def entry_gate_outcome(event: dict, rt: RoundTrip, p: VetoParams) -> float | None:
    """Entry-gate: de gate had deze buy geblokkeerd, dus wat de buy deed is precies
    wat hij zou hebben voorkomen. De P&L is al netto fees."""
    return rt.net_pct


def breakeven_outcome(event: dict, rt: RoundTrip, p: VetoParams) -> float | None:
    """Exit-gate: verschil tussen wat de positie DEED en wat ze had gedaan als de
    breakeven-stop op zijn treffermoment had verkocht.

    Eenvoudiger dan bij de entry-gates: de prijs op signaalmoment en de werkelijke
    exitprijs zijn allebei bekend, dus er is geen counterfactual-reconstructie uit
    candles nodig. Beide benen dragen dezelfde round-trip kosten, dus die vallen
    tegen elkaar weg op de exit-fee na (zelfde tarief).

    Teken conform de afspraak: `werkelijk - hypothetisch`. Was doorhouden slechter
    dan uitstappen, dan is dat negatief en zou de gate verlies hebben voorkomen.
    """
    entry = float(event.get("entry_price") or 0.0)
    price = float(event.get("price") or 0.0)
    if entry <= 0 or price <= 0:
        return None
    hypothetisch = (price / entry - 1.0) * 100 - p.cost_pct
    return round(rt.net_pct - hypothetisch, 4)


# --- koppeling -------------------------------------------------------------

def _match_same_cycle(event_ms: int, market: str, roundtrips: list[RoundTrip],
                      window_ms: int) -> RoundTrip | None:
    """Entry-gate: koppel aan de round-trip uit dezelfde cyclus.

    Het event komt uit de SignalRow, die NA de trade-rij wordt weggeschreven, dus
    de buy ligt vlak vóór het event en niet erna. Daarom symmetrisch matchen op de
    kleinste |buy - event| binnen het venster. De cooldown per markt garandeert dat
    er binnen dat venster hooguit één entry ligt.
    """
    best: RoundTrip | None = None
    for rt in roundtrips:
        if rt.market != market or abs(rt.buy_ms - event_ms) > window_ms:
            continue
        if best is None or abs(rt.buy_ms - event_ms) < abs(best.buy_ms - event_ms):
            best = rt
    return best


def _match_during_position(event_ms: int, market: str, roundtrips: list[RoundTrip],
                           window_ms: int) -> RoundTrip | None:
    """Exit-gate: koppel aan de round-trip die op dat moment OPENSTOND."""
    for rt in roundtrips:
        if rt.market == market and rt.buy_ms <= event_ms <= rt.sell_ms:
            return rt
    return None


_MATCHERS = {"same_cycle": _match_same_cycle, "during_position": _match_during_position}


# --- events laden ----------------------------------------------------------

def load_events_from_db(details_key: str, mode: str | None = None,
                        gate_hash: str | None = None) -> list[dict]:
    """Shadow-events uit de SignalRows: rijen met `details[details_key]`.

    Bindende skips laten geen trade na en worden dus niet gemeten (terecht).

    `mode` scheidt paper- van live-historie. `gate_hash` beperkt tot één
    configuratie; rijen van vóór v0.20.0 hebben nog geen `details["gate_hash"]` en
    vallen dan buiten de gescopede meting, met `gate_hash=None` als `--all`-achtige
    ontsnapping naar de totaalmeting.
    """
    from sqlalchemy import select

    from ..db import SignalRow, session
    with session() as s:
        stmt = select(SignalRow).order_by(SignalRow.ts.asc())
        if mode is not None:
            stmt = stmt.where(SignalRow.mode == mode)
        rows = s.execute(stmt).scalars().all()
    out: list[dict] = []
    for r in rows:
        details = r.details or {}
        if not isinstance(details, dict) or not details.get(details_key):
            continue
        if gate_hash is not None and (details.get("gate_hash") or {}).get(
                _gate_of(details_key)) != gate_hash:
            continue
        out.append({"ts": r.ts, "market": r.market, **details})
    return out


def _gate_of(details_key: str) -> str:
    return details_key.removeprefix("shadow_")


# --- kern ------------------------------------------------------------------

def analyze_shadow_gate(cfg, spec: GateSpec, *, events: list[dict] | None = None,
                        trades: list[dict] | None = None, mode: str | None = None,
                        gate_hash: str | None = None,
                        match_window_candles: int = 1) -> dict:
    """Meet de netto gate-waarde van één shadow-gate uit echte uitkomsten.

    Injecteer `events` en/of `trades` om DB-toegang te omzeilen (tests).
    """
    p = params_from_config(cfg)
    if events is None:
        events = load_events_from_db(spec.details_key, mode, gate_hash)
    if trades is None:
        trades = load_roundtrips_from_db(mode)
    base = {
        "error": None,
        "gate": spec.name,
        "label": spec.label,
        "n_events": len(events),
        "n_resolved": 0,
        "n_unresolved": len(events),
        "n_deduped": 0,
        "target_resolved": TARGET_RESOLVED,
        "position_size_eur": round(p.position_size_eur, 2),
        "config_hash": gate_hash,
        "config_scope": "current" if gate_hash else "all",
        "summary": None,
        "per_market": [],
    }
    if not events:
        return base

    roundtrips = build_roundtrips(trades)
    window_ms = interval_seconds(p.interval) * max(1, match_window_candles) * 1000
    matcher = _MATCHERS[spec.match]

    gekoppeld: list[tuple[dict, RoundTrip]] = []
    for ev in events:
        rt = matcher(_to_ms(ev["ts"]), ev["market"], roundtrips, window_ms)
        if rt is not None:
            gekoppeld.append((ev, rt))

    if spec.dedup:
        eerste: dict[tuple[str, int], tuple[dict, RoundTrip]] = {}
        for ev, rt in gekoppeld:
            sleutel = (rt.market, rt.buy_ms)
            if sleutel not in eerste or _to_ms(ev["ts"]) < _to_ms(eerste[sleutel][0]["ts"]):
                eerste[sleutel] = (ev, rt)
        base["n_deduped"] = len(gekoppeld) - len(eerste)
        gekoppeld = list(eerste.values())

    waarden: list[tuple[str, float]] = []
    for ev, rt in gekoppeld:
        val = spec.outcome(ev, rt, p)
        if val is not None:
            waarden.append((rt.market, val))

    base["n_resolved"] = len(waarden)
    base["n_unresolved"] = len(events) - len(waarden) - base["n_deduped"]
    if not waarden:
        return base

    base["summary"] = _summ([v for _, v in waarden], p.position_size_eur)
    per_markt: dict[str, list[float]] = {}
    for market, val in waarden:
        per_markt.setdefault(market, []).append(val)
    rijen = []
    for market, vals in sorted(per_markt.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        summ = _summ(vals, p.position_size_eur)
        if summ:
            rijen.append({"group": market, **summ})
    base["per_market"] = rijen
    return base
