"""Telegram notifications. Silently no-ops when not configured."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            ).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Telegram notification failed: %s", exc)
