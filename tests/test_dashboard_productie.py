"""Productiedashboard (v0.25.2): de besluitentabel toont wat de bot deed, niet
wat een shadow-gate "genegeerd" heeft, en alleen van de mode die draait.
De front-end zelf wordt gecontroleerd door tests/frontend/render.mjs (jsdom)."""
import pytest
from fastapi.testclient import TestClient

from tradebot.db import SignalRow, session
from tradebot.web import app


@pytest.fixture()
def client(tmp_path):
    from tradebot import db
    db._engine = db._Session = None
    db.init_db(f"sqlite:///{tmp_path / 'tradebot.db'}")
    with session() as s:
        s.add_all([
            SignalRow(market="A-EUR", action="buy", decision="buy", reason="koop", mode="paper"),
            SignalRow(market="A-EUR", action="sell", decision="shadow",
                      reason="SHADOW-TIMESTOP genegeerd", mode="paper"),
            SignalRow(market="B-EUR", action="buy", decision="buy", reason="live koop", mode="live"),
        ])
        s.commit()
    yield TestClient(app)
    db._engine = db._Session = None


def test_besluiten_zonder_meetregels(client):
    rows = client.get("/api/signals?shadow=false").json()
    assert [r["decision"] for r in rows] == ["buy"]


def test_besluiten_alleen_van_de_draaiende_mode(client):
    rows = client.get("/api/signals").json()          # trading_mode = paper (default)
    assert {r["market"] for r in rows} == {"A-EUR"}
    assert len(rows) == 2                              # zonder filter blijft shadow zichtbaar


def test_dashboard_heeft_geen_meetgates_meer():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "src/tradebot/static/index.html").read_text("utf-8")
    for weg in ('data-tab="gates"', 'id="gatesum"', 'id="llmhealth"', 'id="vetoanalysis"'):
        assert weg not in html
