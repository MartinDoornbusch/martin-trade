"""Attributie-run: waar komt het verschil met de kalibratie van 18 juli 2026 vandaan?

Die run gebruikte een backtester die op ZEVEN punten anders was dan de huidige. Zet
je die tegelijk aan en de winnaar verschuift, dan weet je alleen dát hij verschoof
en niet waardoor. De vraag "zijn ema20/50, rr 1,5 en score 3 nog steeds de
winnaars" is dan niet te beantwoorden.

Volledige attributie over zeven assen is te duur en ook niet nodig voor de hele
grid. Deze module stapelt de correcties op ÉÉN referentievariant (de
productieconfig) en rapporteert de delta per stap. Zeven extra runs, en het
verschil tussen een cijfer en een verklaring. De volledige grid draai je daarna
één keer met alles aan, via `python -m tradebot.optimizer`.

VALIDEER EERST DE REFERENTIERIJ. Het v0.18.0-model wordt hier gereconstrueerd door
correcties uit te zetten, niet door de oude code te draaien. Die reconstructie is
alleen geldig als rij 1 de uitvoer van 18 juli daadwerkelijk reproduceert op
dezelfde data. Er is geen document met die uitvoer (zie
`docs/kalibratie-v0.20.0.md`), dus de enige echte anker-check is de oude code zelf
draaien uit commit 56d9e55. Wijkt rij 1 af, dan is er onderweg nog iets veranderd
en is elke delta eronder betekenisloos.

De onderste rij is NIET de productiebot. De breakeven-stop staat in de stapeling
bindend, wat voor een backtest onvermijdelijk is (een shadow-gate doet per
definitie niets), terwijl hij in productie op shadow staat. De chase-guard zit
helemaal niet in de backtester. De rij die het huidige gedrag beschrijft is
gemarkeerd met `<- productie`.

De stapeling is cumulatief in een vaste volgorde. Let op bij het lezen: de
correcties zijn niet additief. Slippage op een exit die door de time-stop wordt
geraakt kost iets anders dan op een stop-exit, dus een andere volgorde geeft
andere tussenstappen bij hetzelfde eindresultaat. De volgorde hieronder loopt van
"raakt alleen de fill" naar "raakt de hele portefeuille".

Wat hier bewust NIET in zit: blok 1 (entries op afgesloten candles). De backtester
had dat probleem nooit, want historische candles zijn per definitie afgesloten.
Dat is precies waarom de live bot méér trades maakte dan de backtest voorspelde:
de fout zat in de engine, niet in het model. Blok 1 is dus niet te attribueren met
een backtest en verandert deze cijfers niet.

Usage:
    python -m tradebot.calibrate BTC-EUR ETH-EUR SOL-EUR XRP-EUR LINK-EUR \
        --interval 4h --limit 1100
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from .backtest import DEFAULT_WARMUP, run_backtest, run_portfolio_backtest
from .config import get_config
from .decision import FeeModel
from .exchange import BitvavoClient, Candle, candle_window, parse_end_ms
from .optimizer import default_warmup

# Warmup van de run van 18 juli: de vaste 60 uit de oude backtester.
OUDE_WARMUP = 60


@dataclass
class Stap:
    """Eén correctie, cumulatief bovenop alle voorgaande."""
    naam: str
    punt: str
    trend_break: bool
    time_stop: bool
    breakeven: bool
    intrabar: bool
    slippage: bool
    portfolio: bool
    geschaalde_warmup: bool
    productie: bool = False     # beschrijft deze rij het huidige productiegedrag?


STAPPEN = [
    Stap("v0.18.0-model (zoals 18 juli)", "referentie",
         trend_break=True, time_stop=False, breakeven=False, intrabar=False,
         slippage=False, portfolio=False, geschaalde_warmup=False),
    Stap("- trend-break-exit", "r1/1.2",
         trend_break=False, time_stop=False, breakeven=False, intrabar=False,
         slippage=False, portfolio=False, geschaalde_warmup=False),
    Stap("+ intrabar exits", "2.2",
         trend_break=False, time_stop=False, breakeven=False, intrabar=True,
         slippage=False, portfolio=False, geschaalde_warmup=False),
    Stap("+ slippage op beide benen", "2.3",
         trend_break=False, time_stop=False, breakeven=False, intrabar=True,
         slippage=True, portfolio=False, geschaalde_warmup=False),
    Stap("+ time-stop", "2.1",
         trend_break=False, time_stop=True, breakeven=False, intrabar=True,
         slippage=True, portfolio=False, geschaalde_warmup=False),
    Stap("+ geschaalde warmup", "3.2",
         trend_break=False, time_stop=True, breakeven=False, intrabar=True,
         slippage=True, portfolio=False, geschaalde_warmup=True),
    Stap("+ bucket-sizing en slots", "2.4",
         trend_break=False, time_stop=True, breakeven=False, intrabar=True,
         slippage=True, portfolio=True, geschaalde_warmup=True, productie=True),
    Stap("+ breakeven-stop BINDEND", "2.1",
         trend_break=False, time_stop=True, breakeven=True, intrabar=True,
         slippage=True, portfolio=True, geschaalde_warmup=True),
]


def config_voor(cfg, stap: Stap):
    """Productieconfig met alleen de exits van deze stap aan."""
    c = cfg.model_copy(deep=True)
    exits = dict(c.exits or {})
    exits["time_stop_candles"] = exits.get("time_stop_candles", 12) if stap.time_stop else 0
    be = dict(exits.get("breakeven_stop", {}) or {})
    be["enabled"] = stap.breakeven
    be["binding"] = stap.breakeven      # in shadow doet hij per definitie niets
    exits["breakeven_stop"] = be
    c.exits = exits
    return c


def fee_model_voor(cfg, stap: Stap) -> FeeModel:
    """Altijd het ECHTE fee-model, ook in de referentierij.

    De buffer op nul zetten om "geen slippage" te modelleren was fout: dat verlaagt
    ook `min_edge` van 1,10% naar 1,00%, terwijl de oude code die 1,10% wél
    hanteerde. Of de fill geslipt wordt is een aparte vlag (`slippage_on_fill`).
    """
    return FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                    cfg.fees["slippage_buffer_pct"])


def stap_signatuur(cfg, stap: Stap) -> str:
    """Alles wat deze stap aan de backtester meegeeft, in één vergelijkbare vorm.

    Bestaat om te voorkomen dat een stap stilletjes een no-op wordt. Dat is één keer
    gebeurd: de slippage-stap gaf delta +0,00 met identieke trades omdat `run_stap`
    de vlag niet doorgaf. Zo'n rij is erger dan een ontbrekende rij, want hij leest
    als een bevinding ("slippage kost niets") terwijl er niets gemeten is.

    Een bron-inspectie zou dit ene geval vangen maar breekt bij de volgende
    refactor. Deze signatuur dekt ook stappen die nog gebouwd moeten worden:
    verandert er niets aan de INVOER, dan faalt de run hard.

    Dit is laag 1 van twee. Hij vangt een stap die niets zegt te veranderen, maar
    niet een bedradingsfout waarbij de stap wél iets declareert en `run_stap` het
    niet doorgeeft. Daarvoor is laag 2 nodig, in `attributie`: een identiek
    RESULTAAT als de vorige stap. Dat kan geen harde fout zijn, want een correctie
    die op deze data echt niets doet ziet er precies zo uit; de trend-break-exit is
    daar het voorbeeld van. Vandaar een markering in plaats van een exception.
    """
    c = config_voor(cfg, stap)
    fm = fee_model_voor(cfg, stap)
    return json.dumps({
        "strategy": c.strategy, "decision": c.decision, "exits": c.exits,
        "regime": getattr(c, "regime", {}), "risk": c.risk,
        "fees": [fm.maker_pct, fm.taker_pct, fm.slippage_buffer_pct],
        "intrabar": stap.intrabar, "trend_break": stap.trend_break,
        "slippage_on_fill": stap.slippage, "portfolio": stap.portfolio,
        "warmup": default_warmup(c.strategy) if stap.geschaalde_warmup else OUDE_WARMUP,
    }, sort_keys=True, default=str)


def controleer_geen_no_ops(cfg, stappen: list[Stap]) -> None:
    """Faal hard als een stap dezelfde invoer heeft als zijn voorganger."""
    vorige = None
    for stap in stappen:
        huidig = stap_signatuur(cfg, stap)
        if vorige is not None and huidig == vorige:
            raise ValueError(
                f"stap '{stap.naam}' ({stap.punt}) verandert niets aan de invoer van de "
                f"backtester en zou dus altijd delta 0,00 geven. Een rij die niets meet "
                f"leest als een bevinding; repareer de bedrading of haal de stap weg.")
        vorige = huidig


def run_stap(data: dict[str, list[Candle]], cfg, stap: Stap) -> dict:
    c = config_voor(cfg, stap)
    fm = fee_model_voor(cfg, stap)
    warmup = default_warmup(c.strategy) if stap.geschaalde_warmup else OUDE_WARMUP
    if stap.portfolio:
        return run_portfolio_backtest(data, c, fm, warmup=warmup, intrabar=stap.intrabar,
                                      trend_break=stap.trend_break,
                                      slippage_on_fill=stap.slippage)
    # Enkelvoudige modus draait per markt en wordt gemiddeld, zodat de vergelijking
    # met de portfolio-stap over dezelfde markten gaat.
    per_markt = {markt: run_backtest(candles, c, fm, warmup=warmup,
                                     intrabar=stap.intrabar,
                                     trend_break=stap.trend_break,
                                     slippage_on_fill=stap.slippage)
                 for markt, candles in data.items()}
    resultaten = list(per_markt.values())
    n = len(resultaten)
    trades = sum(r["closed_trades"] for r in resultaten)
    wins = sum((r["win_rate_pct"] or 0) * r["closed_trades"] / 100 for r in resultaten)
    return {
        "mode": "single (gemiddeld over markten)",
        "closed_trades": trades,
        "win_rate_pct": round(wins / trades * 100, 1) if trades else None,
        "net_return_pct": round(sum(r["net_return_pct"] for r in resultaten) / n, 2),
        "max_drawdown_pct": round(sum(r["max_drawdown_pct"] for r in resultaten) / n, 1),
        "total_fees_eur": round(sum(r["total_fees_eur"] for r in resultaten), 2),
        "per_markt": per_markt,
    }


def _uitkomst(r: dict) -> tuple:
    return (r["net_return_pct"], r["closed_trades"], r["win_rate_pct"],
            r["max_drawdown_pct"])


def attributie(data: dict[str, list[Candle]], cfg) -> list[dict]:
    controleer_geen_no_ops(cfg, STAPPEN)          # laag 1: declareert de stap iets?
    rijen, vorige, vorige_uitkomst = [], None, None
    for stap in STAPPEN:
        r = run_stap(data, cfg, stap)
        # Laag 2: kwam er ook iets uit? Geen exception, want een correctie die op
        # deze data echt niets doet ziet er identiek uit (de trend-break-exit is daar
        # het voorbeeld van). Wel zichtbaar, zodat "delta 0,00" nooit meer
        # onopgemerkt als bevinding gelezen wordt terwijl het een bedradingsfout is.
        identiek = vorige_uitkomst is not None and _uitkomst(r) == vorige_uitkomst
        rijen.append({
            "naam": stap.naam,
            "punt": stap.punt,
            "rendement": r["net_return_pct"],
            "delta": None if vorige is None else round(r["net_return_pct"] - vorige, 2),
            "trades": r["closed_trades"],
            "win": r["win_rate_pct"] or 0.0,
            "dd": r["max_drawdown_pct"],
            "productie": stap.productie,
            "identiek": identiek,
            "per_markt": r.get("per_markt"),
        })
        vorige, vorige_uitkomst = r["net_return_pct"], _uitkomst(r)
    return rijen


def print_attributie(rijen: list[dict]) -> None:
    print(f"\n{'stap':38s} {'punt':10s} {'rend%':>8s} {'delta':>8s} {'trades':>7s} "
          f"{'win%':>6s} {'dd%':>6s}")
    for r in rijen:
        delta = "  —" if r["delta"] is None else f"{r['delta']:+.2f}"
        vlag = "  <- productie" if r["productie"] else ""
        if r.get("identiek"):
            vlag += "  <- IDENTIEK aan de vorige rij: of de correctie doet op deze " \
                    "data niets, of de bedrading klopt niet"
        print(f"{r['naam']:38s} {r['punt']:10s} {r['rendement']:>8.2f} {delta:>8s} "
              f"{r['trades']:>7d} {r['win']:>6.1f} {r['dd']:>6.1f}{vlag}")
    referentie = rijen[0]
    if referentie.get("per_markt"):
        print("\nReferentierij per markt - leg deze vijf naast de ankerrun van de oude")
        print("code (commit 56d9e55). Wijken ze alle vijf een beetje af, dan zit het in de")
        print("kostenboekhouding; wijkt er een sterk af, dan in een specifieke trade.")
        print(f"\n{'markt':12s} {'rend%':>8s} {'trades':>7s} {'win%':>6s} {'dd%':>6s} "
              f"{'fees EUR':>9s}")
        for markt, r in sorted(referentie["per_markt"].items()):
            print(f"{markt:12s} {r['net_return_pct']:>8.2f} {r['closed_trades']:>7d} "
                  f"{(r['win_rate_pct'] or 0):>6.1f} {r['max_drawdown_pct']:>6.1f} "
                  f"{r['total_fees_eur']:>9.2f}")

    print("\nDe rij met '<- productie' beschrijft het huidige gedrag. De laatste rij")
    print("zet de breakeven-stop BINDEND (in een backtest doet een shadow-gate per")
    print("definitie niets); in productie staat hij op shadow. De chase-guard zit")
    print("helemaal niet in de backtester.")
    print("\nDe correcties zijn niet additief: een andere stapelvolgorde geeft andere")
    print("tussenstappen bij hetzelfde eindresultaat. Lees de delta's als 'wat deze")
    print("correctie toevoegde gegeven de voorgaande', niet als een losse bijdrage.")
    print("Blok 1 (entries op afgesloten candles) staat er bewust niet in: de")
    print("backtester had dat probleem nooit, want historische candles zijn altijd")
    print("afgesloten. Die fout zat in de engine en verklaart juist waarom de live")
    print("bot méér trades maakte dan de backtest voorspelde.")


def vergelijk_matrix(data: dict[str, list[Candle]], cfg) -> list[dict]:
    """Productieconfig in PORTFOLIO-modus, met time-stop en regime als assen.

    Twee vragen die de gestapelde attributie niet kan beantwoorden:

    * de time-stop-delta daar wordt in `single` gemeten, all-in en zonder slots,
      cooldown of correlatiecap. Dat is een bovengrens, geen productiegetal. En
      zodra de portfolio-stap aangaat zit de time-stop er al in, dus het getal dat
      je wilt weten rolt er nooit uit;
    * het regime-filter is het enige mechanisme dat al gebouwd is om een
      verliesregime te vermijden. De vraag is niet welke variant het meest verdiende
      in het gunstige venster, maar of er een configuratie is die het slechte
      venster overleeft.

    Vier runs, alle vier in de modus waarin de bot draait.
    """
    warmup = default_warmup(cfg.strategy)
    fm = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                  cfg.fees["slippage_buffer_pct"])
    proxy_market = str((getattr(cfg, "regime", {}) or {}).get("proxy_market", "BTC-EUR"))
    uit = []
    for n_ts in (int((cfg.exits or {}).get("time_stop_candles", 12) or 12), 0):
        for regime in (False, True):
            c = cfg.model_copy(deep=True)
            c.exits = {**(c.exits or {}), "time_stop_candles": n_ts}
            c.regime = {**(getattr(c, "regime", {}) or {}),
                        "enabled": True, "binding": regime}
            r = run_portfolio_backtest(data, c, fm, warmup=warmup,
                                       proxy_candles=data.get(proxy_market))
            # LET OP: `r` bevat zelf een sleutel "regime" (de gerapporteerde
            # status). De gevraagde as heet daarom `regime_gevraagd`, anders
            # overschrijft `**r` hem en tonen alle rijen dezelfde waarde.
            uit.append({"time_stop": n_ts, "regime_gevraagd": regime, **r})
    return uit


def print_vergelijking(rijen: list[dict]) -> None:
    print(f"\n{'time-stop':>10s} {'regime':>8s} {'status':>16s} {'rend%':>8s} "
          f"{'trades':>7s} {'win%':>6s} {'dd%':>6s} {'fees EUR':>9s}")
    for r in rijen:
        gevraagd = "aan" if r["regime_gevraagd"] else "uit"
        print(f"{r['time_stop']:>10d} {gevraagd:>8s} {r['regime']:>16s} "
              f"{r['net_return_pct']:>8.2f} {r['closed_trades']:>7d} "
              f"{(r['win_rate_pct'] or 0):>6.1f} {r['max_drawdown_pct']:>6.1f} "
              f"{r['total_fees_eur']:>9.2f}")
    if any(r["regime"] == "proxy ONTBREEKT" for r in rijen):
        print("\nLET OP: de proxy-markt ontbreekt in de dataset, dus de regime-gate heeft")
        print("niet gedraaid. Voeg hem toe aan de markten; een stil uitgeschakelde gate")
        print("leest als 'regime helpt niet' terwijl hij nooit is toegepast.")
    if rijen and rijen[0].get("buy_hold_pct") is not None:
        print(f"\nIJkpunt: gelijkgewogen kopen-en-vasthouden over dezelfde markten en "
              f"hetzelfde venster gaf {rijen[0]['buy_hold_pct']:+.2f}%.")
    print("\nAlle vier in portfolio-modus, dus onderling vergelijkbaar en vergelijkbaar")
    print("met de live-run. `time-stop 0` is de gate uit, niet op shadow: in een backtest")
    print("doet een shadow-gate per definitie niets.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("markets", nargs="+")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--limit", type=int, default=1100)
    parser.add_argument("--end", default=None,
                        help="einde van het venster (ISO-8601 of epoch ms). "
                             "Zonder dit pakt --limit de NIEUWSTE candles, dus "
                             "twee runs op verschillende momenten zien andere data")
    parser.add_argument("--vergelijk", action="store_true",
                        help="in plaats van de attributie: productieconfig in "
                             "portfolio-modus met time-stop en regime als assen")
    args = parser.parse_args()

    cfg = get_config()
    feed = BitvavoClient()
    end_ms = parse_end_ms(args.end)
    fetch = feed.get_candles_history if (args.limit > 1440 or end_ms) else feed.get_candles
    data = {m: fetch(m, args.interval, args.limit, end_ms) for m in args.markets}

    print(f"\nAttributie op de productievariant: ema{cfg.strategy['ema_fast']}/"
          f"{cfg.strategy['ema_slow']}, score>={cfg.strategy['min_signal_score']}, "
          f"atr*{cfg.decision['atr_stop_multiplier']}, rr{cfg.decision['reward_risk_ratio']}")
    eerste = next(iter(data.values()))
    print(f"{', '.join(args.markets)} ({args.interval}, "
          f"{min(len(v) for v in data.values())} candles per markt)")
    waarschuwing = "" if args.end else (
        "  <- NIET gepind; gebruik --end om dit venster vast te leggen en de "
        "ankerrun exact te herhalen")
    print(f"venster: {candle_window(eerste)}{waarschuwing}")
    if args.vergelijk:
        print("\nVergelijking op de productieconfig, alle vier in portfolio-modus")
        print_vergelijking(vergelijk_matrix(data, cfg))
        return
    print(f"warmup oud {OUDE_WARMUP} -> geschaald {default_warmup(cfg.strategy)} "
          f"(DEFAULT_WARMUP in de backtester is {DEFAULT_WARMUP})")
    print_attributie(attributie(data, cfg))


if __name__ == "__main__":
    main()
