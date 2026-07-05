"""Backtester: same strategy + fee model over historical candles.

Usage:
    python -m tradebot.backtest BTC-EUR --interval 4h --limit 1000
"""
from __future__ import annotations

import argparse

from .config import get_config
from .decision import FeeModel
from .exchange import BitvavoClient, Candle
from .strategy import build_snapshot, check_exit, evaluate_buy


def max_drawdown_pct(equity: list[float]) -> float:
    """Grootste piek-naar-dal terugval in procenten."""
    peak, max_dd = float("-inf"), 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)
    return round(max_dd, 1)


def run_backtest(candles: list[Candle], cfg, fee_model: FeeModel,
                 start_eur: float = 1000.0) -> dict:
    strategy_cfg = cfg.strategy
    decision_cfg = cfg.decision
    warmup = 60
    cash = start_eur
    position = None  # (amount, entry, stop, target, fees_paid)
    trades, wins, total_fees = 0, 0, 0.0
    equity_curve = []
    min_edge = fee_model.min_edge_pct(float(decision_cfg["min_profit_pct"]))

    for i in range(warmup, len(candles)):
        window = candles[: i + 1]
        snap = build_snapshot("BT", window, strategy_cfg)
        price = snap.price

        if position:
            amount, entry, stop, target, fees_paid = position
            should_exit, _ = check_exit(entry, stop, target, snap)
            if should_exit:
                gross = amount * price
                fee = gross * fee_model.taker_pct / 100
                cash += gross - fee
                total_fees += fee
                pnl = gross - fee - (amount * entry + fees_paid)
                trades += 1
                wins += 1 if pnl > 0 else 0
                position = None
        else:
            cand = evaluate_buy(snap, strategy_cfg)
            if cand.action == "buy":
                stop_dist = snap.atr * float(decision_cfg["atr_stop_multiplier"])
                expected_pct = stop_dist * float(decision_cfg["reward_risk_ratio"]) / price * 100
                if expected_pct >= min_edge and cash > 10:
                    spend = cash  # single-market backtest: all-in per position
                    fee = spend * fee_model.taker_pct / 100
                    amount = (spend - fee) / price
                    total_fees += fee
                    cash = 0.0
                    stop = price - stop_dist
                    target = price + stop_dist * float(decision_cfg["reward_risk_ratio"])
                    position = (amount, price, stop, target, fee)

        equity_curve.append(cash + (position[0] * price if position else 0.0))

    final = equity_curve[-1] if equity_curve else start_eur
    max_dd = max_drawdown_pct(equity_curve)
    return {
        "closed_trades": trades,
        "win_rate_pct": round(wins / trades * 100, 1) if trades else None,
        "net_return_pct": round((final / start_eur - 1) * 100, 2),
        "total_fees_eur": round(total_fees, 2),
        "max_drawdown_pct": max_dd,
        "final_eur": round(final, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("market")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    cfg = get_config()
    feed = BitvavoClient()
    if args.limit > 1440:
        candles = feed.get_candles_history(args.market, args.interval, args.limit)
    else:
        candles = feed.get_candles(args.market, args.interval, args.limit)
    fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                         cfg.fees["slippage_buffer_pct"])
    result = run_backtest(candles, cfg, fee_model)
    print(f"\nBacktest {args.market} ({args.interval}, {len(candles)} candles)")
    for k, v in result.items():
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
