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
from dataclasses import dataclass

from .backtest import DEFAULT_WARMUP, run_backtest, run_portfolio_backtest
from .config import get_config
from .decision import FeeModel
from .exchange import BitvavoClient, Candle
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
    return FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                    cfg.fees["slippage_buffer_pct"])


def run_stap(data: dict[str, list[Candle]], cfg, stap: Stap) -> dict:
    c = config_voor(cfg, stap)
    fm = fee_model_voor(cfg, stap)
    warmup = default_warmup(c.strategy) if stap.geschaalde_warmup else OUDE_WARMUP
    if stap.portfolio:
        return run_portfolio_backtest(data, c, fm, warmup=warmup, intrabar=stap.intrabar,
                                      trend_break=stap.trend_break)
    # Enkelvoudige modus draait per markt en wordt gemiddeld, zodat de vergelijking
    # met de portfolio-stap over dezelfde markten gaat.
    resultaten = [run_backtest(candles, c, fm, warmup=warmup, intrabar=stap.intrabar,
                               trend_break=stap.trend_break)
                  for candles in data.values()]
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
    }


def attributie(data: dict[str, list[Candle]], cfg) -> list[dict]:
    rijen, vorige = [], None
    for stap in STAPPEN:
        r = run_stap(data, cfg, stap)
        rijen.append({
            "naam": stap.naam,
            "punt": stap.punt,
            "rendement": r["net_return_pct"],
            "delta": None if vorige is None else round(r["net_return_pct"] - vorige, 2),
            "trades": r["closed_trades"],
            "win": r["win_rate_pct"] or 0.0,
            "dd": r["max_drawdown_pct"],
            "productie": stap.productie,
        })
        vorige = r["net_return_pct"]
    return rijen


def print_attributie(rijen: list[dict]) -> None:
    print(f"\n{'stap':38s} {'punt':10s} {'rend%':>8s} {'delta':>8s} {'trades':>7s} "
          f"{'win%':>6s} {'dd%':>6s}")
    for r in rijen:
        delta = "  —" if r["delta"] is None else f"{r['delta']:+.2f}"
        vlag = "  <- productie" if r["productie"] else ""
        print(f"{r['naam']:38s} {r['punt']:10s} {r['rendement']:>8.2f} {delta:>8s} "
              f"{r['trades']:>7d} {r['win']:>6.1f} {r['dd']:>6.1f}{vlag}")
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("markets", nargs="+")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--limit", type=int, default=1100)
    args = parser.parse_args()

    cfg = get_config()
    feed = BitvavoClient()
    fetch = feed.get_candles_history if args.limit > 1440 else feed.get_candles
    data = {m: fetch(m, args.interval, args.limit) for m in args.markets}

    print(f"\nAttributie op de productievariant: ema{cfg.strategy['ema_fast']}/"
          f"{cfg.strategy['ema_slow']}, score>={cfg.strategy['min_signal_score']}, "
          f"atr*{cfg.decision['atr_stop_multiplier']}, rr{cfg.decision['reward_risk_ratio']}")
    print(f"{', '.join(args.markets)} ({args.interval}, "
          f"{min(len(v) for v in data.values())} candles per markt)")
    print(f"warmup oud {OUDE_WARMUP} -> geschaald {default_warmup(cfg.strategy)} "
          f"(DEFAULT_WARMUP in de backtester is {DEFAULT_WARMUP})")
    print_attributie(attributie(data, cfg))


if __name__ == "__main__":
    main()
