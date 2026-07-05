"""HA add-on entrypoint: vertaalt Supervisor options (/data/options.json) naar
env-vars voor tradebot en start de app. DB staat op /data (persistent over updates).
"""
import json
import os
from pathlib import Path

OPTIONS_FILE = Path("/data/options.json")

ENV_MAP = {
    "trading_mode": "TRADING_MODE",
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
            if value not in (None, ""):
                os.environ.setdefault(env, str(value))
    os.environ.setdefault("DATABASE_URL", "sqlite:////data/tradebot.db")

    from tradebot.main import run
    run()


if __name__ == "__main__":
    main()
