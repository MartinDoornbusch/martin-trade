"""Bewaakt de koppeling tussen de add-on-configuratie en de repo (review blok 3).

Drie soorten drift die de review aantrof, alle drie stil en alle drie met effect
op de Pi:

* de add-on-versie liep vier minors voor op pyproject.toml (0.18.0 vs 0.14.1);
* het schema begrensde `max_open_positions` op 5 terwijl config.yaml 10 zei, en
  entrypoint.py zet elke ingevulde optie als env-var die de yaml overschrijft;
* opties stonden in ENV_MAP maar niet in het schema, of andersom.

Deze tests draaien in CI en laten de bouw falen zodra de bestanden uit de pas
lopen. Ze lezen de echte bestanden, geen kopie.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ADDON_CFG = ROOT / "tradebot-addon" / "config.yaml"
ENTRYPOINT = ROOT / "tradebot-addon" / "entrypoint.py"
PYPROJECT = ROOT / "pyproject.toml"
APP_CFG = ROOT / "config" / "config.yaml"


def addon() -> dict:
    return yaml.safe_load(ADDON_CFG.read_text(encoding="utf-8"))


def app() -> dict:
    return yaml.safe_load(APP_CFG.read_text(encoding="utf-8"))


def entrypoint_env_map() -> dict[str, str]:
    """Leest ENV_MAP uit entrypoint.py zonder het te importeren (het bestand
    start de app zodra je main() aanroept en verwacht /data/options.json)."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    block = text.split("ENV_MAP = {", 1)[1].split("}", 1)[0]
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block))


# --- 3.3 versiekoppeling --------------------------------------------------------

def test_package_version_matches_pyproject():
    """Derde versielocatie, gevonden in ronde 2: `src/tradebot/__init__.py` stond nog
    op 0.18.0 terwijl pyproject en de add-on op 0.20.0 stonden. Die waarde komt in
    elke meetexport terecht, dus drift maakt een bewijsartefact onbetrouwbaar."""
    from tradebot import __version__

    version = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"),
                        re.MULTILINE).group(1)
    assert __version__ == version


def test_addon_version_matches_pyproject():
    version = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"),
                        re.MULTILINE).group(1)
    assert addon()["version"] == version, (
        "tradebot-addon/config.yaml en pyproject.toml moeten dezelfde versie hebben; "
        "HA leest de add-on-versie en rolt daarop uit")


# --- 3.1 schema tegenover de yaml -----------------------------------------------

def test_option_defaults_match_app_config():
    """Elke default in de add-on moet de yaml-waarde herhalen.

    entrypoint.py zet elke ingevulde optie als env-var, en die wint van de yaml.
    Een afwijkende default is dus geen 'startwaarde' maar een stille override:
    zo draaide de Pi op 3 posities terwijl de repo 10 zei.
    """
    opts = addon()["options"]
    cfg = app()
    assert opts["max_open_positions"] == cfg["risk"]["max_open_positions"]
    assert opts["max_position_pct"] == cfg["risk"]["max_position_pct"]
    assert opts["bucket_eur"] == cfg["risk"]["bucket_eur"]
    assert opts["cooldown_hours"] == cfg["risk"]["cooldown_hours_after_trade"]
    assert opts["sizing"] == cfg["risk"]["sizing"]
    assert opts["auto_fill"] == cfg["universe"]["auto_fill"]
    assert opts["regime_enabled"] == cfg["regime"]["enabled"]
    assert opts["regime_binding"] == cfg["regime"]["binding"]
    assert opts["llm_veto_binding"] == cfg["decision"]["llm_veto_binding"]
    assert opts["analysis_interval_minutes"] == cfg["schedule"]["analysis_interval_minutes"]
    assert opts["candle_interval"] == cfg["schedule"]["candle_interval"]
    assert opts["markets"] == ",".join(cfg["markets"])
    assert opts["watchlist"] == ",".join(cfg["watchlist"])


def test_numeric_schema_bounds_allow_the_configured_value():
    """De schemagrenzen moeten de waarde uit de yaml kunnen bevatten."""
    schema = addon()["schema"]
    opts = addon()["options"]
    for key in ("analysis_interval_minutes", "max_position_pct", "max_open_positions",
                "cooldown_hours", "bucket_eur", "live_max_capital_eur"):
        match = re.fullmatch(r"int\((\d+),(\d+)\)", str(schema[key]))
        assert match, f"{key} heeft geen begrensd int-schema"
        low, high = int(match.group(1)), int(match.group(2))
        assert low <= opts[key] <= high, f"{key}={opts[key]} valt buiten {low}-{high}"


def test_max_open_positions_ceiling_matches_the_market_cap():
    """Plafond gelijk aan lists.MAX_MARKETS: meer open posities dan verhandelbare
    markten heeft geen betekenis, en alt-sprawl was een verliesoorzaak."""
    from tradebot.lists import MAX_MARKETS

    high = int(re.fullmatch(r"int\(\d+,(\d+)\)",
                            str(addon()["schema"]["max_open_positions"])).group(1))
    assert high == MAX_MARKETS


# --- 3.2 opties, schema en ENV_MAP in de pas ------------------------------------

def test_every_option_has_a_schema_entry_and_vice_versa():
    data = addon()
    assert set(data["options"]) == set(data["schema"])


def test_every_option_is_wired_to_an_env_var():
    """Een optie zonder ENV_MAP-regel doet niets; een ENV_MAP-regel zonder optie
    is dode bedrading."""
    assert set(addon()["options"]) == set(entrypoint_env_map())


def test_operational_switches_are_available_as_options():
    """De knoppen die de review miste, moeten operationeel te sturen zijn."""
    opts = set(addon()["options"])
    assert {"sizing", "bucket_eur", "auto_fill", "regime_enabled", "regime_binding",
            "llm_veto_binding", "blocklist"} <= opts


def test_strategy_parameters_stay_out_of_the_addon():
    """Strategie-parameters blijven in de yaml: ze wijzigen de meting zelf."""
    opts = set(addon()["options"])
    verboden = {"ema_fast", "ema_slow", "rsi_period", "rsi_buy_zone_min",
                "rsi_buy_zone_max", "rsi_overbought", "atr_period",
                "min_signal_score", "min_profit_pct", "atr_stop_multiplier",
                "reward_risk_ratio", "maker_pct", "taker_pct",
                "slippage_buffer_pct", "time_stop_candles", "time_stop_min_net_pct",
                "signal_on_closed_candles", "max_chase_atr", "chase_guard_binding"}
    assert not (opts & verboden)
