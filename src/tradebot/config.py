"""Central configuration: .env secrets + config.yaml strategy parameters."""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    trading_mode: str = "paper"  # paper | live
    bitvavo_api_key: str = ""
    bitvavo_api_secret: str = ""
    groq_api_key: str = ""
    gemini_api_key: str = ""
    mistral_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    live_confirm: str = ""
    live_max_capital_eur: float = 100.0
    dashboard_token: str = ""
    database_url: str = "sqlite:///data/tradebot.db"


class LLMProviderCfg(BaseModel):
    name: str
    model: str
    daily_budget: int = 100


class AppConfig(BaseModel):
    markets: list[str]
    watchlist: list[str] = []
    blocklist: list[str] = []
    schedule: dict[str, Any]
    strategy: dict[str, Any]
    fees: dict[str, float]
    decision: dict[str, Any]
    risk: dict[str, Any]
    regime: dict[str, Any] = {}
    universe: dict[str, Any] = {}
    exits: dict[str, Any] = {}
    curation: dict[str, Any] = {}
    llm: dict[str, Any]

    @property
    def llm_providers(self) -> list[LLMProviderCfg]:
        return [LLMProviderCfg(**p) for p in self.llm.get("providers", [])]


@lru_cache
def get_secrets() -> Secrets:
    return Secrets()


# --- meet-scoping ----------------------------------------------------------
#
# Elke shadow-gate (LLM-veto, regime, breakeven, chase) wordt apart gemeten en
# gaat pas bindend bij een positieve netto gate over >= 20 afgewikkelde trades.
# Om te voorkomen dat uitkomsten van twee configuraties in één precisiecijfer
# belanden, krijgt elke gemeten gebeurtenis een config-hash mee.
#
# Bewust GEEN enkele globale hash over alles. Die zou bij vier gates de meetklok
# van alle vier resetten zodra je aan één gate draait: tunen van
# `exits.breakeven_stop.trigger_atr` in week drie zou ook de regime- en
# veto-telling terugzetten naar nul, waardoor de drempel van 20 in een fase
# waarin je juist afstelt praktisch onbereikbaar wordt.
#
# Daarom een gedeelde KERN plus per gate zijn eigen secties.
#
# Kern: bepaalt of en tegen welke prijs een entry ontstaat, en tegen welke kosten
# de uitkomst wordt afgezet. Wijzigt die, dan verandert de populatie voor elke
# gate en is resetten juist correct.
#   strategy  - signaalregels, RSI-zone, `signal_on_closed_candles`, chase-guard
#   decision  - ATR/RR, fee-gate-drempel, LLM-veto-schakelaars
#   fees      - de kosten waartegen elke uitkomst wordt afgezet
#   universe  - auto_fill bepaalt WELKE markten kandidaat zijn
# Bewust NIET in de kern: `risk` (bucket_eur, max_position_pct). Die schalen
# alleen de euro-bedragen in het rapport, niet de procentuele uitkomst per trade,
# en zouden de hash laten klapperen bij elke operationele sizing-tweak.
#
# Bekende, geaccepteerde onnauwkeurigheid: de uitkomst van een ENTRY-gate (veto,
# regime, chase) is een volledige round-trip, dus exit-parameters beïnvloeden hem
# wel degelijk. `exits` zit toch niet in hun scope, want anders is de gedeelde
# kern feitelijk weer globaal en keert het probleem hierboven terug. De ruil is
# bewust: iets meer meetruis in ruil voor haalbare drempels per gate.
CORE_SECTIONS = ("strategy", "decision", "fees", "universe")

GATE_SECTIONS = {
    "veto": CORE_SECTIONS,
    "chase": CORE_SECTIONS,                      # parameters staan onder `strategy`
    "regime": (*CORE_SECTIONS, "regime"),
    "breakeven": (*CORE_SECTIONS, "exits"),      # hele `exits`: de time-stop kan een
                                                 # breakeven-treffer vóór zijn
}


def config_fingerprint(cfg, sections: tuple[str, ...] = CORE_SECTIONS) -> str:
    """Korte, stabiele hash over de meet-relevante config.

    Duck-typed: werkt met AppConfig of elk object met dezelfde attributen
    (SimpleNamespace in tests); ontbrekende secties tellen als leeg. Terugzetten
    van een waarde geeft dezelfde hash en dus de oude meetcohorte terug, want de
    hash gaat over waarden en niet over tijd.
    """
    payload = {section: dict(getattr(cfg, section, {}) or {}) for section in sections}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def gate_fingerprint(cfg, gate: str) -> str:
    """Config-hash voor één specifieke shadow-gate; zie `GATE_SECTIONS`."""
    return config_fingerprint(cfg, GATE_SECTIONS[gate])


def _csv_env(name: str) -> list[str] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return [m.strip().upper() for m in raw.split(",") if m.strip()]


def _num_env(name: str, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return cast(raw)
    except ValueError:
        return None


@lru_cache
def get_config() -> AppConfig:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    # HA add-on opties overschrijven de yaml. Alleen operationele knoppen: een
    # parameter hoort hier als omzetten een bedrijfsvoeringsbeslissing is die geen
    # deploy mag vergen én de gemeten strategie niet verandert (universum, sizing,
    # gate-schakelaars, banlijst). Strategie-parameters (EMA-periodes, RSI-zone,
    # signaalscore, ATR/RR, fee-gate-drempels, exit-parameters) bewust niet: die
    # wijzigen de meting zelf en gaan via optimizer/backtest + commit.
    markets = _csv_env("TRADEBOT_MARKETS")
    watchlist = _csv_env("TRADEBOT_WATCHLIST")
    blocklist = _csv_env("TRADEBOT_BLOCKLIST")
    if markets:
        data["markets"] = markets
    if watchlist is not None:
        data["watchlist"] = watchlist
    if blocklist is not None:
        data["blocklist"] = blocklist
    interval_min = _num_env("TRADEBOT_INTERVAL_MINUTES", int)
    if interval_min:
        data["schedule"]["analysis_interval_minutes"] = interval_min
    candle = os.environ.get("TRADEBOT_CANDLE_INTERVAL", "").strip()
    if candle:
        data["schedule"]["candle_interval"] = candle
    veto_binding = os.environ.get("TRADEBOT_LLM_VETO_BINDING", "").strip().lower()
    if veto_binding in ("true", "false", "1", "0", "yes", "no"):
        data["decision"]["llm_veto_binding"] = veto_binding in ("true", "1", "yes")
    for env, key in [("TRADEBOT_REGIME_BINDING", "binding"),
                     ("TRADEBOT_REGIME_ENABLED", "enabled")]:
        val = os.environ.get(env, "").strip().lower()
        if val in ("true", "false", "1", "0", "yes", "no"):
            data.setdefault("regime", {})[key] = val in ("true", "1", "yes")
    for env, section, key, cast in [
        ("TRADEBOT_MAX_POSITION_PCT", "risk", "max_position_pct", float),
        ("TRADEBOT_MAX_OPEN_POSITIONS", "risk", "max_open_positions", float),
        ("TRADEBOT_COOLDOWN_HOURS", "risk", "cooldown_hours_after_trade", float),
        ("TRADEBOT_POSITION_BUCKET_EUR", "risk", "bucket_eur", float),
    ]:
        val = _num_env(env, cast)
        if val is not None:
            data[section][key] = val
    sizing = os.environ.get("TRADEBOT_SIZING", "").strip().lower()
    if sizing in ("bucket", "percent"):
        data["risk"]["sizing"] = sizing
    auto_fill = os.environ.get("TRADEBOT_AUTO_FILL", "").strip().lower()
    if auto_fill in ("true", "false", "1", "0", "yes", "no"):
        data.setdefault("universe", {})["auto_fill"] = auto_fill in ("true", "1", "yes")
    return AppConfig(**data)
