"""Meting van de breakeven-stop (shadow).

De breakeven-stop verkoopt in shadow niet: hij logt een treffer en de positie
blijft open. Daardoor is de meting eenvoudiger dan bij de andere gates. De prijs
op signaalmoment en de werkelijke exitprijs zijn allebei bekend, dus de netto
gate is (hypothetische exit) min (werkelijke exit), zonder de
counterfactual-reconstructie uit candles die de LLM-veto nodig heeft.

Twee dingen die deze gate onderscheiden van de entry-gates:

* de VRAAG. Regime en chase vragen "de gate had deze buy geblokkeerd, wat deed
  die buy?". Deze vraagt "de gate had hier geëxit, wat deed de positie daarna?".
  Vandaar een eigen `outcome` binnen dezelfde generieke romp.
* de DEDUPLICATIE. Een treffer wordt elke cyclus opnieuw gelogd zolang de koers
  onder de drempel hangt, dus een positie die vier cycli onder de drempel hangt
  levert vier events op. Gededupliceerd op de EERSTE treffer per positie.

Waarom dedup in de ANALYSEMODULE en niet bij het loggen: de ruwe log blijft dan
compleet, zodat achteraf nog te zien is hoe lang een positie onder de drempel
hing (dat is een andere, ook interessante vraag). De dedup is een meetkeuze, en
meetkeuzes wil je kunnen herzien zonder deploy en zonder dataverlies. De kosten
zijn verwaarloosbaar: enkele rijen per uur.
"""
from __future__ import annotations

from .shadow_gate import GateSpec, analyze_shadow_gate, breakeven_outcome

SPEC = GateSpec(
    name="breakeven",
    details_key="shadow_breakeven",
    label="Breakeven-stop (winst die er was mag geen verlies worden)",
    outcome=breakeven_outcome,
    match="during_position",
    dedup=True,
)


def analyze_breakeven(cfg, **kwargs) -> dict:
    """Netto gate-waarde van de breakeven-stop uit echte uitkomsten."""
    be_cfg = ((getattr(cfg, "exits", {}) or {}).get("breakeven_stop", {}) or {})
    out = analyze_shadow_gate(cfg, SPEC, **kwargs)
    out["binding"] = bool(be_cfg.get("binding", False))
    out["enabled"] = bool(be_cfg.get("enabled", False))
    out["trigger_atr"] = float(be_cfg.get("trigger_atr", 0.0) or 0.0)
    out["offset_pct"] = float(be_cfg.get("offset_pct", 0.0) or 0.0)
    return out


def main() -> None:  # pragma: no cover - CLI-gemak, niet in de testsuite
    import json

    from ..config import get_config
    print(json.dumps(analyze_breakeven(get_config()), indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
