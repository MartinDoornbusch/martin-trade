import pytest

from tradebot import config as cfgmod


@pytest.fixture()
def clear_cache():
    cfgmod.get_config.cache_clear()
    yield
    cfgmod.get_config.cache_clear()


def test_env_overrides_markets_and_watchlist(monkeypatch, tmp_path, clear_cache):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "markets: [BTC-EUR]\nwatchlist: [SOL-EUR]\nschedule: {}\nstrategy: {}\n"
        "fees: {}\ndecision: {}\nrisk: {}\nllm: {}\n")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", yaml_file)
    monkeypatch.setenv("TRADEBOT_MARKETS", "btc-eur, eth-eur")
    monkeypatch.setenv("TRADEBOT_WATCHLIST", "ada-eur")
    cfg = cfgmod.get_config()
    assert cfg.markets == ["BTC-EUR", "ETH-EUR"]   # genormaliseerd naar uppercase
    assert cfg.watchlist == ["ADA-EUR"]


def test_empty_env_keeps_yaml_values(monkeypatch, tmp_path, clear_cache):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "markets: [BTC-EUR]\nwatchlist: [SOL-EUR]\nschedule: {}\nstrategy: {}\n"
        "fees: {}\ndecision: {}\nrisk: {}\nllm: {}\n")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", yaml_file)
    monkeypatch.delenv("TRADEBOT_MARKETS", raising=False)
    monkeypatch.delenv("TRADEBOT_WATCHLIST", raising=False)
    cfg = cfgmod.get_config()
    assert cfg.markets == ["BTC-EUR"]
    assert cfg.watchlist == ["SOL-EUR"]
