"""Meting van de time-stop (shadow).

Deze gate is in v0.18.0 BINDEND in productie gezet zonder enige meting, tegen de
eigen projectregel in dat elke gate eerst in shadow draait. De eerste meting kwam
er pas met `calibrate --vergelijk` in v0.20.0: in portfolio-modus, op zes maanden
echte data over vijf markten, kostte hij 15,7 procentpunt rendement en draaide het
resultaat van +3,52% naar -12,17%. De win-rate zakte van 45,2% naar 27,2%, deels
per definitie (een positie die rond break-even wordt gesloten telt na fees als
verlies) en deels doordat het aantal trades met een kwart steeg: 84 naar 114, dus
meer fees op precies de manier die de vorige bot 15% kapitaal kostte.

Daarom staat hij sindsdien op shadow, en meet deze module wat hij zou hebben
gedaan. Zelfde vraag en zelfde rekensom als bij de breakeven-stop: de gate had hier
geëxit, wat deed de positie daarna? Dus ook dezelfde `exit_gate_outcome` en
dezelfde deduplicatie, want een treffer wordt elke cyclus opnieuw gelogd zolang de
positie stil blijft hangen.
"""
from __future__ import annotations

from .shadow_gate import GateSpec, analyze_shadow_gate, exit_gate_outcome

SPEC = GateSpec(
    name="timestop",
    details_key="shadow_timestop",
    label="Time-stop (stilstaande positie geeft zijn slot terug)",
    outcome=exit_gate_outcome,
    match="during_position",
    dedup=True,
)


def analyze_timestop(cfg, **kwargs) -> dict:
    """Netto gate-waarde van de time-stop uit echte uitkomsten."""
    exits = getattr(cfg, "exits", {}) or {}
    out = analyze_shadow_gate(cfg, SPEC, **kwargs)
    out["binding"] = bool(exits.get("time_stop_binding", True))
    out["time_stop_candles"] = int(exits.get("time_stop_candles", 0) or 0)
    out["min_net_pct"] = float(exits.get("time_stop_min_net_pct", 0.0) or 0.0)
    return out


def main() -> None:  # pragma: no cover - CLI-gemak, niet in de testsuite
    """CLI: python -m tradebot.analysis.timestop [--all]"""
    import json
    import sys

    from ..config import gate_fingerprint, get_config, get_secrets
    from ..db import init_db
    cfg = get_config()
    secrets = get_secrets()
    init_db(secrets.database_url)
    scope_all = "--all" in sys.argv[1:]
    print(json.dumps(
        analyze_timestop(cfg, mode=None if scope_all else secrets.trading_mode,
                         gate_hash=None if scope_all else gate_fingerprint(cfg, "timestop")),
        indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
