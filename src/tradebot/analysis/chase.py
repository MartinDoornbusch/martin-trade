"""Meting van de chase-guard (shadow).

De chase-guard slaat een entry over als de live prijs te ver van de signaalclose
af staat. In shadow gaat de koop gewoon door, dus de uitkomst is de gerealiseerde
round-trip-P&L van precies die buy: dezelfde vraag en dezelfde rekensom als bij
de regime-gate.

Geen deduplicatie, en dat is gemeten in plaats van aangenomen: doordat de koop in
shadow doorgaat, strandt de kandidaat de volgende cyclus al op "position already
open" vóórdat de guard aan bod komt. Eén event per werkelijke buy dus.
`tests/test_engine_cycle.py::test_chase_guard_logs_once_per_buy_not_once_per_cycle`
legt dat vast; verandert dat gedrag, dan moet deze spec mee.
"""
from __future__ import annotations

from .shadow_gate import GateSpec, analyze_shadow_gate, entry_gate_outcome

SPEC = GateSpec(
    name="chase",
    details_key="shadow_chase",
    label="Chase-guard (entry-drift tussen signaalclose en fill)",
    outcome=entry_gate_outcome,
    match="same_cycle",
    dedup=False,
)


def analyze_chase(cfg, **kwargs) -> dict:
    """Netto gate-waarde van de chase-guard uit echte uitkomsten."""
    strategy = getattr(cfg, "strategy", {}) or {}
    out = analyze_shadow_gate(cfg, SPEC, **kwargs)
    out["binding"] = bool(strategy.get("chase_guard_binding", False))
    out["max_chase_atr"] = float(strategy.get("max_chase_atr", 0.0) or 0.0)
    return out


def main() -> None:  # pragma: no cover - CLI-gemak, niet in de testsuite
    import json

    from ..config import get_config
    print(json.dumps(analyze_chase(get_config()), indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
