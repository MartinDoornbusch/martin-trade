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


def test_chart_payload_with_position():
    from datetime import datetime, timezone
    pos = Position("BTC-EUR", 1.0, 120.0, 110.0, 140.0, datetime.now(timezone.utc))
    d = build_chart_payload("BTC-EUR", make_candles(), make_cfg(), pos)
    assert d["position"] == {"entry": 120.0, "stop_loss": 110.0, "take_profit": 140.0}


def test_ema_fast_tracks_price_closer_than_slow():
    d = build_chart_payload("BTC-EUR", make_candles(), make_cfg())
    last_close = d["close"][-1]
    assert abs(d["ema_fast"][-1] - last_close) < abs(d["ema_slow"][-1] - last_close)
