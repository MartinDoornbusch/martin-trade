import httpx
import respx
from sqlalchemy import select

from tradebot.config import gate_fingerprint, get_config
from tradebot.db import LLMAttemptRow, LLMCallRow, session
from tradebot.llm import BASE_URLS, LLMRouter, ProviderState
from tradebot.strategy import Candidate, MarketSnapshot


def rows(model):
    with session() as s:
        return s.execute(select(model).order_by(model.id)).scalars().all()


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


# --- v0.23.0: falen mag niet meer onzichtbaar zijn -------------------------------


@respx.mock
def test_a_failing_provider_leaves_a_trace(memory_db):
    """De kern van de storing die vier weken onopgemerkt bleef.

    Tot v0.22.0 deed een mislukte call niets meer dan een `log.warning`: er kwam
    geen rij in `llm_calls` (dat is en blijft het oordeellogboek) en nergens
    anders ook niet. Een dode provider was daardoor van buiten niet te
    onderscheiden van een markt waarin geen kandidaat opkwam.
    """
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(
        status_code=404, json={"error": {"code": "model_decommissioned"}})
    router = LLMRouter([ProviderState("groq", "llama-3.1-8b-instant", "key", 100)])
    assert router.second_opinion(make_candidate()) is None

    pogingen = rows(LLMAttemptRow)
    assert len(pogingen) == 1
    assert pogingen[0].ok is False
    assert pogingen[0].http_status == 404
    assert "model_decommissioned" in pogingen[0].error
    assert pogingen[0].model == "llama-3.1-8b-instant"
    assert pogingen[0].purpose == "veto"
    # En het oordeellogboek blijft schoon: een fout is geen oordeel.
    assert rows(LLMCallRow) == []


@respx.mock
def test_a_failed_attempt_still_consumes_budget(memory_db):
    """Tot v0.22.0 telde alleen een geslaagde call mee, waardoor een provider die
    structureel weigert zijn dagbudget nooit opmaakte en bij elke kandidaat
    opnieuw als eerste werd geprobeerd."""
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(status_code=429)
    p = ProviderState("groq", "m", "key", 100)
    router = LLMRouter([p])
    router.second_opinion(make_candidate())
    assert p.used_today == 1


@respx.mock
def test_the_verdict_row_carries_the_veto_gate_hash(memory_db):
    """De bug die elke gescopede veto-meting leegmaakte.

    `llm.py` schreef `config_fingerprint(cfg)` (de globale hash van vóór v0.20.0)
    terwijl `analysis/veto.py` filtert op `gate_fingerprint(cfg, "veto")`. Die
    twee verschillen, dus `where(LLMCallRow.config_hash == ...)` matchte nooit en
    de meting gaf structureel nul rijen, ongeacht of de LLM draaide.
    """
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(json=verdict_response())
    router = LLMRouter([ProviderState("groq", "m", "key", 100)])
    router.second_opinion(make_candidate())
    assert rows(LLMCallRow)[0].config_hash == gate_fingerprint(get_config(), "veto")


@respx.mock
def test_probe_tests_the_chain_without_polluting_the_measurement(memory_db):
    """De testknop loopt langs het echte productiepad, maar een oordeel over een
    verzonnen kandidaat hoort niet in de meetreeks van de veto-gate."""
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(json=verdict_response())
    router = LLMRouter([ProviderState("groq", "openai/gpt-oss-20b", "key", 100)])
    a = router.probe("groq")
    assert a.ok is True and a.verdict.agree is True
    assert [r.purpose for r in rows(LLMAttemptRow)] == ["test"]
    assert rows(LLMCallRow) == []


def test_probe_on_an_unconfigured_provider_reports_instead_of_raising(memory_db):
    a = LLMRouter([]).probe("groq")
    assert a.ok is False and "niet geconfigureerd" in a.error
