"""Alfa van de lopende run: rendement min wat dezelfde blootstelling passief had opgeleverd.

Waarom dit er is. Het overzicht opende met P&L, win-rate en "sinds start", en die
drie lezen als strategievalidatie. Bij bucket-sizing met auto-fill staan alle slots
vrijwel altijd vol, dus de run is grotendeels belegd en een positief rendement is
eerst bèta. Fase 2 is precies daarop beslist: niet het rendement maar de alfa, en die
was in de tweejaarskalibratie nul tot negatief. Dit is dezelfde maat, nu op de run
zelf, zodat criterium (3) van de go/no-go ook op paper te volgen is.

Definitie, gelijk aan `optimizer.py` (`alpha_train`/`alpha_test`):

    alfa = rendement_run - gemiddelde_blootstelling x rendement_markt

* rendement_run: laatste equity-snapshot gedeeld door de eerste, min 1.
* blootstelling: per snapshot (totaal - cash) / totaal, gemiddeld over de snapshots.
  De snapshots komen elke 6 uur, dus dat is een tijdgewogen gemiddelde.
* markt: een gelijkgewogen mandje van ALLE markten die de run verhandeld heeft, over
  hetzelfde venster. Bewust niet BTC alleen: de bot handelt vooral alts en die bewegen
  harder dan BTC. Tegen BTC zou alt-bèta in een stijgende markt als alfa verschijnen,
  en dat is precies het valse positief dat deze maat moet voorkomen. BTC staat er als
  referentie naast.

Bekende beperkingen, niet opgelost maar benoemd:

* Het mandje is ex post samengesteld (markten die de scanner later koos, tellen vanaf
  het begin mee). Zelfde keuze als de backtester, die ook over de verhandelde markten
  mat. Het maakt de lat niet systematisch lager.
* Eerste-orde-benadering, net als in de optimizer: timing (belegd zijn wanneer de markt
  stijgt) telt mee als alfa. Dat is bedoeld, want timing is wat een regime-filter claimt.
* Eén venster geeft één getal zonder interval. De marktrichting en de grootste daling
  van het mandje staan erbij, zodat zichtbaar is of het venster eenzijdig was.

Read-only: voert nooit orders uit.
"""
from __future__ import annotations

import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone

from ..exchange import Candle, ExchangeAdapter
from .veto import _to_ms, interval_seconds

REFERENCE_MARKET = "BTC-EUR"
INTERVAL = "4h"


@dataclass(frozen=True)
class EquityPoint:
    ts_ms: int
    total_eur: float
    cash_eur: float


# --- pure rekenwerk -----------------------------------------------------------

def exposure_pct(points: list[EquityPoint]) -> float:
    """Gemiddelde blootstelling in procenten: (totaal - cash) / totaal per snapshot."""
    fracties = [(p.total_eur - p.cash_eur) / p.total_eur for p in points if p.total_eur > 0]
    return round(sum(fracties) / len(fracties) * 100, 2) if fracties else 0.0


def _close_at(candles: list[Candle], ts_ms: int, keys: list[int] | None = None) -> float | None:
    """Slotkoers van de laatste candle die op of vóór `ts_ms` begon (forward fill).
    `candles` oplopend gesorteerd, zoals `get_candles_history` ze levert."""
    keys = keys if keys is not None else [c.ts for c in candles]
    i = bisect_right(keys, ts_ms)
    return candles[i - 1].close if i else None


def basket_path(candles_by_market: dict[str, list[Candle]], start_ms: int,
                end_ms: int) -> tuple[list[tuple[int, float]], list[str]]:
    """Gelijkgewogen index (start = 1,0) over [start, end], plus de meegenomen markten.

    Een markt zonder koers op het startmoment valt weg: je kunt hem niet op dag één
    gekocht hebben. Tussenliggende gaten worden vooruit gevuld.
    """
    keys = {m: [c.ts for c in cs] for m, cs in candles_by_market.items()}
    basis: dict[str, float] = {}
    for m, cs in candles_by_market.items():
        c0 = _close_at(cs, start_ms, keys[m])
        if c0 and c0 > 0:
            basis[m] = c0
    if not basis:
        return [], []
    tijdlijn = sorted({t for m in basis for t in keys[m] if start_ms <= t <= end_ms}
                      | {start_ms, end_ms})
    pad = []
    for ts in tijdlijn:
        waarden = [(_close_at(candles_by_market[m], ts, keys[m]) or c0) / c0
                   for m, c0 in basis.items()]
        pad.append((ts, sum(waarden) / len(waarden)))
    return pad, sorted(basis)


def max_decline_pct(path: list[tuple[int, float]]) -> float:
    """Grootste daling van piek naar dal in procenten (positief getal)."""
    piek, slechtst = 0.0, 0.0
    for _, v in path:
        piek = max(piek, v)
        if piek > 0:
            slechtst = max(slechtst, (piek - v) / piek)
    return round(slechtst * 100, 2)


def compute_alfa(points: list[EquityPoint], candles_by_market: dict[str, list[Candle]],
                 reference: list[Candle] | None = None) -> dict:
    """Alfa van de run over het venster van de equity-snapshots. Puur: geen DB, geen netwerk."""
    uit: dict = {"error": None, "n_snapshots": len(points), "alfa_pp": None}
    if len(points) < 2:
        uit["error"] = "te weinig equity-snapshots (minstens twee nodig)"
        return uit
    punten = sorted(points, key=lambda p: p.ts_ms)
    start, eind = punten[0], punten[-1]
    if start.total_eur <= 0:
        uit["error"] = "eerste snapshot heeft geen waarde"
        return uit

    run_pct = (eind.total_eur / start.total_eur - 1) * 100
    expo = exposure_pct(punten)
    pad, markten = basket_path(candles_by_market, start.ts_ms, eind.ts_ms)
    uit.update({
        "start": _iso(start.ts_ms),
        "end": _iso(eind.ts_ms),
        "days": round((eind.ts_ms - start.ts_ms) / 86_400_000, 1),
        "run_return_pct": round(run_pct, 2),
        "exposure_pct": expo,
        "benchmark_markets": markten,
        "skipped_markets": sorted(set(candles_by_market) - set(markten)),
    })
    if not pad:
        uit["error"] = "geen koersen voor het vergelijkingsmandje in dit venster"
        return uit

    markt_pct = (pad[-1][1] - 1) * 100
    passief = expo / 100 * markt_pct
    uit.update({
        "benchmark_return_pct": round(markt_pct, 2),
        "benchmark_max_decline_pct": max_decline_pct(pad),
        "benchmark_direction": "stijgend" if markt_pct >= 0 else "dalend",
        "passive_expected_pct": round(passief, 2),
        "alfa_pp": round(run_pct - passief, 2),
    })
    if reference:
        r0, r1 = _close_at(reference, start.ts_ms), _close_at(reference, eind.ts_ms)
        if r0 and r1:
            uit["reference_market"] = REFERENCE_MARKET
            uit["reference_return_pct"] = round((r1 / r0 - 1) * 100, 2)
    return uit


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# --- DB en Bitvavo ------------------------------------------------------------

def load_equity_from_db() -> list[EquityPoint]:
    """Alle equity-snapshots, oudste eerst.

    `EquityRow` heeft geen mode-kolom. Zolang er alleen paper draait is dat geen
    probleem; bij de omschakeling naar live moet de run hier een eigen startpunt
    krijgen, anders loopt de paper-historie mee in de live-alfa.
    """
    from sqlalchemy import select

    from ..db import EquityRow, session
    with session() as s:
        rows = s.execute(select(EquityRow).order_by(EquityRow.ts.asc())).scalars().all()
    # SQLite geeft de tijdzone niet terug; opgeslagen is UTC (`db.utcnow`). Expliciet
    # zetten, anders hangt de uitlijning op de candles af van de TZ van de container.
    return [EquityPoint(_to_ms(r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)),
                        float(r.total_eur), float(r.cash_eur)) for r in rows]


def traded_markets(mode: str | None) -> list[str]:
    """Elke markt waarin de run ooit een buy deed."""
    from sqlalchemy import select

    from ..db import TradeRow, session
    with session() as s:
        stmt = select(TradeRow.market).where(TradeRow.side == "buy").distinct()
        if mode is not None:
            stmt = stmt.where(TradeRow.mode == mode)
        return sorted(s.execute(stmt).scalars().all())


def analyze_alfa(adapter: ExchangeAdapter, *, mode: str | None = None,
                 points: list[EquityPoint] | None = None,
                 markets: list[str] | None = None) -> dict:
    """Haalt snapshots, verhandelde markten en candles op en rekent `compute_alfa`.

    Eén candle-verzoek per markt (4h, het venster plus marge). Injecteer `points` en
    `markets` om de DB te omzeilen; het netwerk loopt via `adapter`.
    """
    points = load_equity_from_db() if points is None else points
    markets = traded_markets(mode) if markets is None else markets
    if len(points) < 2:
        return compute_alfa(points, {})
    sec = interval_seconds(INTERVAL)
    span_s = max(0, int(time.time()) - min(p.ts_ms for p in points) // 1000)
    nodig = span_s // sec + 6
    candles: dict[str, list[Candle]] = {}
    for m in markets:
        try:
            candles[m] = adapter.get_candles_history(m, INTERVAL, nodig)
        except Exception:  # noqa: BLE001 - één markt mag de meting niet breken
            candles[m] = []
    try:
        ref = candles.get(REFERENCE_MARKET) or adapter.get_candles_history(
            REFERENCE_MARKET, INTERVAL, nodig)
    except Exception:  # noqa: BLE001
        ref = None
    uit = compute_alfa(points, candles, ref)
    uit["mode"] = mode
    return uit
