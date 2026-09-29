"""Meetweergave en alfa van de run (v0.24.0).

Aanleiding: een review van de gate-status op 2026-09-27 vond vier weergavefouten en
een ontbrekende maat. Elke test hieronder pint er één vast.

| Bevinding | Test |
|-----------|------|
| precisie als halfbreedte rond de puntschatting (1/1 werd "100% ±39,7") | `test_wilson_interval_*` |
| per-markt-rijen met n=1 nodigden uit tot patroonzoeken | `test_small_groups_are_pooled_*` |
| "events" telde ruwe guard-rijen (1332) naast buys (13) | `test_breakeven_counts_positions_not_rows` |
| LLM-veto-tegel telde alle calls ooit, naast een gate met 0 events | `test_stats_veto_rate_is_scoped_to_the_current_cohort` |
| rendement van de run zonder alfa | `test_alfa_*` |
"""
import pytest
from fastapi.testclient import TestClient

from tests.test_shadow_gates import START, STEP_MS, make_cfg, roundtrip
from tradebot.analysis import analyze_breakeven
from tradebot.analysis.alfa import (
    EquityPoint,
    analyze_alfa,
    basket_path,
    compute_alfa,
    exposure_pct,
    max_decline_pct,
)
from tradebot.analysis.veto import (
    POOLED_LABEL,
    _summ,
    _wilson_half_width,
    _wilson_interval,
    pool_small_groups,
)
from tradebot.exchange import Candle

H4 = 4 * 3600 * 1000


# --- Wilson als interval ----------------------------------------------------------

def test_wilson_interval_matches_the_dashboard_case():
    """De breakeven-stand van 2026-09-27: 6/14. Ruwe proportie 42,9%, maar het
    interval ligt rond het Wilson-midden (44,4%), niet rond 42,9%."""
    lo, hi = _wilson_interval(6, 14)
    assert (lo, hi) == (21.4, 67.4)
    assert round((lo + hi) / 2, 1) != round(6 / 14 * 100, 1)
    assert round(hi - lo, 1) == pytest.approx(2 * _wilson_half_width(6, 14), abs=0.2)


def test_wilson_interval_stays_within_zero_and_hundred():
    """De oude weergave gaf bij 1/1 een bovengrens van 139,7% en bij 0/1 een
    ondergrens van -39,7%. Het interval zelf blijft binnen [0, 100]."""
    assert _wilson_interval(1, 1) == (20.7, 100.0)
    assert _wilson_interval(0, 1) == (0.0, 79.3)
    assert _wilson_interval(0, 0) is None


def test_summary_carries_the_interval():
    s = _summ([-1.0, 2.0, 3.0, -4.0, 5.0], 250.0)
    assert (s["precision_lo_pct"], s["precision_hi_pct"]) == _wilson_interval(2, 5)
    assert "precision_margin_pp" in s  # export en CLI lezen hem nog


# --- kleine groepen poolen ----------------------------------------------------------

def test_small_groups_are_pooled_below_five():
    groups = {"A": [1.0] * 5, "B": [-1.0] * 4, "C": [2.0], "D": [-2.0, 1.0]}
    rijen = pool_small_groups(groups, 250.0)
    assert [r["group"] for r in rijen] == ["A", POOLED_LABEL]
    assert rijen[1]["n"] == 7
    assert rijen[1]["pooled_groups"] == 3


def test_small_groups_are_pooled_without_losing_totals():
    """Poolen verschuift alleen de weergave: de som over de rijen blijft gelijk aan
    de samenvatting over alles."""
    groups = {"A": [1.0] * 6, "B": [-3.0], "C": [2.0, -1.0]}
    rijen = pool_small_groups(groups, 250.0)
    alles = _summ([v for vals in groups.values() for v in vals], 250.0)
    assert sum(r["n"] for r in rijen) == alles["n"]
    assert round(sum(r["net_gate_eur"] for r in rijen), 2) == alles["net_gate_eur"]


# --- posities in plaats van ruwe rijen ---------------------------------------------

def test_breakeven_counts_positions_not_rows(memory_db):
    """Vier treffers op een gesloten positie plus drie op een nog open positie in een
    andere markt: dat zijn twee posities, waarvan één open, en zeven ruwe rijen."""
    gesloten = [{"ts": START + i * STEP_MS // 4, "market": "A-EUR",
                 "shadow_breakeven": "treffer", "entry_price": 100.0, "price": 100.6}
                for i in range(1, 5)]
    open_ = [{"ts": START + 30 * STEP_MS + i, "market": "B-EUR",
              "shadow_breakeven": "treffer", "entry_price": 50.0, "price": 50.3}
             for i in range(3)]
    d = analyze_breakeven(make_cfg(), events=gesloten + open_, trades=roundtrip(pnl=-4.0))

    assert d["n_events"] == 7
    assert d["n_positions"] == 2
    assert d["n_open_positions"] == 1
    assert d["n_resolved"] == 1


# --- gescopede veto-rate -----------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    from tradebot import db
    from tradebot.web import app

    db._engine = db._Session = None
    db.init_db(f"sqlite:///{tmp_path / 'tradebot.db'}")
    yield TestClient(app)
    db._engine = db._Session = None


def test_stats_veto_rate_is_scoped_to_the_current_cohort(client):
    """Tot v0.23.1 telde de tegel elke call ooit (100% op 121) terwijl de veto-gate
    op de huidige cohorte 0 events had. Nu dezelfde scope als de gate-meting."""
    from tradebot.config import gate_fingerprint, get_config
    from tradebot.db import LLMCallRow, session

    huidig = gate_fingerprint(get_config(), "veto")
    with session() as s:
        for _ in range(3):
            s.add(LLMCallRow(provider="groq", model="oud", verdict="veto", config_hash="oudehash"))
        s.add(LLMCallRow(provider="groq", model="nieuw", verdict="veto", config_hash=huidig))
        s.add(LLMCallRow(provider="groq", model="nieuw", verdict="agree", config_hash=huidig))
        s.commit()

    body = client.get("/api/stats").json()
    assert body["llm_calls"] == 2
    assert body["llm_veto_rate_pct"] == 50.0
    assert body["llm_calls_all"] == 5
    assert body["llm_veto_rate_all_pct"] == 80.0


# --- alfa van de run ----------------------------------------------------------------

def reeks(closes, start=START):
    return [Candle(start + i * H4, c, c, c, c, 1.0) for i, c in enumerate(closes)]


def punten(totals, cash):
    return [EquityPoint(START + i * H4, t, c) for i, (t, c) in enumerate(zip(totals, cash, strict=True))]


def test_alfa_is_zero_for_a_fully_invested_run_that_tracks_the_market():
    """Volledig belegd en precies de markt gevolgd: rendement is bèta, alfa is nul."""
    pts = punten([1000, 1050, 1100, 1200], [0, 0, 0, 0])
    d = compute_alfa(pts, {"A-EUR": reeks([100, 105, 110, 120])})
    assert d["run_return_pct"] == 20.0
    assert d["exposure_pct"] == 100.0
    assert d["alfa_pp"] == 0.0


def test_alfa_is_negative_when_a_positive_run_lags_its_exposure():
    """De situatie die het overzicht verbergt: de run staat +10%, maar was 90%
    belegd in een markt die +20% deed. Passief was +18% te verwachten, dus alfa is
    -8 punt terwijl P&L en 'sinds start' groen staan."""
    pts = punten([1000, 1100], [100, 110])
    d = compute_alfa(pts, {"A-EUR": reeks([100, 120])})
    assert d["run_return_pct"] == 10.0
    assert d["exposure_pct"] == 90.0
    assert d["passive_expected_pct"] == 18.0
    assert d["alfa_pp"] == -8.0
    assert d["benchmark_direction"] == "stijgend"


def test_alfa_benchmark_is_an_equal_weight_basket_not_btc():
    """Twee alts (+40% en 0%) tegen BTC +5%: het mandje doet +20%, BTC staat er
    alleen als referentie naast. Tegen BTC zou alt-bèta als alfa verschijnen."""
    pts = punten([1000, 1200], [0, 0])
    d = compute_alfa(pts, {"A-EUR": reeks([10, 14]), "B-EUR": reeks([5, 5])},
                     reference=reeks([100, 105]))
    assert d["benchmark_return_pct"] == 20.0
    assert d["reference_return_pct"] == 5.0
    assert d["alfa_pp"] == 0.0


def test_alfa_skips_a_market_without_a_price_at_the_start():
    pts = punten([1000, 1000], [1000, 1000])
    later = reeks([7, 8], start=START + 10 * H4)
    pad, markten = basket_path({"A-EUR": reeks([10, 10]), "L-EUR": later}, START, START + H4)
    assert markten == ["A-EUR"]
    assert pad[-1][1] == 1.0
    d = compute_alfa(pts, {"A-EUR": reeks([10, 10]), "L-EUR": later})
    assert d["skipped_markets"] == ["L-EUR"]


def test_alfa_reports_the_largest_decline_of_the_basket():
    """Of het venster eenzijdig was, bepaalt hoeveel het getal waard is (fase 2-les)."""
    assert max_decline_pct([(0, 1.0), (1, 1.2), (2, 0.9), (3, 1.1)]) == 25.0
    assert exposure_pct(punten([1000, 1000], [250, 750])) == 50.0


def test_alfa_needs_two_snapshots():
    d = compute_alfa(punten([1000], [1000]), {})
    assert d["alfa_pp"] is None
    assert "snapshots" in d["error"]


class FakeAdapter:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get_candles_history(self, market, interval, total, end_ms=None):
        self.calls.append((market, interval))
        if market not in self.data:
            raise RuntimeError("onbekende markt")
        return self.data[market]


def test_analyze_alfa_fetches_one_series_per_market_and_survives_a_failure():
    """Eén markt die faalt breekt de meting niet; BTC wordt als referentie gehaald,
    ook als de run er nooit in handelde."""
    pts = punten([1000, 1100], [0, 0])
    ad = FakeAdapter({"A-EUR": reeks([10, 11]), "BTC-EUR": reeks([100, 104])})
    d = analyze_alfa(ad, points=pts, markets=["A-EUR", "WEG-EUR"], mode="paper")
    assert d["benchmark_markets"] == ["A-EUR"]
    assert d["skipped_markets"] == ["WEG-EUR"]
    assert d["reference_return_pct"] == 4.0
    assert d["alfa_pp"] == 0.0
    assert {m for m, _ in ad.calls} == {"A-EUR", "WEG-EUR", "BTC-EUR"}
    assert all(i == "4h" for _, i in ad.calls)
