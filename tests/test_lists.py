import pytest

from tradebot.config import AppConfig
from tradebot.lists import MAX_MARKETS, get_lists, modify


def make_cfg() -> AppConfig:
    return AppConfig(markets=["BTC-EUR", "ETH-EUR"], watchlist=["SOL-EUR"],
                     schedule={}, strategy={}, fees={}, decision={}, risk={}, llm={})


@pytest.fixture()
def cfg(memory_db):
    return make_cfg()


def test_blocklist_rejects_add(memory_db):
    cfg = AppConfig(markets=["BTC-EUR", "ETH-EUR"], watchlist=["SOL-EUR"],
                    blocklist=["DOGE-EUR"], schedule={}, strategy={}, fees={},
                    decision={}, risk={}, llm={})
    ok, msg = modify(cfg, "watchlist", "doge-eur", "add")
    assert not ok and "banlijst" in msg


def test_fallback_to_config_without_override(cfg):
    state = get_lists(cfg)
    assert state["markets"] == ["BTC-EUR", "ETH-EUR"]
    assert state["source"] == "config"


def test_add_to_watchlist_and_promote(cfg):
    ok, _ = modify(cfg, "watchlist", "ada-eur", "add")
    assert ok
    assert "ADA-EUR" in get_lists(cfg)["watchlist"]
    ok, _ = modify(cfg, "markets", "ADA-EUR", "add")  # promoveren
    assert ok
    state = get_lists(cfg)
    assert "ADA-EUR" in state["markets"]
    assert "ADA-EUR" not in state["watchlist"]  # automatisch verplaatst
    assert state["source"] == "gui"


def test_markets_cap_enforced(cfg):
    # Fixture start met 2 markten; vul aan tot de cap (MAX_MARKETS) is bereikt.
    fillers = ["A-EUR", "B-EUR", "C-EUR", "D-EUR", "E-EUR", "F-EUR", "G-EUR",
               "H-EUR", "I-EUR", "J-EUR"][: MAX_MARKETS - 2]
    for m in fillers:
        assert modify(cfg, "markets", m, "add")[0]
    assert len(get_lists(cfg)["markets"]) == MAX_MARKETS
    ok, msg = modify(cfg, "markets", "Z-EUR", "add")
    assert not ok and "max" in msg


def test_minimum_one_trading_market(cfg):
    assert modify(cfg, "markets", "ETH-EUR", "remove")[0]
    ok, msg = modify(cfg, "markets", "BTC-EUR", "remove")
    assert not ok and "minimaal 1" in msg


def test_invalid_notation_rejected(cfg):
    ok, msg = modify(cfg, "markets", "bitcoin", "add")
    assert not ok and "ongeldige" in msg


def test_duplicate_add_rejected(cfg):
    ok, _ = modify(cfg, "watchlist", "SOL-EUR", "add")
    assert not ok
