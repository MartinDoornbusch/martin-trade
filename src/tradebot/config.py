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


@lru_cache
def get_config() -> AppConfig:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return AppConfig(**yaml.safe_load(fh))
