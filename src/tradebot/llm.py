"""LLM second-opinion layer with multi-provider fallback on free tiers.

Roles are strictly limited: the LLM reviews a BUY candidate that already passed
all mechanical gates (signal score, risk limits, fee gate) and returns
agree/veto with confidence. It never initiates trades and never touches exits.

Providers (all expose OpenAI-compatible chat endpoints):
  groq    -> https://api.groq.com/openai/v1
  gemini  -> https://generativelanguage.googleapis.com/v1beta/openai
  mistral -> https://api.mistral.ai/v1

Twee dingen zijn hier sinds v0.23.0 anders, allebei omdat een kapotte LLM-laag
wekenlang onzichtbaar bleef.

1. De config-hash op een `llm_calls`-rij is nu `gate_fingerprint(cfg, "veto")`,
   dezelfde die `analysis/veto.py` gebruikt om te filteren. Tot v0.22.0 stond
   hier `config_fingerprint(cfg)`, de globale hash van voor v0.20.0. Die twee
   verschillen (gemeten: f4dce99df56b tegen 98bbb5e4b0ad), dus
   `where(LLMCallRow.config_hash == ...)` matchte nooit en elke gescopede
   veto-meting gaf nul rijen. Alleen `--all` zag nog iets.

2. Elke POGING wordt vastgelegd in `llm_attempts`, geslaagd of niet. Falen deed
   hiervoor niets meer dan een `log.warning` en doorschuiven naar de volgende
   provider, dus `llm_calls` toonde uitsluitend wat lukte en een dode provider
   was van buiten niet van een stille markt te onderscheiden.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .config import gate_fingerprint, get_config
from .db import LLMAttemptRow, LLMCallRow, session
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

# Vaste kandidaat voor de testknop. Bewust dezelfde velden en hetzelfde
# request-formaat als een echte call, inclusief `response_format: json_object`:
# dat is nu juist het stuk dat sneuvelt bij een modelwissel, en een test die dat
# niet raakt bewijst alleen dat de sleutel klopt.
PROBE_PROMPT = json.dumps({
    "market": "TEST-EUR",
    "signal_score": 3,
    "signal_reasons": ["ema_cross_up", "macd_flip", "rsi_in_zone"],
    "price": 100.0,
    "rsi": 45.0,
    "ema_fast_vs_slow_pct": 1.2,
    "macd_histogram": 0.0012,
    "macd_histogram_prev": -0.0004,
    "atr_pct_of_price": 2.0,
    "price_vs_bb_lower_pct": 3.5,
    "change_last_24h_pct": 1.8,
})


@dataclass
class Verdict:
    agree: bool
    confidence: float
    reasoning: str
    provider: str = ""


@dataclass
class Attempt:
    """Uitkomst van precies één poging bij één provider."""

    provider: str
    model: str
    ok: bool
    latency_ms: int
    http_status: int = 0
    error: str = ""
    verdict: Verdict | None = None


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


def _foutbericht(exc: Exception) -> tuple[int, str]:
    """(http-status, kort bericht). De body telt: een uitgezet model meldt zich
    als `model_decommissioned` in de JSON en niet in de statusregel."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = (exc.response.text or "").strip().replace("\n", " ")
        return exc.response.status_code, (body or str(exc))[:300]
    return 0, f"{type(exc).__name__}: {exc}"[:300]


class LLMRouter:
    def __init__(self, providers: list[ProviderState], timeout: int = 20):
        self.providers = providers
        self.timeout = timeout

    def provider(self, name: str) -> ProviderState | None:
        return next((p for p in self.providers if p.name == name), None)

    def _call(self, p: ProviderState, prompt: str) -> tuple[Verdict, int]:
        t0 = time.monotonic()
        # Budget vóór het verzoek ophogen, niet erna. Tot v0.22.0 telde alleen een
        # GESLAAGDE call mee, waardoor een provider die structureel weigert zijn
        # dagbudget nooit opmaakte en bij elke kandidaat opnieuw werd geprobeerd.
        p.used_today += 1
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
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        latency = int((time.monotonic() - t0) * 1000)
        v = Verdict(bool(data["agree"]), float(data["confidence"]),
                    str(data.get("reasoning", ""))[:2000], provider=p.name)
        return v, latency

    def attempt(self, p: ProviderState, prompt: str, *, purpose: str = "veto") -> Attempt:
        """Eén poging, altijd vastgelegd in `llm_attempts`, nooit gooiend."""
        t0 = time.monotonic()
        try:
            verdict, latency = self._call(p, prompt)
            out = Attempt(p.name, p.model, True, latency, verdict=verdict)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
            status, bericht = _foutbericht(exc)
            latency = int((time.monotonic() - t0) * 1000)
            out = Attempt(p.name, p.model, False, latency, http_status=status, error=bericht)
            log.warning("LLM provider %s (%s) faalde: %s", p.name, p.model, bericht)
        self._log_attempt(out, purpose)
        return out

    @staticmethod
    def _log_attempt(a: Attempt, purpose: str) -> None:
        # Loggen mag de handel nooit breken: een volle schijf of een gelockte DB
        # is geen reden om een cyclus te laten klappen.
        try:
            with session() as s:
                s.add(LLMAttemptRow(provider=a.provider, model=a.model, purpose=purpose,
                                    ok=a.ok, http_status=a.http_status,
                                    error=a.error, latency_ms=a.latency_ms))
                s.commit()
        except Exception:  # noqa: BLE001
            log.exception("kon LLM-poging niet loggen")

    def probe(self, name: str) -> Attempt:
        """Testknop: één echte call langs precies het productiepad.

        Schrijft bewust GEEN `llm_calls`-rij. Een testoordeel over een verzonnen
        kandidaat is geen second opinion over een echte koop en hoort niet in de
        meetreeks van de veto-gate te belanden.
        """
        p = self.provider(name)
        if p is None:
            return Attempt(name, "", False, 0,
                           error="provider niet geconfigureerd of geen API-sleutel")
        return self.attempt(p, PROBE_PROMPT, purpose="test")

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
            result = self.attempt(p, prompt)
            if not result.ok or result.verdict is None:
                continue
            verdict = result.verdict
            # Dezelfde hash als waarop `analysis/veto.py` filtert. Zie de
            # modulekop: hier stond de globale hash, en dat maakte elke
            # gescopede veto-meting structureel leeg.
            try:
                chash = gate_fingerprint(get_config(), "veto")
            except Exception:  # noqa: BLE001 - hash mag het loggen nooit breken
                chash = ""
            with session() as s:
                s.add(LLMCallRow(provider=p.name, model=p.model, market=candidate.market,
                                 verdict="agree" if verdict.agree else "veto",
                                 confidence=verdict.confidence,
                                 reasoning=verdict.reasoning, latency_ms=result.latency_ms,
                                 config_hash=chash))
                s.commit()
            return verdict
        log.error("All LLM providers failed or exhausted budget")
        return None


def build_router(cfg_providers, secrets, timeout: int) -> LLMRouter:
    keys = {"groq": secrets.groq_api_key, "gemini": secrets.gemini_api_key,
            "mistral": secrets.mistral_api_key}
    states = [ProviderState(p.name, p.model, keys.get(p.name, ""), p.daily_budget)
              for p in cfg_providers if keys.get(p.name)]
    return LLMRouter(states, timeout)
