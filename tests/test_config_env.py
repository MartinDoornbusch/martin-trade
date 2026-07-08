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


def test_numeric_env_overrides(monkeypatch, tmp_path, clear_cache):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "markets: [BTC-EUR]\nschedule: {analysis_interval_minutes: 60, candle_interval: 4h}\n"
        "strategy: {}\nfees: {}\ndecision: {}\n"
        "risk: {max_position_pct: 25, max_open_positions: 3, cooldown_hours_after_trade: 12}\n"
        "llm: {}\n")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", yaml_file)
    monkeypatch.setenv("TRADEBOT_INTERVAL_MINUTES", "30")
    monkeypatch.setenv("TRADEBOT_CANDLE_INTERVAL", "1h")
    monkeypatch.setenv("TRADEBOT_MAX_POSITION_PCT", "10")
    monkeypatch.setenv("TRADEBOT_MAX_OPEN_POSITIONS", "2")
    monkeypatch.setenv("TRADEBOT_COOLDOWN_HOURS", "24")
    cfg = cfgmod.get_config()
    assert cfg.schedule["analysis_interval_minutes"] == 30
    assert cfg.schedule["candle_interval"] == "1h"
    assert cfg.risk["max_position_pct"] == 10.0
    assert cfg.risk["max_open_positions"] == 2.0
    assert cfg.risk["cooldown_hours_after_trade"] == 24.0


def test_invalid_numeric_env_ignored(monkeypatch, tmp_path, clear_cache):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "markets: [BTC-EUR]\nschedule: {analysis_interval_minutes: 60, candle_interval: 4h}\n"
        "strategy: {}\nfees: {}\ndecision: {}\nrisk: {max_position_pct: 25}\nllm: {}\n")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", yaml_file)
    monkeypatch.setenv("TRADEBOT_INTERVAL_MINUTES", "abc")
    cfg = cfgmod.get_config()
    assert cfg.schedule["analysis_interval_minutes"] == 60  # ongeldige waarde genegeerd
