from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradebot.decision import (
    Decision,
    DecisionEngine,
    FeeModel,
    Position,
    RiskManager,
    apply_second_opinion,
)
from tradebot.strategy import Candidate, MarketSnapshot

FEES = FeeModel(maker_pct=0.15, taker_pct=0.25, slippage_buffer_pct=0.10)
RISK_CFG = {"max_position_pct": 25.0, "max_open_positions": 3,
            "cooldown_hours_after_trade": 12, "daily_loss_cap_pct": 3.0,
            "paper_start_eur": 1000.0}
DEC_CFG = {"min_profit_pct": 0.50, "atr_stop_multiplier": 2.0,
           "reward_risk_ratio": 2.0, "use_llm_second_opinion": False,
           "llm_min_confidence": 0.6, "max_breakeven_win_rate": 0.50}


def snap(price=100.0, atr=1.0) -> MarketSnapshot:
    return MarketSnapshot("BTC-EUR", price, 101, 100, 40, 0.5, -0.1, atr, 98, 100, 1.0)


def candidate(action="buy", price=100.0, atr=1.0) -> Candidate:
    return Candidate("BTC-EUR", action, 4, ["test"], snap(price, atr))


def engine() -> DecisionEngine:
    return DecisionEngine(FEES, RiskManager(RISK_CFG), DEC_CFG)


def test_fee_model_round_trip():
    assert FEES.round_trip_pct() == 0.5
    assert FEES.min_edge_pct(0.5) == 1.1  # 0.5 fees + 0.1 slippage + 0.5 profit


# --- sizing-modi ------------------------------------------------------------

BUCKET_CFG = {"sizing": "bucket", "bucket_eur": 250.0, "max_position_pct": 25.0,
              "max_open_positions": 10, "cooldown_hours_after_trade": 12,
              "daily_loss_cap_pct": 3.0, "paper_start_eur": 1000.0}


def test_percent_mode_is_default_and_unchanged():
    rm = RiskManager(RISK_CFG)  # geen "sizing" -> percent
    assert rm.effective_max_positions(1000.0) == 3
    assert rm.effective_max_positions(5000.0) == 3  # vast
    assert rm.position_size_eur(1000.0, 1000.0) == 250.0  # 25%


def test_bucket_mode_scales_slots_with_capital():
    rm = RiskManager(BUCKET_CFG)
    assert rm.effective_max_positions(1000.0) == 4   # 1000/250
    assert rm.effective_max_positions(1249.0) == 4   # net onder de grens
    assert rm.effective_max_positions(1250.0) == 5   # extra slot
    assert rm.effective_max_positions(1500.0) == 6
    assert rm.effective_max_positions(9999.0) == 10  # geplafonneerd op max_open_positions


def test_bucket_mode_fixed_position_size():
    rm = RiskManager(BUCKET_CFG)
    assert rm.position_size_eur(2000.0, 2000.0) == 250.0  # vast bedrag, niet 25%
    assert rm.position_size_eur(2000.0, 120.0) == 120.0   # begrensd door vrije cash


def test_bucket_mode_can_open_respects_dynamic_max():
    rm = RiskManager(BUCKET_CFG)
    # Bij portfolio 1000 zijn er 4 slots; 4 open posities blokkeert de 5e.
    open_pos = [Position(f"M{i}-EUR", 1.0, 100.0, 98.0, 104.0,
                         datetime.now(timezone.utc)) for i in range(4)]
    ok, why = rm.can_open("NEW-EUR", open_pos, None, 1000.0, 0.0)
    assert not ok and "max open positions (4)" in why
    # Bij portfolio 1250 komt er een slot bij; de 5e mag dan wel.
    ok2, _ = rm.can_open("NEW-EUR", open_pos, None, 1250.0, 0.0)
    assert ok2


# --- LLM second opinion / shadow-mode ---------------------------------------

def _buy_decision() -> Decision:
    return Decision("BTC-EUR", "buy", "score 4", amount_quote_eur=250.0,
                    stop_loss=98.0, take_profit=104.0, details={"score": 4})


def _verdict(agree: bool, conf: float):
    return SimpleNamespace(agree=agree, confidence=conf, reasoning="near lower band",
                           provider="groq")


def test_binding_veto_blocks_buy():
    d = apply_second_opinion(_buy_decision(), _verdict(False, 0.8), 0.6, binding=True)
    assert d.action == "skip" and "LLM veto" in d.reason


def test_binding_low_confidence_blocks_buy():
    d = apply_second_opinion(_buy_decision(), _verdict(True, 0.4), 0.6, binding=True)
    assert d.action == "skip"


def test_agree_leaves_buy_untouched():
    d = apply_second_opinion(_buy_decision(), _verdict(True, 0.9), 0.6, binding=True)
    assert d.action == "buy" and "SHADOW" not in d.reason


def test_shadow_veto_keeps_buy_but_annotates():
    d = apply_second_opinion(_buy_decision(), _verdict(False, 0.8), 0.6, binding=False)
    assert d.action == "buy"
    assert "SHADOW-VETO genegeerd" in d.reason
    assert d.details["shadow_veto"].startswith("LLM veto")
    assert d.stop_loss == 98.0 and d.take_profit == 104.0  # niveaus behouden


def test_binding_none_is_conservative_skip():
    d = apply_second_opinion(_buy_decision(), None, 0.6, binding=True)
    assert d.action == "skip"


def test_shadow_none_keeps_buy():
    d = apply_second_opinion(_buy_decision(), None, 0.6, binding=False)
    assert d.action == "buy"


def test_breakeven_gate_blocks_a_setup_that_needs_an_implausible_hit_rate():
    """De optellende fee-gate is in v0.20.0 vervangen door de break-even-trefkans.

    Aanleiding: over twee jaar backtest blokkeerde die gate 0,0% van de kandidaten.
    Hij vroeg "ligt het koersdoel minstens 1,10% weg", en dat was altijd zo, want
    de verwachte beweging is ATR x 2 x r en dus 10 tot 15% op 4h-crypto. Hij toetste
    of de beweging groot genoeg was, niet of de verwachtingswaarde positief is.

    Hier: ATR 0,2% van de prijs vraagt een trefkans van (0,4 + 0,6) / (0,4 x 3) =
    83%. Dat is geen setup, dat is een loterij.
    """
    d = engine().evaluate_buy(candidate(atr=0.2), [], None, 1000, 1000, 0)
    assert d.action == "skip"
    assert "break-even-gate" in d.reason
    assert d.details["breakeven_win_rate"] > 0.80


def test_breakeven_gate_allows_a_plausible_setup():
    """ATR 1% van de prijs met r = 2,0 vraagt (2 + 0,6) / (2 x 3) = 43%. Haalbaar,
    dus de trade mag door."""
    d = engine().evaluate_buy(candidate(atr=1.0), [], None, 1000, 1000, 0)
    assert d.action == "buy"
    assert 0.40 < d.details["breakeven_win_rate"] < 0.50


def test_breakeven_win_rate_matches_the_design_document():
    """Formule uit docs/ontwerp-ev-gate.md §1, veralgemeend. De veelgenoemde 40% is
    de KOSTENLOZE asymptoot en dus altijd te soepel: met c = 0,60% ligt de lat op 44
    tot 52% bij een realistische ATR van 1 tot 3% van de prijs."""
    from tradebot.decision import breakeven_win_rate

    for atr_pct, verwacht in [(1.0, 0.52), (2.0, 0.46), (3.0, 0.44)]:
        p_ster = breakeven_win_rate(atr_pct, 2.0, 1.5, 0.60)
        assert p_ster == pytest.approx(verwacht, abs=0.005), (atr_pct, p_ster)
    # kostenloos -> de asymptoot van 40%
    assert breakeven_win_rate(2.0, 2.0, 1.5, 0.0) == pytest.approx(0.40)
    assert breakeven_win_rate(0.0, 2.0, 1.5, 0.60) is None


def test_old_fee_gate_numbers_are_still_reported():
    """`expected_pct` en `min_edge_pct` blijven in de details staan: het dashboard
    toont ze en de vergelijking met de oude meting moet leesbaar blijven."""
    d = engine().evaluate_buy(candidate(atr=1.0), [], None, 1000, 1000, 0)
    assert d.details["expected_pct"] > d.details["min_edge_pct"]


def test_gate_is_off_when_no_ceiling_is_configured():
    """Zonder `max_breakeven_win_rate` gedraagt de engine zich als vóór v0.20.0, dus
    een bestaande config verandert niet stil van gedrag."""
    from tradebot.decision import DecisionEngine

    zonder = DecisionEngine(FEES, RiskManager(RISK_CFG),
                            {k: v for k, v in DEC_CFG.items()
                             if k != "max_breakeven_win_rate"})
    d = zonder.evaluate_buy(candidate(atr=0.2), [], None, 1000, 1000, 0)
    assert d.action == "buy"


def test_levels_and_size_are_unchanged():
    d = engine().evaluate_buy(candidate(atr=1.0), [], None, 1000, 1000, 0)
    assert d.action == "buy"
    assert d.amount_quote_eur == 250.0  # 25% of 1000
    assert d.stop_loss == 98.0
    assert d.take_profit == 104.0


def test_no_signal_is_skip():
    d = engine().evaluate_buy(candidate(action="hold"), [], None, 1000, 1000, 0)
    assert d.action == "skip"


def test_max_positions_gate():
    now = datetime.now(timezone.utc)
    positions = [Position(f"M{i}-EUR", 1, 1, 1, 1, now) for i in range(3)]
    d = engine().evaluate_buy(candidate(), positions, None, 1000, 250, 0)
    assert d.action == "skip"
    assert "max open positions" in d.reason


def test_duplicate_market_gate():
    now = datetime.now(timezone.utc)
    d = engine().evaluate_buy(candidate(), [Position("BTC-EUR", 1, 1, 1, 1, now)],
                              None, 1000, 750, 0)
    assert d.action == "skip"
    assert "already open" in d.reason


def test_cooldown_gate():
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    d = engine().evaluate_buy(candidate(), [], recent, 1000, 1000, 0)
    assert d.action == "skip"
    assert "cooldown" in d.reason


def test_daily_loss_cap_gate():
    d = engine().evaluate_buy(candidate(), [], None, 1000, 1000, daily_pnl_eur=-50)
    assert d.action == "skip"
    assert "daily loss cap" in d.reason


# --- 4.2 (helft): maker/taker-asymmetrie in de fee-gate -------------------------

def test_round_trip_and_fee_gate_follow_the_broker_mode():
    """Besluit 4.2, eerste helft. `min_edge_pct` rekende altijd met 2x taker,
    terwijl `LiveBroker` een maker-entry (limit postOnly) en een market-exit doet.
    Live werd de fee-gate daardoor systematisch te streng gemeten.

    No-op voor fase 2 (paper vult beide benen als taker, dus 1,10% blijft 1,10%) en
    een correctie voor fase 3 (1,00%). Bewust NIET meegenomen: de scanner-versus-
    engine-divergentie op de spread, want die verandert wél welke kandidaten door de
    gate komen en daarmee de meetcohorte van alle vier de shadow-gates.
    """
    paper = FeeModel(0.15, 0.25, 0.10, entry_is_maker=False)
    live = FeeModel(0.15, 0.25, 0.10, entry_is_maker=True)

    assert paper.round_trip_pct() == 0.5          # taker + taker
    assert live.round_trip_pct() == 0.4           # maker + taker
    assert paper.min_edge_pct(0.5) == 1.1         # ongewijzigd t.o.v. v0.19.0
    assert live.min_edge_pct(0.5) == pytest.approx(1.0)


def test_fee_model_defaults_to_the_paper_assumption():
    """Zonder expliciete modus blijft het oude, conservatieve gedrag gelden."""
    assert FeeModel(0.15, 0.25, 0.10).round_trip_pct() == 0.5
