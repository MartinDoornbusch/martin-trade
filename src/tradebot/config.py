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
    export: dict[str, Any] = {}
    meta: dict[str, Any] = {}
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

# Restval die de kern anders via de achterdeur weer globaal maakt: de parameters
# van de chase-guard staan onder `strategy` en die van de LLM-veto onder
# `decision`, dus allebei IN de kern. Aan `max_chase_atr` draaien zou daarmee
# alsnog alle vier de meetklokken resetten.
#
# De uitweg volgt uit de semantiek van shadow zelf: een gate die niet bindend is,
# blokkeert per definitie niets en verandert de populatie buys dus niet. Zijn
# parameters horen daarom in niemands kern zolang `binding: false`. De
# BINDING-VLAG blijft wel altijd meetellen: die omzetten laat de gate de populatie
# wél bepalen, en dan is een reset van alle metingen inhoudelijk verdedigbaar.
#
# Een gate ziet zijn eigen parameters altijd, ongeacht binding; anders zou tunen
# van bijvoorbeeld `trigger_atr` in shadow niet meer van de eigen oude meting
# gescheiden worden, wat juist het doel van de scoping is.
SHADOW_GATES = {
    "veto": {
        "section": None,                      # parameters staan in de kern (`decision`)
        "binding": ("decision", "llm_veto_binding"),
        "params": [("decision", "llm_min_confidence"),
                   ("decision", "use_llm_second_opinion")],
    },
    "chase": {
        "section": None,                      # parameters staan in de kern (`strategy`)
        "binding": ("strategy", "chase_guard_binding"),
        "params": [("strategy", "max_chase_atr")],
    },
    "regime": {
        "section": "regime",
        "binding": ("regime", "binding"),
        "params": [("regime", "enabled"), ("regime", "proxy_market")],
    },
    "timestop": {
        "section": "exits",
        "binding": ("exits", "time_stop_binding"),
        "params": [("exits", "time_stop_candles"), ("exits", "time_stop_min_net_pct")],
    },
    "breakeven": {
        "section": "exits",                   # hele `exits`: de time-stop kan een
        "binding": ("exits", "breakeven_stop", "binding"),   # treffer vóór zijn
        "params": [("exits", "breakeven_stop", "enabled"),
                   ("exits", "breakeven_stop", "trigger_atr"),
                   ("exits", "breakeven_stop", "offset_pct"),
                   ("exits", "breakeven_stop", "offset_margin_pct")],
    },
}

def gate_sections(cfg, gate: str) -> tuple[str, ...]:
    """Config-secties die in de hash van één gate horen.

    Kern, plus de eigen sectie van deze gate, plus de sectie van elke andere gate
    die BINDEND staat. Die laatste regel is de spiegel van de shadow-semantiek:
    zolang een gate niets blokkeert gaat hij niemand anders aan, maar zodra hij
    bindend is vormt hij de populatie buys en hoort hij in ieders scope.
    """
    out = list(CORE_SECTIONS)
    for name, spec in SHADOW_GATES.items():
        section = spec["section"]
        if section is None or section in out:
            continue
        if name == gate or _is_binding_cfg(cfg, spec["binding"]):
            out.append(section)
    return tuple(out)


def _lookup(payload: dict, path: tuple[str, ...]):
    node = payload
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None, None
    return node, path[-1]


def _drop(payload: dict, path: tuple[str, ...]) -> None:
    node, key = _lookup(payload, path)
    if node is not None:
        node.pop(key, None)


def _is_binding(payload: dict, path: tuple[str, ...]) -> bool:
    node, key = _lookup(payload, path)
    return bool(node.get(key)) if node is not None else False


def _is_binding_cfg(cfg, path: tuple[str, ...]) -> bool:
    """Zelfde vraag, maar rechtstreeks op het config-object in plaats van op een
    al opgebouwde payload (nodig vóór we weten welke secties meedoen)."""
    node = getattr(cfg, path[0], {}) or {}
    for key in path[1:-1]:
        node = node.get(key) if isinstance(node, dict) else {}
    return bool(node.get(path[-1])) if isinstance(node, dict) else False


def config_fingerprint(cfg, sections: tuple[str, ...] = CORE_SECTIONS,
                       own_gate: str | None = None) -> str:
    """Korte, stabiele hash over de meet-relevante config.

    Duck-typed: werkt met AppConfig of elk object met dezelfde attributen
    (SimpleNamespace in tests); ontbrekende secties tellen als leeg. Terugzetten
    van een waarde geeft dezelfde hash en dus de oude meetcohorte terug, want de
    hash gaat over waarden en niet over tijd.

    Parameters van een NIET-bindende shadow-gate vallen buiten de hash (behalve
    voor die gate zelf, `own_gate`): zo'n gate blokkeert niets en verandert de
    populatie buys dus niet. Zie de toelichting bij `SHADOW_GATES`.
    """
    payload = json.loads(json.dumps(
        {section: dict(getattr(cfg, section, {}) or {}) for section in sections},
        default=str))
    for gate, spec in SHADOW_GATES.items():
        if gate == own_gate or _is_binding(payload, spec["binding"]):
            continue
        for path in spec["params"]:
            _drop(payload, path)
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def gate_fingerprint(cfg, gate: str) -> str:
    """Config-hash voor één specifieke shadow-gate; zie `gate_sections`.

    Eenrichtingsverkeer, bewust: zodra een gate bindend wordt blokkeert hij, en
    levert hij zelf geen shadow-events met een gerealiseerde uitkomst meer op. De
    go/no-go wordt dus genomen op de data die er tot dat moment ligt en daarna
    meet je die gate niet meer. Ga daarom ruim boven de 20 afgewikkelde trades
    zitten voordat je omzet, niet er net overheen. Terugzetten naar shadow geeft
    exact dezelfde hash als vóór de flip (de hash gaat over waarden), dus de oude
    cohorte wordt weer opgepakt; alleen de periode waarin de gate bindend stond
    vormt een eigen cohorte, en dat is juist correct want daar was de populatie
    buys anders. Leg elke flip als gedateerde gebeurtenis vast in PROJECTPLAN.md,
    zodat een sprong in de meetdata later herleidbaar is tot een beslissing in
    plaats van weggeklaard te moeten worden als ruis.
    """
    return config_fingerprint(cfg, gate_sections(cfg, gate), own_gate=gate)


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
