import pytest

from tradebot.config import AppConfig, Secrets
from tradebot.engine import LIVE_CONFIRM_PHRASE, TradingCycle
from tradebot.lists import is_paused, set_paused


def make_cfg():
    return AppConfig(markets=["BTC-EUR"], schedule={"candle_interval": "4h", "candle_limit": 200},
                     strategy={}, fees={"maker_pct": 0.15, "taker_pct": 0.25,
                                        "slippage_buffer_pct": 0.1},
                     decision={}, risk={"paper_start_eur": 1000.0, "max_position_pct": 25,
                                        "max_open_positions": 3,
                                        "cooldown_hours_after_trade": 12,
                                        "daily_loss_cap_pct": 3}, llm={})


def test_live_without_confirm_phrase_refused(memory_db):
    secrets = Secrets(trading_mode="live", live_confirm="")
    with pytest.raises(RuntimeError, match="live_confirm"):
        TradingCycle(make_cfg(), secrets)


def test_live_with_wrong_phrase_refused(memory_db):
    secrets = Secrets(trading_mode="live", live_confirm="ik snap het")
    with pytest.raises(RuntimeError):
        TradingCycle(make_cfg(), secrets)


def test_live_with_exact_phrase_accepted(memory_db):
    secrets = Secrets(trading_mode="live", live_confirm=LIVE_CONFIRM_PHRASE)
    cycle = TradingCycle(make_cfg(), secrets)
    from tradebot.live import LiveBroker
    assert isinstance(cycle.broker, LiveBroker)


def test_paper_mode_needs_no_phrase(memory_db):
    cycle = TradingCycle(make_cfg(), Secrets(trading_mode="paper"))
    from tradebot.paper import PaperBroker
    assert isinstance(cycle.broker, PaperBroker)


def test_kill_switch_flag(memory_db):
    assert not is_paused()
    set_paused(True)
    assert is_paused()
    set_paused(False)
    assert not is_paused()
