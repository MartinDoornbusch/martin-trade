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

from .backtest import buy_hold_pct, run_backtest, run_portfolio_backtest
from .config import get_config
from .decision import FeeModel
from .exchange import BitvavoClient, Candle, candle_window, parse_end_ms

# Pass 1: de parameters die bepalen WANNEER er een signaal is en hoe ver stop en
# target liggen.
GRID = {
    "ema": [(9, 21), (12, 26), (20, 50)],
    "min_signal_score": [2, 3, 4],
    "atr_stop_multiplier": [1.5, 2.0, 2.5],
    "reward_risk_ratio": [1.5, 2.0, 3.0],
    # Het regime-filter hoort in pass 1 en niet in de exit-pass: het is een
    # ENTRY-gate en bepaalt dus welke trades er überhaupt zijn. Belangrijker: het is
    # het enige mechanisme dat al gebouwd is om een verliesregime te vermijden, dus
    # de vraag "is er een configuratie die het slechte venster overleeft" is zonder
    # deze as niet te beantwoorden.
    "regime_binding": [False, True],
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

# Drempel voor de KOP van de tabel. Max drawdown is één extreme orderstatistiek:
# bij 15 trades wordt hij bepaald door één ongelukkige reeks, dus een variant kan
# bovenaan komen simpelweg omdat hij zijn slechte reeks nog niet heeft gehad.
# Zonder drempel beloont de risicogecorrigeerde ranking dan systematisch
# laagfrequente varianten, en dat is bij een fee-probleem precies de verkeerde
# kant op: weinig trades ziet er goed uit tot je de tweede helft van de cyclus
# meemaakt. 20 sluit aan bij de drempel die elders in dit project geldt voor "een
# uitspraak doen". Varianten eronder vallen niet weg maar zakken naar de
# onderkant, gemarkeerd, zodat zichtbaar blijft dat ze bestonden.
MIN_TRADES_RELIABLE = 20

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
    for (ef, es), score, atr_m, rr, regime in itertools.product(
            GRID["ema"], GRID["min_signal_score"],
            GRID["atr_stop_multiplier"], GRID["reward_risk_ratio"],
            GRID["regime_binding"]):
        c = cfg.model_copy(deep=True)
        c.strategy["ema_fast"], c.strategy["ema_slow"] = ef, es
        c.strategy["min_signal_score"] = score
        c.decision["atr_stop_multiplier"] = atr_m
        c.decision["reward_risk_ratio"] = rr
        c.regime = {**(c.regime or {}), "enabled": True, "binding": regime}
        yield (f"ema{ef}/{es} score>={score} atr*{atr_m} rr{rr} "
               f"regime:{'aan' if regime else 'uit'}"), c


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
                warmup: int, portfolio: bool,
                proxy_candles: list[Candle] | None = None) -> dict:
    if portfolio or len(data) > 1:
        return run_portfolio_backtest(data, cfg, fee_model, warmup=warmup,
                                      proxy_candles=proxy_candles)
    return run_backtest(next(iter(data.values())), cfg, fee_model, warmup=warmup,
                        proxy_candles=proxy_candles)


def evaluate(variant_list: list, train: dict, test: dict, fee_model: FeeModel,
             warmup: int, portfolio: bool,
             min_trades: int = MIN_TRADES_RELIABLE,
             proxy_train: list[Candle] | None = None,
             proxy_test: list[Candle] | None = None) -> list[dict]:
    """Draai ELKE variant op beide perioden en rangschik op de testhelft.

    Rangschikking: eerst betrouwbaarheid (>= `min_trades` afgewikkelde trades),
    dan risicogecorrigeerd testrendement. De trainkolom blijft in de uitvoer staan
    als overfit-indicator, niet als selectiecriterium.
    """
    rows = []
    for desc, c in variant_list:
        r_train = _run_period(train, c, fee_model, warmup, portfolio, proxy_train)
        r_test = _run_period(test, c, fee_model, warmup, portfolio, proxy_test)
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
            # De 70/30-split is CHRONOLOGISCH: train is de oudere periode, test de
            # recente. Een variant die alleen op test wint, wint dus alleen in het
            # meest recente marktregime. `min_pct` is de uitkomst in het SLECHTSTE
            # van de twee vensters en beantwoordt daarmee de vraag die telt: is er
            # een configuratie die ook het ongunstige venster overleeft.
            "min_pct": round(min(r_train["net_return_pct"] or 0,
                                 r_test["net_return_pct"] or 0), 2),
            "bh_train": r_train.get("buy_hold_pct"),
            "bh_test": r_test.get("buy_hold_pct"),
            # Relatief t.o.v. kopen-en-vasthouden. Beslissend gebleken: bij een
            # long-only strategie in een DALENDE markt is "negatief rendement" de
            # normale uitkomst, en zegt alleen het verschil met vasthouden iets over
            # de strategie zelf.
            "rel_train": round((r_train["net_return_pct"] or 0)
                               - (r_train.get("buy_hold_pct") or 0), 2),
            "rel_test": round((r_test["net_return_pct"] or 0)
                              - (r_test.get("buy_hold_pct") or 0), 2),
            "fee_gate_block_pct": r_test.get("fee_gate_block_pct"),
            "expo_train": r_train.get("exposure_pct", 0.0),
            "expo_test": r_test.get("exposure_pct", 0.0),
            # Alpha = rendement min wat je met dezelfde blootstelling passief had
            # gehaald. Scheidt vaardigheid van afwezigheid: een variant die de helft
            # van de tijd in een markt zit die 20% daalt, "verdient" 10 punt door
            # niets te doen. Eerste-orde-benadering (ze veronderstelt dat de
            # blootstelling niet systematisch samenvalt met de beweging), maar precies
            # dat samenvallen is wat een regime-filter claimt en dus wat je wilt meten.
            "alpha_train": round((r_train["net_return_pct"] or 0)
                                 - r_train.get("exposure_pct", 0.0) / 100
                                 * (r_train.get("buy_hold_pct") or 0), 2),
            "alpha_test": round((r_test["net_return_pct"] or 0)
                                - r_test.get("exposure_pct", 0.0) / 100
                                * (r_test.get("buy_hold_pct") or 0), 2),
            "mode": r_test["mode"],
            "regime": r_test.get("regime", "uit"),
        })
    for r in rows:
        r["reliable"] = r["trades"] >= min_trades
    rows.sort(key=lambda r: (r["reliable"], r["test_rar"]), reverse=True)
    return rows


def print_table(title: str, rows: list[dict], top: int,
                min_trades: int = MIN_TRADES_RELIABLE) -> None:
    """Trades staat bewust direct naast `r/dd`: die ratio is alleen te lezen als je
    ziet op hoeveel trades de drawdown in de noemer geschat is."""
    print(f"\n{title}")
    print(f"{'variant':44s} {'test%':>8s} {'r/dd':>6s} {'trades':>7s} {'dd%':>6s} "
          f"{'train%':>8s} {'gap':>7s} {'win%':>6s}")
    for r in rows[:top]:
        vlag = "" if r["reliable"] else f"  <- n<{min_trades}, dd onbetrouwbaar"
        print(f"{r['desc']:44s} {(r['test_pct'] or 0):>8.2f} {r['test_rar']:>6.2f} "
              f"{r['trades']:>7d} {r['dd_pct']:>6.1f} {(r['train_pct'] or 0):>8.2f} "
              f"{r['gap_pct']:>7.2f} {r['win_pct']:>6.1f}{vlag}")
    beste_op_rendement = max(rows, key=lambda r: (r["reliable"], r["test_pct"] or -999))
    if beste_op_rendement["desc"] != rows[0]["desc"]:
        print(f"  (op kaal testrendement zou '{beste_op_rendement['desc']}' winnen met "
              f"{beste_op_rendement['test_pct']:.2f}% bij dd {beste_op_rendement['dd_pct']:.1f}%; "
              f"de risicocorrectie kiest anders)")


def print_overleving(rows: list[dict], top: int = 5) -> None:
    """Tweede tabel: gesorteerd op het SLECHTSTE van de twee vensters.

    De tabel hierboven rangschikt op test, en de split is chronologisch: test is de
    recente periode. Een variant die daar wint kan dat volledig aan het marktregime
    danken. Deze tabel beantwoordt de andere vraag: is er iets dat ook het
    ongunstige venster overleeft. Staat hier alles diep negatief, dan is het antwoord
    op de fase 2-vraag "geen edge", en verhuist het werk naar de instaplogica in
    plaats van naar de gates.
    """
    beste = sorted(rows, key=lambda r: (r["reliable"], r["min_pct"]), reverse=True)
    print("\nOVERLEVING - gesorteerd op het slechtste van beide vensters")
    print(f"{'variant':44s} {'min%':>8s} {'vs bh tr':>9s} {'vs bh te':>9s} "
          f"{'expo tr':>8s} {'expo te':>8s} {'alfa tr':>8s} {'alfa te':>8s} {'trades':>7s}")
    for r in beste[:top]:
        print(f"{r['desc']:44s} {r['min_pct']:>8.2f} {r['rel_train']:>+9.2f} "
              f"{r['rel_test']:>+9.2f} {r['expo_train']:>7.1f}% {r['expo_test']:>7.1f}% "
              f"{r['alpha_train']:>+8.2f} {r['alpha_test']:>+8.2f} {r['trades']:>7d}")
    if beste and beste[0]["min_pct"] < 0:
        print(f"\nGEEN ENKELE variant is positief in beide vensters; de beste haalt "
              f"{beste[0]['min_pct']:.2f}% in zijn slechtste periode.")
    geblokkeerd = [r["fee_gate_block_pct"] for r in rows
                   if r.get("fee_gate_block_pct") is not None]
    if geblokkeerd:
        gem = sum(geblokkeerd) / len(geblokkeerd)
        print(f"Fee-gate hield gemiddeld {gem:.1f}% van de signalen tegen"
              + ("  <- de gate bindt nooit; hij toetst of het koersdoel ver genoeg weg "
                 "ligt, niet of de trade positieve verwachtingswaarde heeft"
                 if gem < 1.0 else ""))
    if rows and rows[0].get("bh_train") is not None:
        print(f"IJkpunt kopen-en-vasthouden over dezelfde markten en vensters: "
              f"train {rows[0]['bh_train']:+.2f}%, test {rows[0]['bh_test']:+.2f}%.")
        beste_rel = max(rows, key=lambda r: min(r["rel_train"], r["rel_test"]))
        print(f"Beste RELATIEF t.o.v. vasthouden (slechtste venster): {beste_rel['desc']} "
              f"met {min(beste_rel['rel_train'], beste_rel['rel_test']):+.2f} punt.")
        beste_alfa = max(rows, key=lambda r: min(r["alpha_train"], r["alpha_test"]))
        alfa = min(beste_alfa["alpha_train"], beste_alfa["alpha_test"])
        print(f"Beste op ALFA (rendement min exposure x marktrendement, slechtste venster): "
              f"{beste_alfa['desc']} met {alfa:+.2f} punt.")
        niets_tr, niets_te = -(rows[0]["bh_train"] or 0), -(rows[0]["bh_test"] or 0)
        print(f"Referentie 'niets doen' (0% blootstelling): {niets_tr:+.2f} punt op train, "
              f"{niets_te:+.2f} op test.")
        print("Lees die twee samen. In een DALEND venster verslaat elke long-only variant "
              "die minder in de markt zit het vasthouden bijna per definitie; in een "
              "stijgend venster kost diezelfde afwezigheid geld. Alfa haalt de "
              "blootstelling eruit: blijft daar niets over, dan meet je afwezigheid en "
              "geen vaardigheid.")


def kwartalen(data: dict[str, list[Candle]], warmup: int, n: int = 4) -> list[tuple]:
    """Kopen-en-vasthouden per gelijk deel van de reeks.

    Bestaat om één vraag meteen zichtbaar te maken: zit er wel een STIJGEND venster
    in de steekproef? Zonder dat is "voegt het regime-filter waarde toe" niet te
    beantwoorden, alleen "beperkt het de schade". Een trendfilter kost geld in een
    stijgende markt, en dat deel van de rekening zie je niet als je alleen dalende
    vensters meet.
    """
    lengte = min(len(v) for v in data.values())
    stap = lengte // n
    uit = []
    for i in range(n):
        segment = {m: v[i * stap:(i + 1) * stap] for m, v in data.items()}
        uit.append((f"Q{i + 1}", buy_hold_pct(segment, min(warmup, stap // 4))))
    return uit


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
    parser.add_argument("--end", default=None,
                        help="einde van het venster (ISO-8601 of epoch ms), "
                             "zodat twee runs exact dezelfde candles zien")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES_RELIABLE,
                        help="drempel voor de kop van de tabel; daaronder is max "
                             "drawdown één ongelukkige reeks in plaats van een meting")
    parser.add_argument("--portfolio", action="store_true",
                        help="portfolio-modus: gedeelde cash, buckets, slots, gates")
    parser.add_argument("--skip-exit-pass", action="store_true",
                        help="alleen pass 1 draaien (sneller, exits op config-waarden)")
    args = parser.parse_args()

    cfg = get_config()
    feed = BitvavoClient()
    end_ms = parse_end_ms(args.end)
    fetch = feed.get_candles_history if (args.limit > 1440 or end_ms) else feed.get_candles
    data = {m: fetch(m, args.interval, args.limit, end_ms) for m in args.markets}
    train, test = split_data(data)
    fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                         cfg.fees["slippage_buffer_pct"])

    core = list(variants(cfg))
    warmup = grid_warmup([c for _, c in core])
    warmup_kop = warmup
    n_bars = min(len(v) for v in data.values())
    print(f"\nOptimizer {', '.join(args.markets)} ({args.interval}): {n_bars} candles per "
          f"markt, train {int(n_bars * 0.7)} / test {n_bars - int(n_bars * 0.7)}")
    print(f"venster: {candle_window(next(iter(data.values())))}")
    print(f"ijkpunt kopen-en-vasthouden: train {buy_hold_pct(train, warmup_kop):+.2f}%, "
          f"test {buy_hold_pct(test, warmup_kop):+.2f}%")
    print("markt per kwartaal (kopen-en-vasthouden): "
          + ", ".join(f"{label} {waarde:+.1f}%"
                      for label, waarde in kwartalen(data, warmup_kop)))
    print(f"warmup {warmup} candles (geschaald met de traagste EMA in de grid, "
          f"gelijk voor alle varianten), {len(core)} varianten x 2 perioden")

    proxy_market = str((cfg.regime or {}).get("proxy_market", "BTC-EUR"))
    proxy_train, proxy_test = train.get(proxy_market), test.get(proxy_market)
    if proxy_train is None:
        print(f"LET OP: proxy-markt {proxy_market} zit niet in de dataset, dus de "
              f"regime-varianten kunnen niet draaien. Voeg hem toe aan de markten.")
    rows = evaluate(core, train, test, fee_model, warmup, args.portfolio, args.min_trades,
                    proxy_train, proxy_test)
    print_table("PASS 1 - kernparameters (gesorteerd op risicogecorrigeerd TEST-rendement)",
                rows, args.top, args.min_trades)
    print_overleving(rows)

    if not args.skip_exit_pass and rows:
        # Pass 2 draait op TWEE zaadjes, niet op één. De test-winnaar is gekozen op de
        # recente periode; erft pass 2 alleen die config, dan kan hij de tak die het
        # ONGUNSTIGE venster het beste overleeft nooit verkennen. Dat is precies wat
        # er gebeurde bij de eerste run: de test-winnaar had regime uit, dus alle 81
        # exit-varianten hadden regime uit en de overlevingstabel van pass 2 was
        # slechter dan die van pass 1.
        zaadjes = [("test-winnaar", rows[0])]
        overlever = max(rows, key=lambda r: (r["reliable"], r["min_pct"]))
        if overlever["desc"] != rows[0]["desc"]:
            zaadjes.append(("beste overlever", overlever))
        for label, zaad in zaadjes:
            exits = list(exit_variants(zaad["cfg"]))
            warmup2 = grid_warmup([c for _, c in exits])
            print(f"\nPASS 2 op de {label} van pass 1: {zaad['desc']} "
                  f"({len(exits)} varianten x 2 perioden)")
            rows2 = evaluate(exits, train, test, fee_model, max(warmup, warmup2),
                             args.portfolio, args.min_trades, proxy_train, proxy_test)
            print_table(f"PASS 2 ({label}) - exit- en drempelparameters",
                        rows2, args.top, args.min_trades)
            print_overleving(rows2)

    print("\nLet op: kies op de test-kolom, niet op train. Een grote gap = overfit.")
    print(f"r/dd = netto testrendement per procentpunt max drawdown. Max drawdown is "
          f"één extreme orderstatistiek: onder {args.min_trades} trades wordt hij bepaald "
          f"door één ongelukkige reeks, dus zulke varianten staan onderaan en zijn "
          f"gemarkeerd.")
    print(f"Modus: {rows[0]['mode'] if rows else 'n.v.t.'} — alleen portfolio-modus is "
          "met de live-run te vergelijken.")


if __name__ == "__main__":
    main()
