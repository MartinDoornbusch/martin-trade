"""Meting van de markt-brede regime-gate (shadow).

Dunne laag op `shadow_gate.analyze_shadow_gate`. Vóór v0.20.0 stond hier de hele
meetromp; met breakeven en chase erbij zouden dat vier bijna identieke kopieën
zijn geworden, dus de romp is generiek gemaakt en wat deze gate onderscheidt
staat in één `GateSpec`.

De regime-gate weert nieuwe entries als de proxy-markt (BTC) in down-trend staat.
In shadow blokkeert hij niet, dus elke regime-down buy is een ECHTE trade met
bekende uitkomst: geen counterfactual-reconstructie nodig, we lezen de
gerealiseerde round-trip-P&L direct.

Geen deduplicatie: doordat de koop in shadow gewoon doorgaat, staat er de
volgende cyclus een positie open en strandt de kandidaat al op de risk-gate. Deze
gate levert dus hooguit één event per werkelijke buy.
"""
from __future__ import annotations

from .shadow_gate import GateSpec, analyze_shadow_gate, entry_gate_outcome

SPEC = GateSpec(
    name="regime",
    details_key="shadow_regime",
    label="Regime-gate (markt-breed, gecodeerd)",
    outcome=entry_gate_outcome,
    match="same_cycle",
    dedup=False,
)


def analyze_regime(cfg, **kwargs) -> dict:
    """Netto gate-waarde van de regime-filter uit echte uitkomsten."""
    regime_cfg = getattr(cfg, "regime", {}) or {}
    out = analyze_shadow_gate(cfg, SPEC, **kwargs)
    out["proxy_market"] = str(regime_cfg.get("proxy_market", "BTC-EUR"))
    out["binding"] = bool(regime_cfg.get("binding", False))
    return out


def main() -> None:  # pragma: no cover - CLI-gemak, niet in de testsuite
    import json

    from ..config import get_config
    print(json.dumps(analyze_regime(get_config()), indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
