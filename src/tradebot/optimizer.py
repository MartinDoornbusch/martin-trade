"""Grid-search parameteroptimalisatie op historische candles (fase 2 tool).

Overfitting-bescherming: data wordt 70/30 gesplitst. Er wordt gerankt op de
trainingsperiode, maar gerapporteerd wordt vooral de out-of-sample (test)
prestatie. Een grote kloof tussen train en test = overfit, niet gebruiken.

Usage:
    python -m tradebot.optimizer BTC-EUR --interval 4h --limit 3000
"""
from __future__ import annotations

import argparse
import itertools

from .backtest import run_backtest
from .config import get_config
from .decision import FeeModel
from .exchange import BitvavoClient

GRID = {
    "ema": [(9, 21), (12, 26), (20, 50)],
    "min_signal_score": [2, 3, 4],
    "atr_stop_multiplier": [1.5, 2.0, 2.5],
    "reward_risk_ratio": [1.5, 2.0, 3.0],
}


def variants(cfg):
    """Genereert (omschrijving, aangepaste config) per grid-combinatie."""
    for (ef, es), score, atr_m, rr in itertools.product(
            GRID["ema"], GRID["min_signal_score"],
            GRID["atr_stop_multiplier"], GRID["reward_risk_ratio"]):
        c = cfg.model_copy(deep=True)
        c.strategy["ema_fast"], c.strategy["ema_slow"] = ef, es
        c.strategy["min_signal_score"] = score
        c.decision["atr_stop_multiplier"] = atr_m
        c.decision["reward_risk_ratio"] = rr
        yield f"ema{ef}/{es} score>={score} atr*{atr_m} rr{rr}", c


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("market")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    cfg = get_config()
    feed = BitvavoClient()
    candles = feed.get_candles_history(args.market, args.interval, args.limit)
    split = int(len(candles) * 0.7)
    train, test = candles[:split], candles[split:]
    fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                         cfg.fees["slippage_buffer_pct"])

    results = []
    for desc, c in variants(cfg):
        r_train = run_backtest(train, c, fee_model)
        results.append((desc, c, r_train))
    results.sort(key=lambda r: -(r[2]["net_return_pct"] or -999))

    print(f"\nOptimizer {args.market} ({args.interval}): {len(candles)} candles, "
          f"train {len(train)} / test {len(test)}\n")
    print(f"{'variant':38s} {'train%':>8s} {'test%':>8s} {'trades':>7s} {'win%':>6s} {'dd%':>6s}")
    for desc, c, r_train in results[:args.top]:
        r_test = run_backtest(test, c, fee_model)
        print(f"{desc:38s} {r_train['net_return_pct']:>8.2f} {r_test['net_return_pct']:>8.2f} "
              f"{r_test['closed_trades']:>7d} "
              f"{(r_test['win_rate_pct'] or 0):>6.1f} {r_test['max_drawdown_pct']:>6.1f}")
    print("\nLet op: kies op test-prestatie, niet op train. Grote kloof = overfit.")


if __name__ == "__main__":
    main()
