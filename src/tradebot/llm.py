"""LLM second-opinion layer with multi-provider fallback on free tiers.

Roles are strictly limited: the LLM reviews a BUY candidate that already passed
all mechanical gates (signal score, risk limits, fee gate) and returns
agree/veto with confidence. It never initiates trades and never touches exits.

Providers (all expose OpenAI-compatible chat endpoints):
  groq    -> https://api.groq.com/openai/v1
  gemini  -> https://generativelanguage.googleapis.com/v1beta/openai
  mistral -> https://api.mistral.ai/v1
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .db import LLMCallRow, session
from .strategy import Candidate

log = logging.getLogger(__name__)

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "mistral": "https://api.mistral.ai/v1",
}

SYSTEM_PROMPT = (
    "You are a conservative crypto swing-trading risk reviewer. You receive a BUY "
    "candidate that already passed technical, risk and fee gates. Your only job is "
    "to catch reasons NOT to buy: conflicting momentum, overextended price, falling-knife "
    "patterns, or weak confluence. Respond ONLY with JSON: "
    '{"agree": true|false, "confidence": 0.0-1.0, "reasoning": "<max 40 words>"}. '
    "Be skeptical: when in doubt, disagree. A missed trade costs nothing; "
    "a bad trade costs fees plus loss."
)


@dataclass
class Verdict:
    agree: bool
    confidence: float
    reasoning: str
    provider: str = ""


@dataclass
class ProviderState:
    name: str
    model: str
    api_key: str
    daily_budget: int
    used_today: int = 0
    day: str = ""

    def available(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.used_today = today, 0
        return bool(self.api_key) and self.used_today < self.daily_budget


class LLMRouter:
    def __init__(self, providers: list[ProviderState], timeout: int = 20):
        self.providers = providers
        self.timeout = timeout

    def _call(self, p: ProviderState, prompt: str) -> tuple[Verdict, int]:
        t0 = time.monotonic()
        resp = httpx.post(
            f"{BASE_URLS[p.name]}/chat/completions",
            headers={"Authorization": f"Bearer {p.api_key}"},
            json={
                "model": p.model,
                "temperature": 0.2,
                "max_tokens": 200,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        p.used_today += 1
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        latency = int((time.monotonic() - t0) * 1000)
        v = Verdict(bool(data["agree"]), float(data["confidence"]),
                    str(data.get("reasoning", ""))[:2000], provider=p.name)
        return v, latency

    def second_opinion(self, candidate: Candidate) -> Verdict | None:
        """Try providers in order; return None if all fail (caller decides policy)."""
        snap = candidate.snapshot
        prompt = json.dumps({
            "market": candidate.market,
            "signal_score": candidate.score,
            "signal_reasons": candidate.reasons,
            "price": snap.price,
            "rsi": round(snap.rsi, 1),
            "ema_fast_vs_slow_pct": round((snap.ema_fast / snap.ema_slow - 1) * 100, 2),
            "macd_histogram": round(snap.macd_hist, 4),
            "macd_histogram_prev": round(snap.macd_hist_prev, 4),
            "atr_pct_of_price": round(snap.atr / snap.price * 100, 2),
            "price_vs_bb_lower_pct": round((snap.price / snap.bb_lower - 1) * 100, 2),
            "change_last_24h_pct": round(snap.change_24c_pct, 2),
        })
        for p in self.providers:
            if not p.available():
                continue
            try:
                verdict, latency = self._call(p, prompt)
                with session() as s:
                    s.add(LLMCallRow(provider=p.name, model=p.model, market=candidate.market,
                                     verdict="agree" if verdict.agree else "veto",
                                     confidence=verdict.confidence,
                                     reasoning=verdict.reasoning, latency_ms=latency))
                    s.commit()
                return verdict
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("LLM provider %s failed: %s — trying next", p.name, exc)
        log.error("All LLM providers failed or exhausted budget")
        return None


def build_router(cfg_providers, secrets, timeout: int) -> LLMRouter:
    keys = {"groq": secrets.groq_api_key, "gemini": secrets.gemini_api_key,
            "mistral": secrets.mistral_api_key}
    states = [ProviderState(p.name, p.model, keys.get(p.name, ""), p.daily_budget)
              for p in cfg_providers if keys.get(p.name)]
    return LLMRouter(states, timeout)
