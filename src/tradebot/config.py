"""Central configuration: .env secrets + config.yaml strategy parameters."""
from __future__ import annotations

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
    schedule: dict[str, Any]
    strategy: dict[str, Any]
    fees: dict[str, float]
    decision: dict[str, Any]
    risk: dict[str, float]
    llm: dict[str, Any]

    @property
    def llm_providers(self) -> list[LLMProviderCfg]:
        return [LLMProviderCfg(**p) for p in self.llm.get("providers", [])]


@lru_cache
def get_secrets() -> Secrets:
    return Secrets()


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
    # HA add-on opties overschrijven de yaml. Alleen operationele knoppen;
    # strategie-parameters (EMA/score/fee-gate) bewust niet, die gaan via optimizer + commit.
    markets = _csv_env("TRADEBOT_MARKETS")
    watchlist = _csv_env("TRADEBOT_WATCHLIST")
    if markets:
        data["markets"] = markets
    if watchlist is not None:
        data["watchlist"] = watchlist
    interval_min = _num_env("TRADEBOT_INTERVAL_MINUTES", int)
    if interval_min:
        data["schedule"]["analysis_interval_minutes"] = interval_min
    candle = os.environ.get("TRADEBOT_CANDLE_INTERVAL", "").strip()
    if candle:
        data["schedule"]["candle_interval"] = candle
    for env, section, key, cast in [
        ("TRADEBOT_MAX_POSITION_PCT", "risk", "max_position_pct", float),
        ("TRADEBOT_MAX_OPEN_POSITIONS", "risk", "max_open_positions", float),
        ("TRADEBOT_COOLDOWN_HOURS", "risk", "cooldown_hours_after_trade", float),
    ]:
        val = _num_env(env, cast)
        if val is not None:
            data[section][key] = val
    return AppConfig(**data)
