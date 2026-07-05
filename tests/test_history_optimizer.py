from tradebot.backtest import max_drawdown_pct
from tradebot.config import AppConfig
from tradebot.exchange import BitvavoClient
from tradebot.optimizer import GRID, variants


class FakeHistoryClient(BitvavoClient):
    """Serveert synthetische candle-pagina's om de paginatie te testen."""

    def __init__(self, total_available=3000):
        super().__init__()
        self.total = total_available
        self.calls = 0

    def _request(self, method, path, body=None, auth=False):
        self.calls += 1
        import urllib.parse as up
        qs = up.parse_qs(up.urlparse(path).query)
        limit = int(qs["limit"][0])
        end = int(qs["end"][0]) if "end" in qs else self.total * 1000
        newest = end // 1000
        rows = [[t * 1000, 1, 1, 1, 1, 1]
                for t in range(newest, max(newest - limit, 0), -1)]
        return rows


def test_history_pagination_fetches_beyond_1440():
    client = FakeHistoryClient()
    candles = client.get_candles_history("BTC-EUR", "4h", 3000)
    assert len(candles) == 3000
    assert client.calls == 3  # 1440 + 1440 + 120
    ts = [c.ts for c in candles]
    assert ts == sorted(ts)          # oplopend
    assert len(set(ts)) == len(ts)   # geen duplicaten over paginagrenzen


def test_history_single_page_when_small():
    client = FakeHistoryClient()
    candles = client.get_candles_history("BTC-EUR", "4h", 100)
    assert len(candles) == 100
    assert client.calls == 1


def test_max_drawdown():
    assert max_drawdown_pct([100, 120, 90, 110]) == 25.0  # 120 -> 90
    assert max_drawdown_pct([100, 110, 120]) == 0.0


def make_cfg() -> AppConfig:
    return AppConfig(markets=["BTC-EUR"], schedule={}, fees={},
                     strategy={"ema_fast": 12, "ema_slow": 26, "min_signal_score": 3},
                     decision={"atr_stop_multiplier": 2.0, "reward_risk_ratio": 2.0},
                     risk={}, llm={})


def test_variants_cover_full_grid_and_apply_overrides():
    cfg = make_cfg()
    combos = list(variants(cfg))
    expected = (len(GRID["ema"]) * len(GRID["min_signal_score"])
                * len(GRID["atr_stop_multiplier"]) * len(GRID["reward_risk_ratio"]))
    assert len(combos) == expected
    desc, c = combos[0]
    assert (c.strategy["ema_fast"], c.strategy["ema_slow"]) == GRID["ema"][0]
    assert cfg.strategy["ema_fast"] == 12  # origineel onaangetast (deep copy)
