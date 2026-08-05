"""HA add-on entrypoint: vertaalt Supervisor options (/data/options.json) naar
env-vars voor tradebot en start de app. DB staat op /data (persistent over updates).
"""
import json
import os
from pathlib import Path

OPTIONS_FILE = Path("/data/options.json")

ENV_MAP = {
    "trading_mode": "TRADING_MODE",
    "markets": "TRADEBOT_MARKETS",
    "watchlist": "TRADEBOT_WATCHLIST",
    "blocklist": "TRADEBOT_BLOCKLIST",
    "analysis_interval_minutes": "TRADEBOT_INTERVAL_MINUTES",
    "candle_interval": "TRADEBOT_CANDLE_INTERVAL",
    "sizing": "TRADEBOT_SIZING",
    "bucket_eur": "TRADEBOT_POSITION_BUCKET_EUR",
    "max_position_pct": "TRADEBOT_MAX_POSITION_PCT",
    "max_open_positions": "TRADEBOT_MAX_OPEN_POSITIONS",
    "cooldown_hours": "TRADEBOT_COOLDOWN_HOURS",
    "auto_fill": "TRADEBOT_AUTO_FILL",
    "regime_enabled": "TRADEBOT_REGIME_ENABLED",
    "regime_binding": "TRADEBOT_REGIME_BINDING",
    "llm_veto_binding": "TRADEBOT_LLM_VETO_BINDING",
    "live_confirm": "LIVE_CONFIRM",
    "live_max_capital_eur": "LIVE_MAX_CAPITAL_EUR",
    "bitvavo_api_key": "BITVAVO_API_KEY",
    "bitvavo_api_secret": "BITVAVO_API_SECRET",
    "groq_api_key": "GROQ_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "mistral_api_key": "MISTRAL_API_KEY",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "dashboard_token": "DASHBOARD_TOKEN",
    "mqtt_host": "MQTT_HOST",
    "mqtt_port": "MQTT_PORT",
    "mqtt_user": "MQTT_USER",
    "mqtt_password": "MQTT_PASSWORD",
}


def main() -> None:
    if OPTIONS_FILE.exists():
        options = json.loads(OPTIONS_FILE.read_text())
        for key, env in ENV_MAP.items():
            value = options.get(key)
            # Lege string of ontbrekende optie = niet zetten, dan wint config.yaml.
            # False is een geldige waarde (uit-stand van een schakelaar) en wordt
            # dus wél doorgegeven; str(False) -> "False", config.py leest dat.
            if value is None or value == "":
                continue
            os.environ.setdefault(env, str(value))
    os.environ.setdefault("DATABASE_URL", "sqlite:////data/tradebot.db")

    from tradebot.main import run
    run()


if __name__ == "__main__":
    main()
