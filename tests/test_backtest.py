"""Tests op de backtester, die sinds v0.18.0 een strategie modelleerde die niet
meer bestond. Deterministisch: synthetische candles, geen netwerk, geen DB.

Test-naar-punt (code review ronde 2, blok 2):

| Punt | Test |
|------|------|
| 2.1 time-stop ontbrak in de backtest | `test_time_stop_closes_a_stalled_position` |
| 2.1 breakeven-stop ontbrak in de backtest | `test_breakeven_stop_fires_when_binding` |
| 2.1 breakeven mag in shadow niet verkopen | `test_breakeven_stop_stays_shadow_when_not_binding` |
| 2.2 exit op slotkoers i.p.v. intrabar | `test_exit_uses_the_intrabar_low_not_the_close` |
| 2.2 stop wint bij een candle die beide raakt | `test_intrabar_stop_wins_when_one_candle_hits_both` |
| 2.3 slippage werd nooit op de fill toegepast | `test_slippage_costs_one_full_buffer_per_round_trip` |
| 2.3 op het SL-pad verschuift de kost, hij verdwijnt niet | `test_slippage_on_the_stop_path_costs_only_one_leg` |
| 2.4 all-in sizing i.p.v. buckets | `test_portfolio_mode_spends_one_bucket_per_position` |
| 2.4 slotlimiet werd niet gerespecteerd | `test_portfolio_mode_respects_max_open_positions` |
| 2.4 correlatiecap ontbrak | `test_portfolio_mode_applies_the_correlation_cluster_cap` |
| 2.4 modus moet in de output staan | `test_output_labels_which_mode_ran` |
| optimalisatie mag niet van de engine afdrijven | `test_build_snapshots_matches_build_snapshot` |
| ... ook op een slice, want EMA seedt op arr[0] | `test_build_snapshots_matches_build_snapshot_on_a_slice` |
"""
from types import SimpleNamespace

import pytest

from tradebot.backtest import (
    leg_slippage_pct,
    run_backtest,
    run_portfolio_backtest,
)
from tradebot.decision import FeeModel
from tradebot.exchange import Candle
from tradebot.strategy import build_snapshot, build_snapshots, intrabar_exit

STEP_MS = 4 * 3600 * 1000
START_MS = 1_700_000_000_000
WARMUP = 60


def candles(closes: list[float], highs: list[float] | None = None,
            lows: list[float] | None = None) -> list[Candle]:
    """Candles met standaard +/-2% wicks, zodat ATR > 0 en de fee-gate ruim haalt."""
    highs = highs or [c * 1.02 for c in closes]
    lows = lows or [c * 0.98 for c in closes]
    return [Candle(ts=START_MS + i * STEP_MS, open=c, high=h, low=lo, close=c,
                   volume=1000.0)
            for i, (c, h, lo) in enumerate(zip(closes, highs, lows, strict=True))]


def make_cfg(**over) -> SimpleNamespace:
    risk = {"paper_start_eur": 1000.0, "sizing": "bucket", "bucket_eur": 250.0,
            "max_position_pct": 25.0, "max_open_positions": 10,
            "cooldown_hours_after_trade": 0, "daily_loss_cap_pct": 100.0,
            "max_correlation": 0.85, "correlation_lookback": 60,
            "max_correlated_positions": 99}
    risk.update(over.pop("risk", {}))
    return SimpleNamespace(
        strategy={"ema_fast": 3, "ema_slow": 5, "rsi_period": 14, "atr_period": 14,
                  "rsi_buy_zone_min": 25, "rsi_buy_zone_max": 45,
                  "rsi_overbought": 70, "min_signal_score": 1},
        decision={"min_profit_pct": 0.50, "atr_stop_multiplier": 2.0,
                  "reward_risk_ratio": 1.5},
        fees={"maker_pct": 0.15, "taker_pct": 0.25, "slippage_buffer_pct": 0.10},
        risk=risk,
        exits=over.pop("exits", {}),
    )


def fees(slippage: float = 0.10) -> FeeModel:
    return FeeModel(0.15, 0.25, slippage)


# --- optimalisatie mag niet van de engine afdrijven -----------------------------

def test_build_snapshots_matches_build_snapshot():
    """`build_snapshots` rekent alle indicatoren in één pass door, wat alleen mag
    omdat elke indicator prefix-stabiel is. Deze test pint dat vast: zonder hem kan
    de backtester stilletjes andere waarden zien dan de engine."""
    cfg = make_cfg()
    data = candles([100 + (i % 7) - (i % 3) * 1.5 for i in range(90)])
    serie = build_snapshots("BT", data, cfg.strategy)
    for i in (20, 45, 61, 89):
        one = build_snapshot("BT", data[: i + 1], cfg.strategy)
        assert serie[i] == one, f"afwijking op index {i}"


def test_build_snapshots_matches_build_snapshot_on_a_slice():
    """Ook op een slice, want dat is wat de optimizer doet: hij draait op
    `candles[split:]`. EMA seedt op `arr[0]`, dus een slice heeft een ander
    startpunt en dat is precies waar een one-pass-implementatie stil van de
    referentie kan afwijken."""
    cfg = make_cfg()
    data = candles([100 + (i % 7) - (i % 3) * 1.5 for i in range(120)])[37:]
    serie = build_snapshots("BT", data, cfg.strategy)
    # Vanaf index 20 zijn alle indicatoren warm (Bollinger 20 is de traagste);
    # daaronder staat aan beide kanten NaN en vergelijkt een dataclass nooit gelijk.
    for i in (25, 50, 82):
        snap = serie[i]
        assert snap.bb_lower == snap.bb_lower, f"indicator nog niet warm op {i}"
        assert snap == build_snapshot("BT", data[: i + 1], cfg.strategy), \
            f"afwijking op slice-index {i}"


# --- 2.2 intrabar-exits ---------------------------------------------------------

def test_intrabar_stop_wins_when_one_candle_hits_both():
    bar = Candle(ts=0, open=100, high=115, low=90, close=100, volume=1)
    assert intrabar_exit(bar, stop=92, target=112) == "stop"
    assert intrabar_exit(bar, stop=92, target=112, stop_first=False) == "target"
    assert intrabar_exit(bar, stop=80, target=120) is None


def test_exit_uses_the_intrabar_low_not_the_close():
    """Regressie op punt 2.2: `check_exit` vergeleek de CLOSE met de stop. Deze
    candle dook met zijn low door de stop maar sloot er ruim boven; de position
    guard stopt live binnen de minuut uit, dus de backtest hoort dat ook te doen.
    Op de oude slotkoers-logica bleef de positie open en telde de trade niet mee,
    wat de win-rate kunstmatig optilde."""
    closes = [100.0] * (WARMUP + 1) + [99.0]
    lows = [c * 0.98 for c in closes]
    lows[-1] = 91.0                       # prikt door de stop (~92,05)
    data = candles(closes, lows=lows)
    r = run_backtest(data, make_cfg(), fees(), warmup=WARMUP)

    assert data[-1].close > 92.0          # slotkoers ligt bóven de stop
    assert r["closed_trades"] == 1
    assert r["exit_reasons"] == {"stop loss": 1}


# --- 2.1 time-stop en breakeven-stop --------------------------------------------

def test_time_stop_closes_a_stalled_position():
    """Regressie op punt 2.1: `run_backtest` riep alleen `check_exit` aan, dus de
    time-stop uit v0.18.0 bestond in de backtest niet."""
    data = candles([100.0] * (WARMUP + 30))
    cfg = make_cfg(exits={"time_stop_candles": 12, "time_stop_min_net_pct": 0.0})
    r = run_backtest(data, cfg, fees(), warmup=WARMUP)

    assert r["closed_trades"] >= 1
    assert "time-stop" in r["exit_reasons"]


def test_breakeven_stop_fires_when_binding():
    """Regressie op punt 2.1: de breakeven-stop uit v0.19.0 ontbrak eveneens.
    Piek loopt ruim boven 1x ATR, koers zakt terug tot binnen de offset."""
    closes = [100.0] * (WARMUP + 1) + [106.0, 106.0, 100.4, 100.4]
    cfg = make_cfg(exits={"breakeven_stop": {"enabled": True, "binding": True,
                                             "trigger_atr": 1.0, "offset_pct": 0.55}})
    r = run_backtest(candles(closes), cfg, fees(), warmup=WARMUP)

    assert r["closed_trades"] == 1
    assert "breakeven-stop" in r["exit_reasons"]


def test_breakeven_stop_stays_shadow_when_not_binding():
    """Met `binding: false` verkoopt de engine niet, dus de backtester ook niet;
    anders modelleert hij een strengere strategie dan er draait."""
    closes = [100.0] * (WARMUP + 1) + [106.0, 106.0, 100.4, 100.4]
    cfg = make_cfg(exits={"breakeven_stop": {"enabled": True, "binding": False,
                                             "trigger_atr": 1.0, "offset_pct": 0.55}})
    r = run_backtest(candles(closes), cfg, fees(), warmup=WARMUP)

    assert r["closed_trades"] == 0
    assert r["open_at_end"] == 1


# --- 2.3 slippage ---------------------------------------------------------------

def test_slippage_costs_one_full_buffer_per_round_trip():
    """Regressie op punt 2.3: `slippage_buffer_pct` zat wel in `min_edge` maar werd
    nooit op de fillprijs toegepast. Per been de helft, dus over de round-trip
    precies één buffer: je koopt op de laat en verkoopt op de bied, wat samen één
    spread kost, en dat is ook hoe `min_edge_pct` en de scanner hem tellen.

    Gemeten op een TIME-STOP-exit, want die vult op de slotkoers en staat dus los
    van de entry. Bij een stop- of target-exit is de drag maar één been: die
    niveaus liggen op `entry +/- k x ATR`, dus ze schuiven mee met een duurdere
    entry en compenseren het instapbeen vanzelf.
    """
    data = candles([100.0] * 73)          # koop op bar 60, time-stop op bar 72
    cfg = make_cfg(exits={"time_stop_candles": 12, "time_stop_min_net_pct": 0.0})

    zonder = run_backtest(data, cfg, fees(slippage=0.0), warmup=WARMUP)
    met = run_backtest(data, cfg, fees(slippage=0.20), warmup=WARMUP)

    assert leg_slippage_pct(fees(slippage=0.20)) == pytest.approx(0.10)
    assert zonder["closed_trades"] == met["closed_trades"] == 1
    assert zonder["open_at_end"] == met["open_at_end"] == 0
    verschil = zonder["net_return_pct"] - met["net_return_pct"]
    assert 0.15 < verschil < 0.25, f"verwacht ~0,20%-punt drag, gemeten {verschil}"


def test_slippage_on_the_stop_path_costs_only_one_leg():
    """Tegenhanger van de time-stop-test. Stop en target liggen op `fill +/- k x ATR`
    en schuiven dus mee met een duurdere entry, waardoor het instapbeen zichzelf
    grotendeels compenseert in het RENDEMENT. Dat is geen gratis lunch: die stop
    ligt daardoor verder van de prijs waarop het besluit is genomen dan de bedoelde
    2x ATR, dus de kost verschuift naar de risicokolom. Deze test pint de kleine
    drag vast, zodat een latere wijziging die de stop losmaakt van de fill niet stil
    van gedrag verandert."""
    closes = [100.0] * (WARMUP + 1) + [99.0]
    lows = [c * 0.98 for c in closes]
    lows[-1] = 91.0
    data = candles(closes, lows=lows)
    cfg = make_cfg()

    zonder = run_backtest(data, cfg, fees(slippage=0.0), warmup=WARMUP)
    met = run_backtest(data, cfg, fees(slippage=0.20), warmup=WARMUP)

    assert zonder["exit_reasons"] == met["exit_reasons"] == {"stop loss": 1}
    verschil = zonder["net_return_pct"] - met["net_return_pct"]
    assert 0.05 < verschil < 0.15, (
        f"verwacht ~één been (0,10%-punt) op het SL-pad, gemeten {verschil}")


# --- 2.4 portfolio-modus --------------------------------------------------------

def four_flat_markets(n: int = WARMUP + 3) -> dict[str, list[Candle]]:
    """Vier markten die alle vier een koopsignaal geven, met licht verschillende
    niveaus zodat ze niet toevallig identiek zijn."""
    return {f"M{i}-EUR": candles([100.0 + i * 10] * n) for i in range(4)}


def test_portfolio_mode_spends_one_bucket_per_position():
    """Regressie op punt 2.4: `spend = cash` zette all-in in, terwijl de bot buckets
    van EUR250 gebruikt. Twee posities x EUR250 x 0,25% taker = EUR1,25 aan
    instapfees; all-in op EUR1000 zou EUR2,50 zijn."""
    cfg = make_cfg(risk={"max_open_positions": 2})
    r = run_portfolio_backtest(four_flat_markets(), cfg, fees(), warmup=WARMUP)

    assert r["open_at_end"] == 2
    assert r["total_fees_eur"] == pytest.approx(1.25, abs=0.01)


def test_portfolio_mode_respects_max_open_positions():
    """Vier koopsignalen, drie slots: er mogen er drie open."""
    cfg = make_cfg(risk={"max_open_positions": 3})
    r = run_portfolio_backtest(four_flat_markets(), cfg, fees(), warmup=WARMUP)
    assert r["open_at_end"] == 3


def test_portfolio_mode_applies_the_correlation_cluster_cap():
    """Drie identiek bewegende markten en clustercap 2: de derde wordt geweigerd,
    net als in de engine."""
    serie = [100.0 * (1 + 0.01) ** i + (i % 5) for i in range(WARMUP + 3)]
    data = {f"C{i}-EUR": candles(list(serie)) for i in range(3)}
    cfg = make_cfg(risk={"max_open_positions": 10, "max_correlated_positions": 2})
    r = run_portfolio_backtest(data, cfg, fees(), warmup=WARMUP)
    assert r["open_at_end"] == 2


def test_portfolio_mode_respects_the_daily_loss_cap():
    """Dagverliescap van 0% laat na het eerste verlies geen nieuwe entry meer toe."""
    closes = [100.0] * (WARMUP + 1) + [99.0] + [100.0] * 5
    lows = [c * 0.98 for c in closes]
    lows[WARMUP + 1] = 91.0
    data = {"A-EUR": candles(closes, lows=lows)}
    cfg = make_cfg(risk={"daily_loss_cap_pct": 0.0, "max_open_positions": 5})
    r = run_portfolio_backtest(data, cfg, fees(), warmup=WARMUP)

    assert r["closed_trades"] == 1
    assert r["open_at_end"] == 0


def test_output_labels_which_mode_ran():
    """Punt 2.4: de modus moet expliciet in de output staan, anders vergelijk je
    per ongeluk een all-in signaalrun met een live-achtige portefeuillerun."""
    data = candles([100.0] * (WARMUP + 3))
    assert run_backtest(data, make_cfg(), fees(), warmup=WARMUP)["mode"] == "single"
    port = run_portfolio_backtest({"A-EUR": data}, make_cfg(), fees(), warmup=WARMUP)
    assert port["mode"] == "portfolio"
    assert port["markets"] == 1
