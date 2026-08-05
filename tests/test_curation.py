"""Tests voor de curatie (v0.18.0): time-stop, banlijst (via lists), quiet-vlag."""
from datetime import datetime, timedelta, timezone

from tradebot.exchange import Candle
from tradebot.strategy import time_stop_hit

STEP_MS = 4 * 3600 * 1000
START = 1_700_000_000_000


def _candles(n_after: int):
    """n_after candles die NA het entry-moment (START) liggen."""
    return [Candle(ts=START + i * STEP_MS, open=100, high=101, low=99, close=100, volume=1.0)
            for i in range(1, n_after + 1)]


def _opened_at():
    return datetime.fromtimestamp(START / 1000, tz=timezone.utc)


# --- time-stop --------------------------------------------------------------

def test_time_stop_triggers_on_stale_flat_position():
    hit, why = time_stop_hit(_candles(14), _opened_at(), 100.0, 100.0,
                             round_trip_pct=0.5, n_candles=12, min_net_pct=0.0)
    assert hit and "time-stop" in why


def test_time_stop_spares_winner():
    # +10% staat ruim boven break-even -> niet knippen, ook al is het lang.
    hit, _ = time_stop_hit(_candles(14), _opened_at(), 100.0, 110.0,
                           round_trip_pct=0.5, n_candles=12, min_net_pct=0.0)
    assert not hit


def test_time_stop_waits_for_n_candles():
    # Pas 5 candles verstreken -> nog niet exiten, ook al staat het onder break-even.
    hit, _ = time_stop_hit(_candles(5), _opened_at(), 100.0, 99.0,
                           round_trip_pct=0.5, n_candles=12, min_net_pct=0.0)
    assert not hit


def test_time_stop_disabled_when_n_zero():
    hit, _ = time_stop_hit(_candles(50), _opened_at(), 100.0, 90.0,
                           round_trip_pct=0.5, n_candles=0, min_net_pct=0.0)
    assert not hit


# --- quiet-vlag -------------------------------------------------------------

def test_quiet_flags_market_without_buy_signals(memory_db):
    from tradebot.db import SignalRow, session
    from tradebot.web import _quiet_markets
    now = datetime.now(timezone.utc)
    with session() as s:
        # BTC: recent koopsignaal -> niet quiet.
        s.add(SignalRow(market="BTC-EUR", action="buy", decision="buy", score=3,
                        reason="signaal", details={}))
        # SOL: oud koopsignaal (>30d) -> quiet met dagen.
        s.add(SignalRow(market="SOL-EUR", action="buy", decision="skip", score=3,
                        reason="oud", details={}, ts=now - timedelta(days=45)))
        s.commit()
    quiet = _quiet_markets(["BTC-EUR", "ETH-EUR", "SOL-EUR"], quiet_days=30)
    by = {q["market"]: q["days"] for q in quiet}
    assert "BTC-EUR" not in by            # recent -> niet gevlagd
    assert by.get("ETH-EUR", "missing") is None   # nooit een signaal
    assert by["SOL-EUR"] >= 30            # oud signaal, dagen geteld
