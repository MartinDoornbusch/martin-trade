"""Tests op de generieke shadow-gate-meting (code review ronde 2, blok 4).

Vier gates delen één romp (events laden, koppelen, Wilson, uitsplitsing per
markt); wat per gate verschilt staat expliciet in een `GateSpec`. Deze tests
bewaken vooral dat die verschillen expliciet BLIJVEN.

| Punt | Test |
|------|------|
| 4.1 breakeven had geen analysemodule | `test_breakeven_gate_measures_hold_versus_exit` |
| 4.2 treffer werd elke cyclus opnieuw gelogd | `test_breakeven_deduplicates_to_the_first_hit_per_position` |
| 4.2 dedup is per gate, geen eigenschap van de analyzer | `test_entry_gates_do_not_deduplicate` |
| 4.3 netto gate zonder counterfactual | `test_breakeven_outcome_needs_no_candles` |
| tekenafspraak gelijk voor alle gates | `test_sign_convention_is_shared` |
| 5.2 mode-filter | `test_events_and_trades_are_filtered_by_mode` |
| scoping op de per-gate config-hash | `test_events_are_scoped_on_the_gate_hash` |
"""
from types import SimpleNamespace

from tradebot.analysis import analyze_breakeven, analyze_chase, analyze_regime
from tradebot.analysis.shadow_gate import (
    breakeven_outcome,
    entry_gate_outcome,
    load_events_from_db,
)
from tradebot.analysis.veto import load_roundtrips_from_db, params_from_config
from tradebot.db import SignalRow, TradeRow, session

STEP_MS = 4 * 3600 * 1000
START = 1_700_000_000_000


def make_cfg(**over) -> SimpleNamespace:
    base = dict(
        strategy={"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "atr_period": 14,
                  "max_chase_atr": 0.5, "chase_guard_binding": False},
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        decision={"atr_stop_multiplier": 2.0, "reward_risk_ratio": 1.5,
                  "min_profit_pct": 0.50},
        risk={"paper_start_eur": 1000.0, "max_position_pct": 25.0},
        schedule={"candle_interval": "4h", "candle_limit": 200},
        regime={"enabled": True, "proxy_market": "BTC-EUR", "binding": False},
        exits={"breakeven_stop": {"enabled": True, "binding": False,
                                  "trigger_atr": 1.0, "offset_pct": 0.55}},
        universe={"auto_fill": True},
    )
    base.update(over)
    return SimpleNamespace(**base)


def trade(ts, market, side, amount, price, pnl=0.0, reason=""):
    return {"ts": ts, "market": market, "side": side, "amount": amount,
            "price": price, "pnl_eur": pnl, "reason": reason}


def roundtrip(market="A-EUR", buy_ts=START, sell_ts=START + 5 * STEP_MS,
              entry=100.0, pnl=-4.0):
    return [trade(buy_ts, market, "buy", 1.0, entry),
            trade(sell_ts, market, "sell", 1.0, entry + pnl, pnl=pnl, reason="stop loss")]


# --- 4.1 en 4.3: de breakeven-meting -------------------------------------------

def test_breakeven_outcome_needs_no_candles():
    """Punt 4.3: prijs op signaalmoment en werkelijke exitprijs zijn allebei bekend,
    dus de netto gate is (hypothetische exit) min (werkelijke exit) zonder
    counterfactual-reconstructie uit candles."""
    p = params_from_config(make_cfg())
    rt = SimpleNamespace(market="A-EUR", buy_ms=START, sell_ms=START + STEP_MS,
                         net_pct=-4.0, exit="stop")
    # Uitstappen op 100,55 tegen entry 100 = +0,55% bruto, min 0,60% kosten = -0,05%.
    # Werkelijk werd het -4%. Verschil -3,95: doorhouden was slechter, dus de gate
    # zou verlies hebben voorkomen (negatief teken).
    uit = breakeven_outcome({"entry_price": 100.0, "price": 100.55}, rt, p)
    assert uit < 0
    assert round(uit, 2) == -3.95


def test_breakeven_gate_measures_hold_versus_exit(memory_db):
    """Punt 4.1: er was geen analysemodule, geen endpoint en geen dashboardkaart,
    dus de go/no-go-regel was voor deze gate niet uit te rekenen."""
    events = [{"ts": START + STEP_MS, "market": "A-EUR", "shadow_breakeven": "treffer",
               "entry_price": 100.0, "price": 100.55}]
    d = analyze_breakeven(make_cfg(), events=events, trades=roundtrip(pnl=-4.0))

    assert d["gate"] == "breakeven"
    assert d["n_events"] == 1
    assert d["n_resolved"] == 1
    assert d["binding"] is False
    assert d["summary"]["n_avoided"] == 1          # doorhouden was slechter
    assert d["summary"]["net_gate_eur"] > 0        # gate voegt waarde toe


def test_breakeven_gate_can_also_cut_profit(memory_db):
    """Andersom moet net zo goed zichtbaar zijn: uitstappen op break-even terwijl de
    positie daarna alsnog het target haalde."""
    events = [{"ts": START + STEP_MS, "market": "A-EUR", "shadow_breakeven": "treffer",
               "entry_price": 100.0, "price": 100.55}]
    d = analyze_breakeven(make_cfg(), events=events,
                          trades=roundtrip(pnl=8.0))
    assert d["summary"]["n_missed"] == 1
    assert d["summary"]["net_gate_eur"] < 0


# --- 4.2: deduplicatie is per gate vastgelegd ----------------------------------

def test_breakeven_deduplicates_to_the_first_hit_per_position(memory_db):
    """Punt 4.2: de treffer wordt elke cyclus opnieuw gelogd zolang de koers onder de
    drempel hangt, dus een positie die vier cycli onder de drempel hangt levert vier
    events op. Gededupliceerd op de eerste treffer per positie."""
    events = [{"ts": START + i * STEP_MS // 4, "market": "A-EUR",
               "shadow_breakeven": "treffer", "entry_price": 100.0,
               "price": 100.55 + i * 0.01}
              for i in range(1, 5)]
    d = analyze_breakeven(make_cfg(), events=events, trades=roundtrip(pnl=-4.0))

    assert d["n_events"] == 4
    assert d["n_deduped"] == 3
    assert d["n_resolved"] == 1
    assert d["summary"]["n"] == 1


def test_entry_gates_do_not_deduplicate(memory_db):
    """Dedup is bewust een eigenschap van de GateSpec en niet van de analyzer.

    Regime en chase leveren in shadow hooguit één event per werkelijke buy: de koop
    gaat gewoon door, dus de volgende cyclus strandt de kandidaat al op "position
    already open". Zouden ze toch dedupliceren, dan zouden twee losse entries in
    dezelfde markt als één meting tellen.
    """
    from tradebot.analysis.chase import SPEC as CHASE
    from tradebot.analysis.regime import SPEC as REGIME
    assert REGIME.dedup is False
    assert CHASE.dedup is False

    events = [{"ts": START, "market": "A-EUR", "shadow_regime": "risk-off"},
              {"ts": START + 20 * STEP_MS, "market": "A-EUR", "shadow_regime": "risk-off"}]
    trades = roundtrip(buy_ts=START, sell_ts=START + 2 * STEP_MS, pnl=-4.0) + \
        roundtrip(buy_ts=START + 20 * STEP_MS, sell_ts=START + 22 * STEP_MS, pnl=-4.0)
    d = analyze_regime(make_cfg(), events=events, trades=trades)

    assert d["n_deduped"] == 0
    assert d["n_resolved"] == 2


def test_sign_convention_is_shared():
    """Eén tekenafspraak voor alle gates: de uitkomst is wat de gate zou hebben
    VOORKOMEN, dus negatief = verlies voorkomen (goed). Zonder die afspraak zou
    `_summ` per gate iets anders betekenen."""
    p = params_from_config(make_cfg())
    verlies = SimpleNamespace(market="A", buy_ms=0, sell_ms=1, net_pct=-3.0, exit="stop")
    winst = SimpleNamespace(market="A", buy_ms=0, sell_ms=1, net_pct=3.0, exit="target")
    assert entry_gate_outcome({}, verlies, p) < 0     # gate had verlies voorkomen
    assert entry_gate_outcome({}, winst, p) > 0       # gate had winst weggesneden


# --- 5.2 mode en scoping op de per-gate hash -----------------------------------

def _add_signal(market, details, mode="paper", ts_offset=0):
    from datetime import datetime, timedelta, timezone
    with session() as s:
        s.add(SignalRow(market=market, action="buy", decision="buy", score=3,
                        reason="test", details=details, mode=mode,
                        ts=datetime(2026, 8, 1, tzinfo=timezone.utc)
                        + timedelta(hours=ts_offset)))
        s.commit()


def test_events_and_trades_are_filtered_by_mode(memory_db):
    """Punt 5.2: het mode-filter stond hardcoded op paper, waardoor de analyse in
    live mode stil op paper-historie rapporteerde."""
    _add_signal("A-EUR", {"shadow_regime": "risk-off"}, mode="paper")
    _add_signal("B-EUR", {"shadow_regime": "risk-off"}, mode="live")
    with session() as s:
        s.add(TradeRow(market="A-EUR", side="buy", amount=1, price=100, fee_eur=0.25,
                       mode="paper"))
        s.add(TradeRow(market="B-EUR", side="buy", amount=1, price=100, fee_eur=0.25,
                       mode="live"))
        s.commit()

    assert [e["market"] for e in load_events_from_db("shadow_regime", "paper")] == ["A-EUR"]
    assert [e["market"] for e in load_events_from_db("shadow_regime", "live")] == ["B-EUR"]
    assert len(load_events_from_db("shadow_regime")) == 2          # geen filter = alles
    assert {t["market"] for t in load_roundtrips_from_db("live")} == {"B-EUR"}
    assert {t["market"] for t in load_roundtrips_from_db("paper")} == {"A-EUR"}


def test_events_are_scoped_on_the_gate_hash(memory_db):
    """Elke gate scoopt op zijn eigen hash. Rijen van vóór v0.20.0 hebben geen
    `gate_hash` en vallen buiten de gescopede meting; zonder hash meegeven krijg je
    de totaalmeting."""
    _add_signal("A-EUR", {"shadow_regime": "risk-off", "gate_hash": {"regime": "aaa"}})
    _add_signal("B-EUR", {"shadow_regime": "risk-off", "gate_hash": {"regime": "bbb"}})
    _add_signal("C-EUR", {"shadow_regime": "risk-off"})            # pre-v0.20.0

    assert [e["market"] for e in
            load_events_from_db("shadow_regime", "paper", "aaa")] == ["A-EUR"]
    assert len(load_events_from_db("shadow_regime", "paper")) == 3


def test_chase_gate_reports_its_own_parameters(memory_db):
    d = analyze_chase(make_cfg(), events=[], trades=[])
    assert d["gate"] == "chase"
    assert d["max_chase_atr"] == 0.5
    assert d["binding"] is False
    assert d["summary"] is None
