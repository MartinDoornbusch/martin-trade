import httpx
import respx

from tradebot.llm import BASE_URLS, LLMRouter, ProviderState
from tradebot.strategy import Candidate, MarketSnapshot


def make_candidate() -> Candidate:
    snap = MarketSnapshot("BTC-EUR", 100.0, 101, 100, 40, 0.5, -0.1, 1.0, 98, 100, 1.0)
    return Candidate("BTC-EUR", "buy", 4, ["test"], snap)


def verdict_response(agree=True, confidence=0.8):
    return {
        "choices": [{"message": {"content":
            f'{{"agree": {str(agree).lower()}, "confidence": {confidence}, "reasoning": "ok"}}'}}]
    }


@respx.mock
def test_primary_provider_used(memory_db):
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(json=verdict_response())
    router = LLMRouter([ProviderState("groq", "llama-3.1-8b-instant", "key", 100)])
    v = router.second_opinion(make_candidate())
    assert v.agree is True
    assert v.provider == "groq"


@respx.mock
def test_fallback_on_provider_error(memory_db):
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(status_code=429)
    respx.post(f"{BASE_URLS['gemini']}/chat/completions").respond(
        json=verdict_response(agree=False, confidence=0.9))
    router = LLMRouter([
        ProviderState("groq", "llama-3.1-8b-instant", "key", 100),
        ProviderState("gemini", "gemini-2.5-flash", "key", 100),
    ])
    v = router.second_opinion(make_candidate())
    assert v.provider == "gemini"
    assert v.agree is False


@respx.mock
def test_budget_exhaustion_skips_provider(memory_db):
    respx.post(f"{BASE_URLS['mistral']}/chat/completions").respond(json=verdict_response())
    exhausted = ProviderState("groq", "m", "key", daily_budget=1)
    exhausted.available()  # sets day
    exhausted.used_today = 1
    router = LLMRouter([exhausted, ProviderState("mistral", "m", "key", 100)])
    v = router.second_opinion(make_candidate())
    assert v.provider == "mistral"


@respx.mock
def test_all_fail_returns_none(memory_db):
    respx.post(f"{BASE_URLS['groq']}/chat/completions").mock(
        side_effect=httpx.ConnectError("down"))
    router = LLMRouter([ProviderState("groq", "m", "key", 100)])
    assert router.second_opinion(make_candidate()) is None


def test_no_keys_returns_none(memory_db):
    router = LLMRouter([ProviderState("groq", "m", "", 100)])
    assert router.second_opinion(make_candidate()) is None
