"""Grid-search parameteroptimalisatie op historische candles (fase 2 tool).

Overfitting-bescherming: de data wordt 70/30 gesplitst. Sinds v0.20.0 draait ELKE
variant op beide perioden en wordt er gerangschikt op de TEST-helft, met de
train-kolom ernaast als overfit-indicator. Daarvoor werd op train gesorteerd en
kreeg alleen de top vijf een testrun, waardoor de slotregel "kies op test, niet op
train" niet uitvoerbaar was: stond de echte testwinnaar zesde op train, dan zag je
hem nooit.

Twee passes, om de looptijd beheersbaar te houden zonder de exit-parameters weg te
laten (81 x 81 = 6561 combinaties zou onwerkbaar zijn):

  pass 1 - kern: EMA-paar, signaalscore, ATR-stop en reward/risk (81 varianten)
  pass 2 - exits: RSI-koopzone, minimale winst, time-stop en breakeven-stop,
           bovenop de winnaar van pass 1 (81 varianten)

Usage:
    python -m tradebot.optimizer BTC-EUR --interval 4h --limit 3000
    python -m tradebot.optimizer BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR \\
        --portfolio --interval 4h --limit 1100
"""
from __future__ import annotations

import argparse
import itertools

from .backtest import run_backtest, run_portfolio_backtest
from .config import get_config
from .decision import FeeModel
from .exchange import BitvavoClient, Candle

# Pass 1: de parameters die bepalen WANNEER er een signaal is en hoe ver stop en
# target liggen.
GRID = {
    "ema": [(9, 21), (12, 26), (20, 50)],
    "min_signal_score": [2, 3, 4],
    "atr_stop_multiplier": [1.5, 2.0, 2.5],
    "reward_risk_ratio": [1.5, 2.0, 3.0],
}

# Pass 2: de parameters die bepalen wanneer een positie WEER DICHT gaat, plus de
# twee entry-drempels die de review miste. `breakeven` is (trigger_atr, offset_pct)
# of None voor "uit"; in de optimizer wordt hij bindend gezet, want anders meet je
# een gate die per definitie niets doet.
EXIT_GRID = {
    "rsi_zone": [(25, 45), (30, 50), (35, 55)],
    "min_profit_pct": [0.25, 0.50, 1.00],
    "time_stop_candles": [0, 12, 24],
    "breakeven": [None, (1.0, 0.55), (1.5, 0.55)],
}

# Onder dit aantal afgewikkelde trades is een rendement geen meting maar ruis;
# zulke varianten zakken naar de onderkant van de tabel in plaats van eruit te
# vallen, zodat zichtbaar blijft dat ze bestonden.
MIN_TRADES_RELIABLE = 5

# Vloer op de drawdown in de risicogecorrigeerde maat: zonder vloer schiet de
# ratio door het dak bij een variant die toevallig nauwelijks drawdown had.
DD_FLOOR_PCT = 1.0


def default_warmup(strategy_cfg: dict) -> int:
    """Aantal candles dat een variant nodig heeft voordat zijn indicatoren warm zijn.

    `indicators.ema` seedt op `arr[0]` en convergeert pas na ruwweg twee tot drie
    keer de periode. De vaste warmup van 60 was daardoor te kort voor ema_slow 50
    (100 tot 150 candles nodig) en benadeelde de traagste variant in een grid die
    ema 9/21, 12/26 en 20/50 naast elkaar zet.

    Bewust geschaalde warmup en GEEN SMA-seeding van de EMA. Seeden lost het
    convergentieprobleem netter op, maar verandert de indicatorwaarden van de
    draaiende bot: het paper-experiment loopt en zijn cijfers moeten vergelijkbaar
    blijven met wat er tot nu toe gemeten is. Een langere aanloop in de backtester
    kost alleen wat data en raakt de productiecode niet aan.

    Meegenomen: 3x de traagste EMA (ook de vaste MACD-slow van 26 plus zijn
    signaallijn van 9), 3x de ATR-periode en het Bollinger-venster van 20.
    """
    ema_slow = int(strategy_cfg.get("ema_slow", 26))
    macd_span = 3 * 26 + 9
    atr_span = 3 * int(strategy_cfg.get("atr_period", 14))
    return max(60, 3 * ema_slow, macd_span, atr_span, 20)


def grid_warmup(configs: list) -> int:
    """Eén warmup voor alle varianten in een run.

    Cruciaal voor vergelijkbaarheid: kreeg elke variant zijn eigen warmup, dan
    handelen ze over verschillende perioden en vergelijk je rendementen die niet
    over dezelfde bars gaan.
    """
    return max(default_warmup(c.strategy) for c in configs) if configs else 60


def variants(cfg):
    """Pass 1: (omschrijving, aangepaste config) per kern-combinatie."""
    for (ef, es), score, atr_m, rr in itertools.product(
            GRID["ema"], GRID["min_signal_score"],
            GRID["atr_stop_multiplier"], GRID["reward_risk_ratio"]):
        c = cfg.model_copy(deep=True)
        c.strategy["ema_fast"], c.strategy["ema_slow"] = ef, es
        c.strategy["min_signal_score"] = score
        c.decision["atr_stop_multiplier"] = atr_m
        c.decision["reward_risk_ratio"] = rr
        yield f"ema{ef}/{es} score>={score} atr*{atr_m} rr{rr}", c


def exit_variants(cfg):
    """Pass 2: exit- en drempelparameters bovenop een gegeven (winnende) config."""
    for (rsi_lo, rsi_hi), min_profit, n_ts, be in itertools.product(
            EXIT_GRID["rsi_zone"], EXIT_GRID["min_profit_pct"],
            EXIT_GRID["time_stop_candles"], EXIT_GRID["breakeven"]):
        c = cfg.model_copy(deep=True)
        c.strategy["rsi_buy_zone_min"], c.strategy["rsi_buy_zone_max"] = rsi_lo, rsi_hi
        c.decision["min_profit_pct"] = min_profit
        exits = dict(c.exits or {})
        exits["time_stop_candles"] = n_ts
        exits["breakeven_stop"] = (
            {"enabled": False} if be is None else
            {"enabled": True, "binding": True, "trigger_atr": be[0], "offset_pct": be[1]})
        c.exits = exits
        be_txt = "uit" if be is None else f"{be[0]}xATR"
        yield (f"rsi{rsi_lo}-{rsi_hi} profit{min_profit} ts{n_ts} be:{be_txt}", c)


def return_over_dd(net_return_pct: float | None, max_dd_pct: float | None) -> float:
    """Risicogecorrigeerd rendement: netto rendement per procentpunt drawdown.

    Waarom deze maat en niet Sharpe of Sortino: die veronderstellen een verdeling
    van periodieke rendementen en zijn bij 5 tot 30 afgewikkelde trades per periode
    vooral een schatting van hun eigen ruis. Rendement gedeeld door maximale
    drawdown maakt geen enkele verdelingsaanname, gebruikt precies de twee
    grootheden die in de go/no-go-criteria van fase 2 al staan (netto P&L na fees
    en max drawdown), en beantwoordt de vraag die er is: hoeveel rendement kreeg ik
    per eenheid pijn onderweg.

    Zwakte, bewust geaccepteerd en daarom afgevlakt met `DD_FLOOR_PCT`: bij een
    kleine drawdown is de ratio instabiel. Lees hem dus samen met de kolom trades.
    """
    if net_return_pct is None:
        return 0.0
    return round(net_return_pct / max(max_dd_pct or 0.0, DD_FLOOR_PCT), 2)


def _run_period(data: dict[str, list[Candle]], cfg, fee_model: FeeModel,
                warmup: int, portfolio: bool) -> dict:
    if portfolio or len(data) > 1:
        return run_portfolio_backtest(data, cfg, fee_model, warmup=warmup)
    return run_backtest(next(iter(data.values())), cfg, fee_model, warmup=warmup)


def evaluate(variant_list: list, train: dict, test: dict, fee_model: FeeModel,
             warmup: int, portfolio: bool) -> list[dict]:
    """Draai ELKE variant op beide perioden en rangschik op de testhelft.

    Rangschikking: eerst betrouwbaarheid (>= MIN_TRADES_RELIABLE afgewikkelde
    trades), dan risicogecorrigeerd testrendement. De trainkolom blijft in de
    uitvoer staan als overfit-indicator, niet als selectiecriterium.
    """
    rows = []
    for desc, c in variant_list:
        r_train = _run_period(train, c, fee_model, warmup, portfolio)
        r_test = _run_period(test, c, fee_model, warmup, portfolio)
        rows.append({
            "desc": desc,
            "cfg": c,
            "train_pct": r_train["net_return_pct"],
            "test_pct": r_test["net_return_pct"],
            "gap_pct": round((r_train["net_return_pct"] or 0)
                             - (r_test["net_return_pct"] or 0), 2),
            "trades": r_test["closed_trades"],
            "win_pct": r_test["win_rate_pct"] or 0.0,
            "dd_pct": r_test["max_drawdown_pct"],
            "test_rar": return_over_dd(r_test["net_return_pct"], r_test["max_drawdown_pct"]),
            "mode": r_test["mode"],
        })
    rows.sort(key=lambda r: (r["trades"] >= MIN_TRADES_RELIABLE, r["test_rar"]),
              reverse=True)
    return rows


def print_table(title: str, rows: list[dict], top: int) -> None:
    print(f"\n{title}")
    print(f"{'variant':44s} {'test%':>8s} {'r/dd':>6s} {'train%':>8s} {'gap':>7s} "
          f"{'trades':>7s} {'win%':>6s} {'dd%':>6s}")
    for r in rows[:top]:
        vlag = "" if r["trades"] >= MIN_TRADES_RELIABLE else "  <- te weinig trades"
        print(f"{r['desc']:44s} {(r['test_pct'] or 0):>8.2f} {r['test_rar']:>6.2f} "
              f"{(r['train_pct'] or 0):>8.2f} {r['gap_pct']:>7.2f} {r['trades']:>7d} "
              f"{r['win_pct']:>6.1f} {r['dd_pct']:>6.1f}{vlag}")


def split_data(data: dict[str, list[Candle]], ratio: float = 0.7):
    train, test = {}, {}
    for market, candles in data.items():
        cut = int(len(candles) * ratio)
        train[market], test[market] = candles[:cut], candles[cut:]
    return train, test


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("markets", nargs="+")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--portfolio", action="store_true",
                        help="portfolio-modus: gedeelde cash, buckets, slots, gates")
    parser.add_argument("--skip-exit-pass", action="store_true",
                        help="alleen pass 1 draaien (sneller, exits op config-waarden)")
    args = parser.parse_args()

    cfg = get_config()
    feed = BitvavoClient()
    fetch = feed.get_candles_history if args.limit > 1440 else feed.get_candles
    data = {m: fetch(m, args.interval, args.limit) for m in args.markets}
    train, test = split_data(data)
    fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                         cfg.fees["slippage_buffer_pct"])

    core = list(variants(cfg))
    warmup = grid_warmup([c for _, c in core])
    n_bars = min(len(v) for v in data.values())
    print(f"\nOptimizer {', '.join(args.markets)} ({args.interval}): {n_bars} candles per "
          f"markt, train {int(n_bars * 0.7)} / test {n_bars - int(n_bars * 0.7)}")
    print(f"warmup {warmup} candles (geschaald met de traagste EMA in de grid, "
          f"gelijk voor alle varianten), {len(core)} varianten x 2 perioden")

    rows = evaluate(core, train, test, fee_model, warmup, args.portfolio)
    print_table("PASS 1 - kernparameters (gesorteerd op risicogecorrigeerd TEST-rendement)",
                rows, args.top)

    if not args.skip_exit_pass and rows:
        winner = rows[0]["cfg"]
        exits = list(exit_variants(winner))
        warmup2 = grid_warmup([c for _, c in exits])
        print(f"\nPASS 2 op de winnaar van pass 1: {rows[0]['desc']} "
              f"({len(exits)} varianten x 2 perioden)")
        rows2 = evaluate(exits, train, test, fee_model, max(warmup, warmup2),
                         args.portfolio)
        print_table("PASS 2 - exit- en drempelparameters", rows2, args.top)

    print("\nLet op: kies op de test-kolom, niet op train. Een grote gap = overfit.")
    print("r/dd = netto testrendement per procentpunt max drawdown; lees hem samen "
          "met het aantal trades.")
    print(f"Modus: {rows[0]['mode'] if rows else 'n.v.t.'} — alleen portfolio-modus is "
          "met de live-run te vergelijken.")


if __name__ == "__main__":
    main()
