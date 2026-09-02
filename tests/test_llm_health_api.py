"""De providerkaart op het dashboard (v0.23.0).

Bestaansreden in één zin: tot v0.22.0 landde een mislukte LLM-call nergens, dus
was een dode provider van buiten niet te onderscheiden van een stille markt. Deze
tests bewaken dat het verschil zichtbaar blijft.
"""
import pytest
import respx
from fastapi.testclient import TestClient

from tradebot.llm import BASE_URLS, LLMRouter, ProviderState
from tradebot.web import app


@pytest.fixture()
def client(tmp_path):
    """Bewust een DB op schijf en niet de `memory_db`-fixture: TestClient draait de
    app in een eigen thread, en een sqlite `:memory:` is per verbinding een eigen
    database. De endpoints zouden dan tegen een lege, tabelloze DB praten.

    De router wordt expliciet gezet omdat `get_router` hem op `app.state` cachet,
    en die state overleeft een test.
    """
    from tradebot import db

    db._engine = db._Session = None
    db.init_db(f"sqlite:///{tmp_path / 'tradebot.db'}")
    app.state.llm_router = LLMRouter([
        ProviderState("groq", "openai/gpt-oss-20b", "key", 200),
        ProviderState("gemini", "gemini-2.5-flash", "", 100),
    ])
    yield TestClient(app)
    app.state.llm_router = None
    db._engine = db._Session = None


def keten(client) -> dict:
    return {r["provider"]: r for r in client.get("/api/llm/health").json()["chain"]}


def test_health_reports_a_chain_and_not_a_single_provider(client):
    """Er is geen 'geselecteerde provider': de keten wordt op volgorde afgelopen.
    Het dashboard moet dat tonen, anders zoekt iemand naar een keuze die er niet is."""
    body = client.get("/api/llm/health").json()
    assert [r["provider"] for r in body["chain"]] == ["groq", "gemini", "mistral"]
    assert [r["order"] for r in body["chain"]] == [1, 2, 3]


def test_a_provider_without_a_key_is_marked_and_is_never_the_active_one(client):
    rijen = keten(client)
    assert rijen["gemini"]["key_present"] is False
    assert rijen["gemini"]["status"] == "geen sleutel"
    assert client.get("/api/llm/health").json()["active"] == "groq"


@respx.mock
def test_a_failure_shows_up_with_status_and_message(client):
    """De storing die aanleiding was: Groq zet een model uit, de call faalt, en tot
    v0.22.0 zag je daar niets van terug."""
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(
        status_code=404, json={"error": {"code": "model_decommissioned"}})
    r = client.post("/api/llm/test", json={"provider": "groq"}).json()
    assert r["ok"] is False and r["http_status"] == 404
    assert "model_decommissioned" in r["error"]

    rij = keten(client)["groq"]
    assert rij["status"] == "fout"
    assert rij["last_error"]["http_status"] == 404
    assert rij["last_ok"] is None


@respx.mock
def test_a_successful_test_flips_the_status_back_to_ok(client):
    respx.post(f"{BASE_URLS['groq']}/chat/completions").respond(json={
        "choices": [{"message": {"content":
            '{"agree": true, "confidence": 0.7, "reasoning": "prima"}'}}]})
    r = client.post("/api/llm/test", json={"provider": "groq"}).json()
    assert r["ok"] is True and r["verdict"]["agree"] is True
    rij = keten(client)["groq"]
    assert rij["status"] == "ok" and rij["last_ok"] is not None


def test_health_says_whether_the_layer_is_switched_on_at_all(client):
    """Het antwoord op de vraag die vier weken open stond. `use_llm_second_opinion`
    staat sinds 2026-08-06 op false; zonder dit veld leest een lege tabel als een
    storing en niet als een besluit."""
    body = client.get("/api/llm/health").json()
    assert body["enabled"] is False
    assert body["run_purpose"] == "infrastructuurtest"
