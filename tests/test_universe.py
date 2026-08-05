"""Tests voor auto-fill-selectie en de cluster-cap-correlatiehelper (v0.17.0).
Puur en deterministisch, zonder feed of DB."""
from tradebot.decision import correlated_positions
from tradebot.scanner import select_auto_fill


def _r(market, score=3, needed=3, fee_ok=True, exp=3.0, req=1.1):
    return {"market": market, "score": score, "score_needed": needed,
            "fee_ok": fee_ok, "expected_move_pct": exp, "required_pct": req}


# --- select_auto_fill -------------------------------------------------------

def test_auto_fill_picks_only_gate_passers():
    results = [
        _r("A-EUR", score=3, fee_ok=True),      # ok
        _r("B-EUR", score=2, fee_ok=True),      # score < needed -> uit
        _r("C-EUR", score=3, fee_ok=False),     # fee-gate faalt -> uit
        _r("D-EUR", score=3, fee_ok=True),      # ok
    ]
    picks = select_auto_fill(results, exclude=set(), want=5)
    assert picks == ["A-EUR", "D-EUR"]


def test_auto_fill_excludes_pinned_and_open():
    results = [_r("A-EUR"), _r("B-EUR"), _r("C-EUR")]
    picks = select_auto_fill(results, exclude={"A-EUR"}, want=5)
    assert "A-EUR" not in picks and picks == ["B-EUR", "C-EUR"]


def test_auto_fill_ranks_by_score_then_edge():
    results = [
        _r("LOW-EUR", score=3, exp=2.0, req=1.1),   # edge 0.9
        _r("HIGH-EUR", score=4, exp=2.0, req=1.1),  # hoogste score wint
        _r("MIDEDGE-EUR", score=3, exp=5.0, req=1.1),  # score 3, grootste edge
    ]
    picks = select_auto_fill(results, exclude=set(), want=3)
    assert picks == ["HIGH-EUR", "MIDEDGE-EUR", "LOW-EUR"]


def test_auto_fill_respects_want_limit():
    results = [_r(f"{c}-EUR") for c in "ABCDE"]
    assert len(select_auto_fill(results, set(), want=2)) == 2
    assert select_auto_fill(results, set(), want=0) == []


# --- correlated_positions (cluster-cap input) -------------------------------

# 20 niet-monotone closes zodat de returns variantie hebben (pearson vereist >= 10).
BASE = [100, 101, 103, 102, 104, 106, 105, 107, 109, 108,
        110, 112, 111, 113, 115, 114, 116, 118, 117, 119]


def _scaled(factor):
    return [x * factor for x in BASE]  # identieke returns -> correlatie ~1


def _anti():
    """Reeks met genegeerde returns -> correlatie ~ -1."""
    out = [100.0]
    for i in range(1, len(BASE)):
        r = BASE[i] / BASE[i - 1] - 1
        out.append(out[-1] * (1 - r))
    return out


def test_correlated_positions_flags_high_correlation():
    others = {"SAME-EUR": _scaled(3.0), "INV-EUR": _anti()}
    out = correlated_positions(BASE, others, max_corr=0.85, lookback=60)
    markets = [m for m, _ in out]
    assert "SAME-EUR" in markets
    assert "INV-EUR" not in markets


def test_cluster_cap_logic_blocks_at_k():
    # Twee gecorreleerde open posities -> bij K=2 wordt de kandidaat geweigerd.
    others = {"P1-EUR": _scaled(2.0), "P2-EUR": _scaled(5.0)}
    out = correlated_positions(BASE, others, max_corr=0.85, lookback=60)
    assert len(out) == 2  # beide > drempel -> cluster vol bij K=2
