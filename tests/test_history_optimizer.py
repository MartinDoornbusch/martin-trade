from types import SimpleNamespace

from tradebot import optimizer
from tradebot.backtest import max_drawdown_pct
from tradebot.config import AppConfig
from tradebot.exchange import BitvavoClient
from tradebot.optimizer import (
    EXIT_GRID,
    GRID,
    default_warmup,
    evaluate,
    exit_variants,
    grid_warmup,
    return_over_dd,
    variants,
)


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
    return AppConfig(markets=["BTC-EUR"], schedule={},
                     fees={"maker_pct": 0.15, "taker_pct": 0.25,
                           "slippage_buffer_pct": 0.10},
                     strategy={"ema_fast": 12, "ema_slow": 26, "min_signal_score": 3,
                               "atr_period": 14, "rsi_buy_zone_min": 25,
                               "rsi_buy_zone_max": 45},
                     decision={"atr_stop_multiplier": 2.0, "reward_risk_ratio": 2.0,
                               "min_profit_pct": 0.5},
                     risk={}, llm={}, exits={"time_stop_candles": 12},
                     regime={"enabled": True, "binding": False,
                             "proxy_market": "BTC-EUR"})


def test_variants_cover_full_grid_and_apply_overrides():
    cfg = make_cfg()
    combos = list(variants(cfg))
    expected = (len(GRID["ema"]) * len(GRID["min_signal_score"])
                * len(GRID["atr_stop_multiplier"]) * len(GRID["reward_risk_ratio"])
                * len(GRID["regime_binding"]))
    assert len(combos) == expected
    desc, c = combos[0]
    assert (c.strategy["ema_fast"], c.strategy["ema_slow"]) == GRID["ema"][0]
    assert cfg.strategy["ema_fast"] == 12  # origineel onaangetast (deep copy)


# --- 3.1: elke variant op beide perioden, rangschikken op test ------------------

def _fake_period(results: dict[int, tuple[float, float]], seen: list):
    """Vervangt de backtest-run: leest het antwoord uit `results` op basis van een
    marker op de config, en noteert welke (marker, periode, warmup) langskwamen."""
    def fake(data, cfg, fee_model, warmup, portfolio, proxy=None):
        periode = next(iter(data))
        seen.append((cfg.marker, periode, warmup))
        train_pct, test_pct = results[cfg.marker]
        pct = train_pct if periode == "TRAIN" else test_pct
        return {"net_return_pct": pct, "closed_trades": 10, "win_rate_pct": 50.0,
                "max_drawdown_pct": 10.0, "mode": "single"}
    return fake


def test_every_variant_runs_on_both_periods_and_ranks_on_test(monkeypatch):
    """Regressie op punt 3.1: `main()` sorteerde alle 81 varianten op TRAIN-rendement
    en draaide alleen voor de top vijf een testrun. Stond de echte testwinnaar zesde
    op train, dan zag je hem nooit — terwijl de slotregel zegt "kies op test".
    Hier is variant 5 de zesde op train en veruit de beste op test."""
    resultaten = {0: (10.0, 1.0), 1: (9.0, 1.0), 2: (8.0, 1.0),
                  3: (7.0, 1.0), 4: (6.0, 1.0), 5: (5.0, 50.0)}
    seen: list = []
    monkeypatch.setattr(optimizer, "_run_period", _fake_period(resultaten, seen))
    varianten = [(f"v{i}", SimpleNamespace(marker=i)) for i in range(6)]

    rows = evaluate(varianten, {"TRAIN": []}, {"TEST": []}, None, 150, False)

    assert {m for m, _, _ in seen} == set(range(6))                  # allemaal gedraaid
    assert {p for _, p, _ in seen} == {"TRAIN", "TEST"}               # op beide perioden
    assert rows[0]["desc"] == "v5"                                    # gekozen op test
    assert rows[0]["train_pct"] == 5.0                                # train blijft zichtbaar
    assert rows[0]["gap_pct"] == -45.0                                # als overfit-indicator


def test_low_trade_variants_sink_to_the_bottom(monkeypatch):
    """Max drawdown is één extreme orderstatistiek: bij weinig trades wordt hij
    bepaald door één ongelukkige reeks, dus een variant kan bovenaan komen omdat hij
    zijn slechte reeks nog niet heeft gehad. Zonder drempel beloont de
    risicogecorrigeerde ranking systematisch laagfrequente varianten, en dat is bij
    een fee-probleem precies de verkeerde kant op. Ze vallen niet weg maar zakken
    naar onderen, gemarkeerd."""
    def fake(data, cfg, fee_model, warmup, portfolio, proxy=None):
        return {"net_return_pct": 99.0 if cfg.marker == 0 else 5.0,
                "closed_trades": 15 if cfg.marker == 0 else 40,
                "win_rate_pct": 100.0, "max_drawdown_pct": 1.0, "mode": "single"}
    monkeypatch.setattr(optimizer, "_run_period", fake)
    rows = evaluate([("weinig", SimpleNamespace(marker=0)),
                     ("genoeg", SimpleNamespace(marker=1))],
                    {"TRAIN": []}, {"TEST": []}, None, 150, False, min_trades=20)

    assert rows[0]["desc"] == "genoeg"
    assert rows[0]["reliable"] is True
    assert rows[1]["desc"] == "weinig"          # blijft zichtbaar
    assert rows[1]["reliable"] is False         # maar expliciet gemarkeerd


def test_min_trades_threshold_defaults_to_twenty():
    """Sluit aan bij de drempel die elders in dit project geldt voor "een uitspraak
    doen" (go/no-go per shadow-gate)."""
    assert optimizer.MIN_TRADES_RELIABLE == 20


# --- 3.2: warmup geschaald met de periodes -------------------------------------

def test_warmup_scales_with_the_slowest_ema():
    """Regressie op punt 3.2: `warmup = 60` was te kort voor ema_slow 50, want
    `indicators.ema` seedt op arr[0] en convergeert pas na 2 tot 3 keer de periode.
    Daardoor benadeelde de grid zijn traagste variant."""
    snel = default_warmup({"ema_slow": 21, "atr_period": 14})
    traag = default_warmup({"ema_slow": 50, "atr_period": 14})
    assert traag >= 150
    assert traag > snel
    assert snel >= 60          # nooit korter dan de oude vaste waarde


def test_grid_uses_one_warmup_for_every_variant(monkeypatch):
    """Alle varianten moeten over dezelfde bars handelen; kreeg elk zijn eigen
    warmup, dan vergelijk je rendementen over verschillende perioden."""
    cfgs = [c for _, c in variants(make_cfg())]
    warmup = grid_warmup(cfgs)
    assert warmup == max(default_warmup(c.strategy) for c in cfgs)
    assert warmup >= 150       # bepaald door ema_slow 50 uit de grid

    seen: list = []
    monkeypatch.setattr(optimizer, "_run_period",
                        _fake_period({i: (1.0, 1.0) for i in range(3)}, seen))
    evaluate([(f"v{i}", SimpleNamespace(marker=i)) for i in range(3)],
             {"TRAIN": []}, {"TEST": []}, None, warmup, False)
    assert {w for _, _, w in seen} == {warmup}


# --- 3.3: exit-parameters in de grid -------------------------------------------

def test_exit_grid_covers_the_missing_parameters():
    """Punt 3.3: de grid dekte ema, score, ATR-stop en R/R, maar niet de
    RSI-zonegrenzen, `min_profit_pct`, `time_stop_candles` of de breakeven-stop."""
    cfg = make_cfg()
    combos = list(exit_variants(cfg))
    verwacht = (len(EXIT_GRID["rsi_zone"]) * len(EXIT_GRID["min_profit_pct"])
                * len(EXIT_GRID["time_stop_candles"]) * len(EXIT_GRID["breakeven"]))
    assert len(combos) == verwacht

    zones = {(c.strategy["rsi_buy_zone_min"], c.strategy["rsi_buy_zone_max"])
             for _, c in combos}
    assert zones == set(EXIT_GRID["rsi_zone"])
    assert {c.decision["min_profit_pct"] for _, c in combos} == set(EXIT_GRID["min_profit_pct"])
    assert {c.exits["time_stop_candles"] for _, c in combos} == set(EXIT_GRID["time_stop_candles"])
    assert cfg.strategy["rsi_buy_zone_min"] == 25          # origineel onaangetast


def test_exit_grid_makes_the_breakeven_stop_binding():
    """In shadow doet de breakeven-stop per definitie niets; om hem te kunnen
    beoordelen moet hij in de optimizer bindend staan."""
    aan = [c for _, c in exit_variants(make_cfg())
           if c.exits["breakeven_stop"].get("enabled")]
    assert aan, "grid moet varianten met breakeven-stop bevatten"
    assert all(c.exits["breakeven_stop"]["binding"] for c in aan)
    uit = [c for _, c in exit_variants(make_cfg())
           if not c.exits["breakeven_stop"].get("enabled")]
    assert uit, "grid moet ook een variant zonder breakeven-stop bevatten"


# --- 3.4: risicogecorrigeerde kolom --------------------------------------------

def test_return_over_dd_prefers_the_calmer_path():
    """Punt 3.4: er werd gerangschikt op rendement terwijl drawdown alleen geprint
    werd. Bij gelijk rendement wint nu het rustigere pad."""
    assert return_over_dd(20.0, 10.0) < return_over_dd(20.0, 5.0)


def test_return_over_dd_is_floored_against_tiny_drawdowns():
    """Zonder vloer schiet de ratio door het dak bij een variant die toevallig
    nauwelijks drawdown had; lees hem daarom samen met het aantal trades."""
    assert return_over_dd(10.0, 0.0) == 10.0
    assert return_over_dd(10.0, 0.2) == 10.0
    assert return_over_dd(None, 5.0) == 0.0


# --- anker-check: venster pinnen en de bedrading van calibrate ------------------

def test_parse_end_ms_accepts_iso_and_epoch():
    """`--limit N` haalt de N NIEUWSTE candles op, dus twee runs op verschillende
    momenten zien andere data. Voor de anker-check is dat fataal: dan vergelijk je
    een codeverschil met een datavenster-verschil."""
    from tradebot.exchange import parse_end_ms

    assert parse_end_ms(None) is None
    assert parse_end_ms("") is None
    assert parse_end_ms("1785945600000") == 1785945600000
    assert parse_end_ms("2026-08-05T16:00:00Z") == 1785945600000
    assert parse_end_ms("2026-08-05T16:00:00") == 1785945600000   # naief = UTC


def test_get_candles_passes_the_end_parameter():
    class Spion(FakeHistoryClient):
        paden: list = []

        def _request(self, method, path, body=None, auth=False):
            Spion.paden.append(path)
            return super()._request(method, path, body, auth)

    Spion.paden = []
    Spion().get_candles("BTC-EUR", "4h", 100, end_ms=1785945600000)
    assert "end=1785945600000" in Spion.paden[0]

    Spion.paden = []
    Spion().get_candles("BTC-EUR", "4h", 100)
    assert "end=" not in Spion.paden[0]


def test_no_calibration_step_can_become_a_no_op():
    """Regressie op een defect dat pas in de echte run zichtbaar werd: de
    slippage-stap gaf delta +0,00 met identieke trades omdat `run_stap` de vlag niet
    doorgaf. Zo'n rij is erger dan een ontbrekende rij, want hij leest als een
    bevinding ("slippage kost niets") terwijl er niets gemeten is.

    Bewust een GEDRAGSassertie en geen bron-inspectie: die zou dit ene geval vangen
    maar breken bij de volgende refactor, en zou niets zeggen over stappen die nog
    gebouwd moeten worden. Hier krijgt elke stap een signatuur van alles wat hij aan
    de backtester meegeeft, en twee opeenvolgende signaturen mogen nooit gelijk zijn.
    """
    import pytest

    from tradebot.calibrate import (
        STAPPEN,
        controleer_geen_no_ops,
        stap_signatuur,
    )

    cfg = make_cfg()
    controleer_geen_no_ops(cfg, STAPPEN)          # de echte stapeling is schoon

    signaturen = [stap_signatuur(cfg, s) for s in STAPPEN]
    assert len(set(signaturen)) == len(STAPPEN), "elke stap moet een eigen invoer hebben"

    # Een gedupliceerde stap moet hard falen, ook als hij er in de tabel prima uitziet.
    from dataclasses import replace as vervang
    kapot = list(STAPPEN[:3]) + [vervang(STAPPEN[2], naam="stiekem een no-op")]
    with pytest.raises(ValueError, match="verandert niets"):
        controleer_geen_no_ops(cfg, kapot)


def test_identical_results_are_flagged_but_not_fatal():
    """Laag 2 van de no-op-bewaking. Een identiek RESULTAAT kan twee dingen
    betekenen: de correctie doet op deze data niets (de trend-break-exit is daar het
    voorbeeld van, en dán is delta 0,00 juist de bevinding), of de stap is niet
    bedraad. Die twee zijn aan het resultaat niet te onderscheiden, dus dit mag geen
    exception zijn — wel een markering, zodat "delta 0,00" nooit meer stilzwijgend
    als bevinding gelezen wordt."""
    from tradebot import calibrate

    vast = {"net_return_pct": 5.0, "closed_trades": 10, "win_rate_pct": 50.0,
            "max_drawdown_pct": 8.0}
    calibrate_run_stap = calibrate.run_stap
    try:
        calibrate.run_stap = lambda data, cfg, stap: dict(vast)
        rijen = calibrate.attributie({}, make_cfg())
    finally:
        calibrate.run_stap = calibrate_run_stap

    assert rijen[0]["identiek"] is False          # eerste rij heeft geen voorganger
    assert all(r["identiek"] for r in rijen[1:])  # de rest is identiek en gemarkeerd
    assert all(r["delta"] == 0.0 for r in rijen[1:])
