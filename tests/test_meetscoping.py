"""Bewaakt de meet-scoping van de shadow-gates (review ronde 2).

Vier gates (LLM-veto, regime, breakeven, chase) worden apart gemeten en gaan pas
bindend bij een positieve netto gate over >= 20 afgewikkelde trades. Twee regels
dragen die meting, en allebei kunnen ze bij een latere refactor stilletjes
wegsijpelen zonder dat er iets anders roodkleurt:

1. Een drempelwijziging aan een NIET-bindende gate laat de hash van de andere
   gates ongemoeid. Anders reset tunen van één gate de meetklok van alle vier en
   wordt de drempel van 20 onbereikbaar in juist de fase waarin je afstelt.
2. `binding: false -> true` verandert de hash van iedereen. Vanaf dat moment
   bepaalt de gate namelijk wél welke buys er zijn.

Deze tests staan bewust apart, in de geest van `test_addon_config.py`: ze bewaken
een afspraak, niet een implementatie.
"""
from types import SimpleNamespace

from tradebot.config import (
    CORE_SECTIONS,
    SHADOW_GATES,
    config_fingerprint,
    gate_fingerprint,
    gate_sections,
)


def cfg(**over) -> SimpleNamespace:
    base = dict(
        strategy={"ema_fast": 12, "signal_on_closed_candles": True,
                  "max_chase_atr": 0.5, "chase_guard_binding": False},
        decision={"reward_risk_ratio": 1.5, "llm_min_confidence": 0.6,
                  "use_llm_second_opinion": True, "llm_veto_binding": False},
        fees={"taker_pct": 0.25},
        universe={"auto_fill": True},
        exits={"time_stop_candles": 12,
               "breakeven_stop": {"enabled": True, "binding": False,
                                  "trigger_atr": 1.0, "offset_pct": 0.55}},
        regime={"enabled": True, "binding": False, "proxy_market": "BTC-EUR"},
        risk={"bucket_eur": 250.0},
    )
    for key, value in over.items():
        base[key] = {**base[key], **value} if isinstance(value, dict) else value
    return SimpleNamespace(**base)


def deep(section: dict, path: list[str], value) -> dict:
    """Kopie van een sectie met één geneste waarde vervangen."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in section.items()}
    node = out
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return out


# --- regel 1: shadow-drempels raken de anderen niet -----------------------------

def test_tuning_a_shadow_threshold_leaves_the_other_gates_untouched():
    basis = cfg()
    gevallen = {
        "chase": cfg(strategy={"max_chase_atr": 2.0}),
        "veto": cfg(decision={"llm_min_confidence": 0.9}),
        "regime": cfg(regime={"proxy_market": "ETH-EUR"}),
        "breakeven": cfg(exits=deep(basis.exits, ["breakeven_stop", "trigger_atr"], 1.5)),
    }
    for getunede_gate, gewijzigd in gevallen.items():
        for andere in SHADOW_GATES:
            if andere == getunede_gate:
                continue
            assert gate_fingerprint(basis, andere) == gate_fingerprint(gewijzigd, andere), \
                f"tunen van {getunede_gate} reset de meting van {andere}"


def test_a_gate_always_sees_its_own_threshold():
    """Zonder deze eigenschap scheidt een gate zijn eigen oude meting niet meer van
    de nieuwe, en dat is precies waar de scoping voor is."""
    basis = cfg()
    gevallen = {
        "chase": cfg(strategy={"max_chase_atr": 2.0}),
        "veto": cfg(decision={"llm_min_confidence": 0.9}),
        "regime": cfg(regime={"proxy_market": "ETH-EUR"}),
        "breakeven": cfg(exits=deep(basis.exits, ["breakeven_stop", "trigger_atr"], 1.5)),
    }
    for gate, gewijzigd in gevallen.items():
        assert gate_fingerprint(basis, gate) != gate_fingerprint(gewijzigd, gate), gate


# --- regel 2: bindend maken reset iedereen --------------------------------------

def test_making_a_gate_binding_resets_every_measurement():
    """Eenrichtingsverkeer: vanaf het moment dat een gate bindend is, blokkeert hij
    en levert hij zelf geen shadow-events met gerealiseerde uitkomst meer op. De
    go/no-go wordt dus genomen op de data die er tot dat moment ligt."""
    basis = cfg()
    bindend = {
        "chase": cfg(strategy={"chase_guard_binding": True}),
        "veto": cfg(decision={"llm_veto_binding": True}),
        "regime": cfg(regime={"binding": True}),
        "breakeven": cfg(exits=deep(basis.exits, ["breakeven_stop", "binding"], True)),
    }
    for omgezette_gate, gewijzigd in bindend.items():
        for gate in SHADOW_GATES:
            assert gate_fingerprint(basis, gate) != gate_fingerprint(gewijzigd, gate), \
                f"{omgezette_gate} bindend maken laat de meting van {gate} staan"


def test_a_binding_gate_shapes_the_population_for_everyone():
    """Andersom: staat een gate bindend, dan vormt zijn parameter de populatie buys
    en telt hij wél mee in de hash van de andere gates."""
    a = cfg(regime={"binding": True, "proxy_market": "BTC-EUR"})
    b = cfg(regime={"binding": True, "proxy_market": "ETH-EUR"})
    assert gate_fingerprint(a, "veto") != gate_fingerprint(b, "veto")
    assert "regime" in gate_sections(a, "veto")
    assert "regime" not in gate_sections(cfg(), "veto")     # shadow: buiten beeld


# --- kern en uitzonderingen -----------------------------------------------------

def test_core_change_resets_every_gate():
    """De kern bepaalt of en tegen welke prijs een entry ontstaat; wijzigt die, dan
    verandert de populatie voor elke gate en is resetten juist correct."""
    basis, gewijzigd = cfg(), cfg(strategy={"ema_fast": 20})
    for gate in SHADOW_GATES:
        assert gate_fingerprint(basis, gate) != gate_fingerprint(gewijzigd, gate), gate


def test_operational_sizing_is_not_part_of_the_hash():
    """`risk` blijft erbuiten: bucket_eur schaalt alleen de euro-bedragen in het
    rapport, niet de procentuele uitkomst per trade."""
    assert "risk" not in CORE_SECTIONS
    assert config_fingerprint(cfg(risk={"bucket_eur": 250.0})) == \
        config_fingerprint(cfg(risk={"bucket_eur": 500.0}))


def test_restoring_a_value_restores_the_cohort():
    """De hash gaat over waarden, niet over tijd: terugzetten geeft de oude
    meetcohorte terug in plaats van een derde."""
    assert gate_fingerprint(cfg(), "regime") == gate_fingerprint(cfg(), "regime")
    assert len(gate_fingerprint(cfg(), "regime")) == 12
