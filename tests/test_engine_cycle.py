"""Integratietests op de bedrading van de analysecyclus.

Draaien de echte `TradingCycle.run_once` en `TradingCycle.check_exits_fast` tegen
een fake feed en een echte `PaperBroker` op een in-memory DB. Geen netwerk, geen
LLM, geen scanner: dit test de volgorde en de doorgifte van state tussen de
gates, niet de gates zelf (die zijn per stuk getest in test_strategy/test_decision).

Bewuste keuze: de koopdrempel wordt via de config gestuurd (`min_signal_score: 1`
op een stijgende reeks) in plaats van indicatorwaarden te fabriceren die precies
score 3 opleveren. Zo blijft de test leesbaar en breekt hij niet op een
scoring-herontwerp; de scoring zelf hoort in test_strategy.py.

Test-naar-bug (code review blok 1):

| Bug | Test |
|-----|------|
| 1.1 slotlimiet overschreden binnen één cyclus | `test_slot_limit_not_exceeded_within_one_cycle` |
| 1.1 vierde positie kreeg een restbedrag i.p.v. de volle bucket | `test_every_position_gets_a_full_bucket` |
| 1.1 cluster-cap telde posities uit dezelfde cyclus niet mee | `test_cluster_cap_counts_positions_from_same_cycle` |
| 1.1 vrijgekomen slot na een exit werd niet gezien | `test_exit_frees_a_slot_for_a_later_market_in_the_same_cycle` |
| 1.2 breakeven-stop in shadow mag niet verkopen | `test_breakeven_stop_shadow_logs_but_keeps_position` |
| 1.2 breakeven-stop bindend moet wel verkopen | `test_breakeven_stop_binding_closes_position` |
| (v0.18.0) time-stop sluit een stilstaande positie | `test_time_stop_closes_stalled_position` |
| (v0.18.0) blocklist blokkeert buys, niet exits | `test_blocklist_blocks_buy_but_not_exit` |
| (v0.11.0) kill-switch blokkeert buys, niet exits | `test_kill_switch_blocks_buy_but_not_exit` |
| exits draaien vóór entries | `test_exit_runs_before_entry_in_same_market` |
| 1.1 entry beoordeeld op de lopende candle | `test_entry_signal_ignores_running_candle` |
| 1.1 exit-route mocht niet meeveranderen | `test_exit_still_uses_the_running_candle` |
| 1.1 oud gedrag blijft meetbaar via de schakelaar | `test_switch_false_restores_signal_on_running_candle` |
| 1.1 SL/TP-anker moet de live prijs blijven | `test_levels_are_anchored_on_the_live_price` |
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tradebot.db import PositionRow, SignalRow, session
from tradebot.engine import TradingCycle
from tradebot.exchange import Candle, ExchangeAdapter, OrderResult
from tradebot.lists import set_paused
from tradebot.paper import PaperBroker

STEP_MS = 4 * 3600 * 1000
N_CANDLES = 80


def rising(start: float = 100.0, step_pct: float = 1.0, n: int = N_CANDLES) -> list[float]:
    """Stijgende reeks: garandeert EMA-snel > EMA-traag, dus een koopsignaal
    bij `min_signal_score: 1`."""
    return [start * (1 + step_pct / 100) ** i for i in range(n)]


def falling(start: float = 100.0, step_pct: float = 1.0, n: int = N_CANDLES) -> list[float]:
    """Dalende reeks: eindigt ruim onder een stop loss net onder de entry.

    De exit-stap van de cyclus kijkt naar `snap.price` (de laatste close), niet
    naar `feed.get_price`; een exit-test moet dus de reeks laten zakken.
    """
    return [start * (1 - step_pct / 100) ** i for i in range(n)]


def flat(level: float = 100.0, n: int = N_CANDLES) -> list[float]:
    """Volledig vlakke reeks. De candle-highs/lows geven wel ATR, zodat een
    positie hier per saldo op break-even min de fees blijft hangen."""
    return [level] * n


class FakeFeed(ExchangeAdapter):
    """Feed met vooraf bepaalde closes per markt. Candles eindigen 'nu', zodat
    `opened_at` van een verse positie tussen de laatste candles valt."""

    def __init__(self, series: dict[str, list[float]], prices: dict[str, float] | None = None):
        self.series = series
        self.prices = prices or {}
        self.calls: list[str] = []

    def _closes(self, market: str) -> list[float]:
        if market not in self.series:
            raise KeyError(f"geen fake-serie voor {market}")
        return self.series[market]

    def get_candles(self, market: str, interval: str, limit: int) -> list[Candle]:
        closes = self._closes(market)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        first = now_ms - (len(closes) - 1) * STEP_MS
        # High/low ruim om de entry: ATR ~4% van de prijs, dus de fee-gate
        # (>= 1,10% verwachte move) bindt hier niet. Die gate is apart getest.
        return [Candle(ts=first + i * STEP_MS, open=c, high=c * 1.02,
                       low=c * 0.98, close=c, volume=1000.0)
                for i, c in enumerate(closes)]

    def get_price(self, market: str) -> float:
        self.calls.append(market)
        return self.prices.get(market, self._closes(market)[-1])

    def get_balances(self) -> dict[str, float]:
        return {}

    def place_market_order(self, market, side, amount_quote) -> OrderResult:
        raise NotImplementedError

    def get_fees_pct(self):
        return 0.15, 0.25


def make_cfg(markets: list[str], **over) -> SimpleNamespace:
    risk = {"paper_start_eur": 1000.0, "sizing": "percent", "bucket_eur": 250.0,
            "max_position_pct": 25.0, "max_open_positions": 3,
            "cooldown_hours_after_trade": 0, "daily_loss_cap_pct": 100.0,
            "max_correlation": 0.85, "correlation_lookback": 60,
            "max_correlated_positions": 99}
    risk.update(over.pop("risk", {}))
    strategy = {"ema_fast": 3, "ema_slow": 5, "rsi_period": 14, "atr_period": 14,
                "rsi_buy_zone_min": 25, "rsi_buy_zone_max": 45,
                "rsi_overbought": 70, "min_signal_score": 1}
    strategy.update(over.pop("strategy_over", {}))
    cfg = SimpleNamespace(
        markets=markets,
        watchlist=[],
        blocklist=over.pop("blocklist", []),
        schedule={"candle_interval": "4h", "candle_limit": N_CANDLES},
        strategy=strategy,
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        decision={"min_profit_pct": 0.50, "atr_stop_multiplier": 2.0,
                  "reward_risk_ratio": 1.5, "use_llm_second_opinion": False,
                  "llm_min_confidence": 0.6, "llm_veto_binding": False},
        risk=risk,
        universe={"auto_fill": False},
        exits=over.pop("exits", {}),
        curation={},
        regime={"enabled": False, "proxy_market": "BTC-EUR", "binding": False},
        llm={"timeout_seconds": 20},
        llm_providers=[],
    )
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def make_secrets() -> SimpleNamespace:
    return SimpleNamespace(
        trading_mode="paper", bitvavo_api_key="", bitvavo_api_secret="",
        groq_api_key="", gemini_api_key="", mistral_api_key="",
        telegram_bot_token="", telegram_chat_id="",
        live_confirm="", live_max_capital_eur=100.0)


def make_cycle(cfg, feed: FakeFeed, start_eur: float = 1000.0) -> TradingCycle:
    """Echte TradingCycle, maar met de fake feed voor zowel analyse als broker."""
    cycle = TradingCycle(cfg, make_secrets())
    cycle.feed = feed
    cycle.broker = PaperBroker(feed, cycle.fee_model, start_eur)
    return cycle


def backdate_position(market: str, hours: float) -> None:
    """Zet `opened_at` terug, zodat candles ná entry meetellen (time-stop)."""
    with session() as s:
        row = s.execute(select(PositionRow).where(PositionRow.market == market)
                        ).scalar_one()
        row.opened_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        s.commit()


def open_markets() -> set[str]:
    with session() as s:
        return {r.market for r in s.execute(select(PositionRow)).scalars().all()}


def shadow_signals() -> list[SignalRow]:
    with session() as s:
        return list(s.execute(select(SignalRow).where(SignalRow.decision == "shadow")
                              ).scalars().all())


@pytest.fixture(autouse=True)
def _unpause(memory_db):
    """Elke test start met de kill-switch uit (KV staat in de memory-DB)."""
    set_paused(False)
    yield


# --- 1.1 slotlimiet en positiegrootte -------------------------------------------

def test_slot_limit_not_exceeded_within_one_cycle():
    """Vier koopsignalen in één cyclus, drie slots: er mogen er drie open gaan.

    Vóór de fix las `run_once` de positielijst één keer vóór de marktloop,
    waardoor `RiskManager.can_open` bij elke markt dezelfde lege lijst zag en
    alle vier de posities open gingen.
    """
    markets = ["A-EUR", "B-EUR", "C-EUR", "D-EUR"]
    feed = FakeFeed({m: rising(start=100.0 + 10 * i) for i, m in enumerate(markets)})
    cfg = make_cfg(markets, risk={"max_open_positions": 3, "max_position_pct": 20.0})
    decisions = make_cycle(cfg, feed).run_once()

    assert len(open_markets()) == 3
    assert sum(1 for d in decisions if d.action == "buy") == 3
    skipped = [d for d in decisions if d.action == "skip"]
    assert any("max open positions" in d.reason for d in skipped)


def test_every_position_gets_a_full_bucket():
    """In bucket-modus krijgt elke geopende positie het volle bucketbedrag.

    Regressie op het tweede gevolg van bug 1.1: doordat `positions` stil stond
    maar `free` wel daalde, kon een extra positie geopend worden met wat er nog
    aan cash over was in plaats van met een volle bucket.
    """
    markets = ["A-EUR", "B-EUR", "C-EUR", "D-EUR"]
    feed = FakeFeed({m: rising(start=100.0 + 10 * i) for i, m in enumerate(markets)})
    cfg = make_cfg(markets, risk={"sizing": "bucket", "bucket_eur": 250.0,
                                  "max_open_positions": 3})
    cycle = make_cycle(cfg, feed)
    cycle.run_once()

    with session() as s:
        rows = s.execute(select(PositionRow)).scalars().all()
    assert len(rows) == 3
    for row in rows:
        ingelegd = row.amount * row.entry_price + row.fees_paid_eur
        assert ingelegd == pytest.approx(250.0, abs=0.01)
    assert cycle.broker.cash_eur() == pytest.approx(250.0, abs=0.01)


def test_cluster_cap_counts_positions_from_same_cycle():
    """Drie identiek bewegende markten, cluster-cap 2: de derde wordt geweigerd.

    Vóór de fix zat een in dezelfde cyclus geopende positie niet in `others`,
    waardoor de correlatie-cluster-cap binnen één cyclus overschreden kon worden.
    """
    markets = ["A-EUR", "B-EUR", "C-EUR"]
    serie = rising()
    feed = FakeFeed({m: list(serie) for m in markets})
    cfg = make_cfg(markets, risk={"max_open_positions": 5, "max_position_pct": 15.0,
                                  "max_correlated_positions": 2})
    decisions = make_cycle(cfg, feed).run_once()

    assert len(open_markets()) == 2
    assert any(d.action == "skip" and "correlatie-gate" in d.reason for d in decisions)


def test_exit_frees_a_slot_for_a_later_market_in_the_same_cycle():
    """Een exit in markt A geeft zijn slot binnen dezelfde cyclus vrij aan B.

    Spiegelbeeld van de slotlimiet-bug: zonder verversing zou de zojuist gesloten
    positie nog meetellen en zou B onterecht op "max open positions" stranden.
    """
    feed = FakeFeed({"A-EUR": falling(), "B-EUR": rising(start=50.0)})
    cfg = make_cfg(["A-EUR", "B-EUR"], risk={"max_open_positions": 1,
                                             "max_position_pct": 25.0})
    cycle = make_cycle(cfg, feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=90.0, take_profit=9999.0, reason="setup")

    decisions = cycle.run_once()

    assert open_markets() == {"B-EUR"}
    assert [d.action for d in decisions] == ["sell", "buy"]


# --- exits vóór entries ---------------------------------------------------------

def test_exit_runs_before_entry_in_same_market():
    """Een markt die uitstapt wordt in diezelfde cyclus niet opnieuw gekocht."""
    feed = FakeFeed({"A-EUR": falling()})
    cycle = make_cycle(make_cfg(["A-EUR"]), feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=90.0, take_profit=9999.0, reason="setup")

    decisions = cycle.run_once()

    assert [d.action for d in decisions] == ["sell"]
    assert open_markets() == set()


# --- blocklist en kill-switch ---------------------------------------------------

def test_blocklist_blocks_buy_but_not_exit():
    feed = FakeFeed({"A-EUR": falling(), "B-EUR": rising(start=50.0)})
    cfg = make_cfg(["A-EUR", "B-EUR"], blocklist=["A-EUR", "B-EUR"])
    cycle = make_cycle(cfg, feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=90.0, take_profit=9999.0, reason="setup")

    decisions = cycle.run_once()

    assert open_markets() == set()                      # A is gesloten
    assert not any(d.action == "buy" for d in decisions)  # B niet gekocht
    assert any("blocklist" in d.reason for d in decisions)


def test_kill_switch_blocks_buy_but_not_exit():
    feed = FakeFeed({"A-EUR": falling(), "B-EUR": rising(start=50.0)})
    cycle = make_cycle(make_cfg(["A-EUR", "B-EUR"]), feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=90.0, take_profit=9999.0, reason="setup")
    set_paused(True)

    decisions = cycle.run_once()

    assert open_markets() == set()
    assert not any(d.action == "buy" for d in decisions)
    assert any("kill-switch" in d.reason for d in decisions)


# --- time-stop en breakeven-stop ------------------------------------------------

def test_time_stop_closes_stalled_position():
    """Positie staat 20 candles stil rond break-even: slot teruggeven."""
    feed = FakeFeed({"A-EUR": flat()})
    cfg = make_cfg(["A-EUR"], exits={"time_stop_candles": 12,
                                     "time_stop_min_net_pct": 0.0})
    cycle = make_cycle(cfg, feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=50.0, take_profit=500.0, reason="setup")
    backdate_position("A-EUR", hours=4 * 20)

    decisions = cycle.run_once()

    assert open_markets() == set()
    assert any(d.action == "sell" and "time-stop" in d.reason for d in decisions)


def test_breakeven_stop_shadow_logs_but_keeps_position():
    """Shadow-mode: treffer wordt gelogd als SignalRow, positie blijft open."""
    closes = [100.0] * 40 + [110.0] * 20 + [100.2] * 20   # piek ver boven entry
    feed = FakeFeed({"A-EUR": closes}, prices={"A-EUR": 100.2})
    cfg = make_cfg(["A-EUR"], exits={"breakeven_stop": {
        "enabled": True, "binding": False, "trigger_atr": 1.0, "offset_pct": 0.55}})
    cycle = make_cycle(cfg, feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=50.0, take_profit=500.0, reason="setup")
    backdate_position("A-EUR", hours=4 * 30)

    decisions = cycle.run_once()

    assert open_markets() == {"A-EUR"}
    assert not any(d.action == "sell" for d in decisions)
    shadows = shadow_signals()
    assert len(shadows) == 1
    assert "shadow_breakeven" in shadows[0].details


def test_breakeven_stop_binding_closes_position():
    closes = [100.0] * 40 + [110.0] * 20 + [100.2] * 20
    feed = FakeFeed({"A-EUR": closes}, prices={"A-EUR": 100.2})
    cfg = make_cfg(["A-EUR"], exits={"breakeven_stop": {
        "enabled": True, "binding": True, "trigger_atr": 1.0, "offset_pct": 0.55}})
    cycle = make_cycle(cfg, feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=50.0, take_profit=500.0, reason="setup")
    backdate_position("A-EUR", hours=4 * 30)

    decisions = cycle.run_once()

    assert open_markets() == set()
    assert any(d.action == "sell" and "breakeven-stop" in d.reason for d in decisions)
    assert shadow_signals() == []


# --- robuustheid ----------------------------------------------------------------

def test_one_broken_market_does_not_kill_the_cycle():
    """Candles ophalen mislukt voor A; B moet gewoon verhandeld worden."""
    feed = FakeFeed({"B-EUR": rising()})   # A-EUR ontbreekt -> KeyError in de feed
    cycle = make_cycle(make_cfg(["A-EUR", "B-EUR"]), feed)

    decisions = cycle.run_once()

    assert open_markets() == {"B-EUR"}
    assert [d.market for d in decisions] == ["B-EUR"]


# --- 1.1 entries op afgesloten candles ------------------------------------------

def spike_at_the_end(level: float = 100.0, spike: float = 130.0,
                     n: int = N_CANDLES) -> list[float]:
    """Vlakke reeks met alleen in de LOPENDE (laatste, nog niet gesloten) candle
    een uitbraak. Op de volledige reeks levert dat uptrend + verse MACD-flip op
    (score 3); op de afgesloten reeks blijft er score 1 over (alleen de
    Bollinger-conditie op een vlakke reeks)."""
    return [level] * (n - 1) + [spike]


def test_entry_signal_ignores_running_candle():
    """Regressie op punt 1.1: `build_snapshot` las `closes[-1]`/`hist[-1]`, dus de
    lopende bar. De MACD-flip is 2 van de 3 benodigde punten en komt en gaat binnen
    één bar, waardoor de bot elke uurrun de vluchtigste realisatie van het signaal
    pakte. Alleen de lopende candle geeft hier een koopsignaal; die mag niet tellen.
    """
    feed = FakeFeed({"A-EUR": spike_at_the_end()})
    cfg = make_cfg(["A-EUR"], strategy_over={"min_signal_score": 3})
    decisions = make_cycle(cfg, feed).run_once()

    assert open_markets() == set()
    assert not any(d.action == "buy" for d in decisions)


def test_switch_false_restores_signal_on_running_candle():
    """`strategy.signal_on_closed_candles: false` zet het oude gedrag terug, zodat
    oud en nieuw naast elkaar meetbaar blijven. Bewijst tegelijk dat de test-serie
    wel degelijk een koopsignaal bevat: het zit alleen in de lopende candle."""
    feed = FakeFeed({"A-EUR": spike_at_the_end()})
    cfg = make_cfg(["A-EUR"], strategy_over={"min_signal_score": 3,
                                             "signal_on_closed_candles": False})
    decisions = make_cycle(cfg, feed).run_once()

    assert open_markets() == {"A-EUR"}
    assert any(d.action == "buy" for d in decisions)


def test_exit_still_uses_the_running_candle():
    """De exit-route mag NIET meeveranderen: die hoort juist op de actuele prijs te
    kijken. De stop wordt hier alleen door de lopende candle geraakt; op afgesloten
    candles zou de positie blijven staan."""
    feed = FakeFeed({"A-EUR": spike_at_the_end(level=100.0, spike=80.0)})
    cycle = make_cycle(make_cfg(["A-EUR"]), feed)
    cycle.broker.buy("A-EUR", 250.0, stop_loss=90.0, take_profit=9999.0, reason="setup")

    decisions = cycle.run_once()

    assert open_markets() == set()
    assert any(d.action == "sell" and "stop loss" in d.reason for d in decisions)


def test_levels_are_anchored_on_the_live_price():
    """Score en ATR komen uit de afgesloten reeks, maar SL/TP en de fee-gate rekenen
    vanaf de prijs die de bot betaalt. Anders zet een tot 4 uur oude close de
    stop-afstand systematisch verkeerd."""
    closes = rising(n=N_CANDLES)
    feed = FakeFeed({"A-EUR": closes})
    cycle = make_cycle(make_cfg(["A-EUR"]), feed)

    cycle.run_once()

    with session() as s:
        row = s.execute(select(PositionRow).where(PositionRow.market == "A-EUR")
                        ).scalar_one()
    live_price = closes[-1]
    assert row.entry_price == pytest.approx(live_price, rel=1e-9)
    assert row.stop_loss < live_price < row.take_profit
