from tradebot.config import AppConfig
from tradebot.decision import Position
from tradebot.exchange import Candle
from tradebot.web import build_chart_payload


def make_cfg() -> AppConfig:
    return AppConfig(markets=["BTC-EUR"], schedule={"candle_interval": "4h"},
                     strategy={"ema_fast": 12, "ema_slow": 26},
                     fees={}, decision={}, risk={}, llm={})


def make_candles(n=50):
    return [Candle(i * 1000, 100 + i, 101 + i, 99 + i, 100 + i, 1.0) for i in range(n)]


def test_chart_payload_shapes():
    d = build_chart_payload("BTC-EUR", make_candles(), make_cfg())
    assert len(d["close"]) == len(d["ema_fast"]) == len(d["ema_slow"]) == len(d["ts"]) == 50
    assert d["position"] is None
    assert d["interval"] == "4h"


def test_chart_payload_carries_ohlc_for_candlesticks():
    """Een lijn van slotkoersen verbergt de intrabar-uitslag, en juist daar gaan SL
    en TP op af. De front-end tekent daarom candles en heeft open/high/low nodig."""
    candles = make_candles()
    d = build_chart_payload("BTC-EUR", candles, make_cfg())
    for key in ("open", "high", "low"):
        assert len(d[key]) == 50, key
    assert d["open"][0] == candles[0].open
    assert d["high"][7] == candles[7].high
    assert d["low"][7] == candles[7].low
    # De front-end labelt de EMA-lijnen met hun periode; zonder deze velden zou
    # de legenda "EMAundefined" tonen zodra de config verandert.
    assert d["ema_fast_period"] == 12
    assert d["ema_slow_period"] == 26


def test_chart_payload_with_position():
    from datetime import datetime, timezone
    pos = Position("BTC-EUR", 1.0, 120.0, 110.0, 140.0, datetime.now(timezone.utc))
    d = build_chart_payload("BTC-EUR", make_candles(), make_cfg(), pos)
    assert d["position"] == {"entry": 120.0, "stop_loss": 110.0, "take_profit": 140.0}


def test_ema_fast_tracks_price_closer_than_slow():
    d = build_chart_payload("BTC-EUR", make_candles(), make_cfg())
    last_close = d["close"][-1]
    assert abs(d["ema_fast"][-1] - last_close) < abs(d["ema_slow"][-1] - last_close)


# --- versie in de dashboard-header ------------------------------------------------

def test_mode_endpoint_reports_the_running_version(memory_db):
    """De Pi loopt achter tot de HA-add-on is bijgewerkt, en dan wijkt het gedrag af
    van wat in git staat. Zonder dit veld is dat verschil op het dashboard onzichtbaar,
    en dat is precies de deploy-richting van de configdrift uit review-blok 3."""
    from tradebot import __version__
    from tradebot.web import mode

    assert mode()["version"] == __version__


def test_dashboard_header_renders_the_version():
    """Het API-veld alleen is niet genoeg: verdwijnt de span uit de header, dan staat
    de versie er wel in de response maar niet op het scherm."""
    from tradebot.web import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'id="ver"' in page
    assert "botVersion" in script


def test_mode_endpoint_carries_the_run_label(memory_db):
    """De statusbalk bovenaan leest `run_purpose` en `run_until`. Zolang de lopende
    run een infrastructuurtest is, mogen P&L en win-rate niet als strategiebewijs
    gelezen worden; zonder deze velden staat dat label nergens op het scherm."""
    from tradebot.web import mode

    md = mode()
    assert "run_purpose" in md
    assert "run_until" in md
    assert set(md["gates"]) == {"veto", "regime", "breakeven", "chase", "timestop"}


# --- front-end als losse bestanden -----------------------------------------

def test_static_assets_are_present_and_wired():
    """De front-end zit sinds v0.22.0 niet meer als string in web.py. Deze test
    bewaakt dat de assets bestaan en dat index.html ze ook echt binnenhaalt: een
    hernoemd bestand zou anders pas in de browser opvallen.

    De paden zijn bewust RELATIEF (geen leading slash). Achter HA-ingress draait de
    app onder een prefix, en een absoluut pad zou daar buiten wijzen."""
    from tradebot.web import STATIC_DIR

    assets = ["app.css", "app.js", "charts.js",
              "vendor/uPlot.iife.min.js", "vendor/uPlot.min.css"]
    for name in assets:
        assert (STATIC_DIR / name).is_file(), name
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in assets:
        assert f'"static/{name}"' in page, name
    assert 'href="/static' not in page
    assert 'src="/static' not in page


def test_dashboard_route_serves_the_index_file(memory_db):
    from fastapi.testclient import TestClient

    from tradebot.web import app

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "AI Trade Platform" in r.text
    assert client.get("/static/app.js").status_code == 200
