"""Backtester: dezelfde strategie, dezelfde exits en hetzelfde fee-model als de engine.

Tot v0.19.0 modelleerde deze module een strategie die niet meer bestond: alleen
`check_exit` op de slotkoers, geen time-stop, geen breakeven-stop, geen slippage
op de fill en all-in sizing. Sinds v0.20.0 draait hij op dezelfde functies en
dezelfde config-objecten als `engine.TradingCycle`.

Twee modi; welke draaide staat expliciet in de output onder "mode":

* "single"    — één markt, all-in per positie, geen cooldown/dagcap/correlatie.
                Voor SIGNAALonderzoek: het rendement is bewust niet met live te
                vergelijken, want de bot zet in werkelijkheid vaste buckets over
                meerdere slots in. Wel de goedkoopste maat om varianten te rangschikken.
* "portfolio" — meerdere markten met gedeelde cash, bucket-sizing, slotlimiet,
                cooldown, dagverliescap en correlatie-cluster-cap, alle uit dezelfde
                `RiskManager` en dezelfde helpers als de engine. Hiermee zijn
                rendement én drawdown wel met de live-run te vergelijken.

Usage:
    python -m tradebot.backtest BTC-EUR --interval 4h --limit 1000
    python -m tradebot.backtest BTC-EUR ETH-EUR SOL-EUR --portfolio --limit 1000
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import get_config
from .correlation import correlation_from_closes
from .decision import FeeModel, Position, RiskManager, breakeven_offset_pct
from .exchange import BitvavoClient, Candle
from .strategy import (
    MarketSnapshot,
    breakeven_stop_hit,
    build_snapshots,
    evaluate_buy,
    intrabar_exit,
    time_stop_hit,
)

DEFAULT_WARMUP = 60
MIN_ORDER_EUR = 10.0   # zelfde ondergrens als DecisionEngine.evaluate_buy


def max_drawdown_pct(equity: list[float]) -> float:
    """Grootste piek-naar-dal terugval in procenten."""
    peak, max_dd = float("-inf"), 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)
    return round(max_dd, 1)


@dataclass
class _Pos:
    """Open positie tijdens een backtest-run."""
    market: str
    amount: float
    entry: float
    stop: float
    target: float
    fees_paid: float
    entry_index: int
    opened_ms: int


def leg_slippage_pct(fee_model: FeeModel) -> float:
    """Slippage per been = de helft van `slippage_buffer_pct`.

    De buffer is in `FeeModel.min_edge_pct` een ROUND-TRIP-post: hij staat daar
    naast `round_trip_pct` (2x fee) als één term, en de scanner telt de werkelijke
    spread op dezelfde manier één keer per round-trip mee. Dat klopt ook
    economisch: je koopt op de laat en verkoopt op de bied, wat over heen én terug
    samen precies één spread kost. De volle buffer op elk been zou de backtester
    stelselmatig strenger maken dan de fee-gate die de entry toelaat.

    Let op bij het lezen van de uitkomst: op een stop- of target-exit lijkt de
    slippage grotendeels te verdwijnen, want die niveaus liggen op
    `fill +/- k x ATR` en schuiven dus mee met een duurdere entry. Dat is geen
    gratis lunch maar een VERSCHUIVING: een stop die op de geslipte fill ankert,
    ligt verder van de prijs waarop het besluit is genomen dan de bedoelde
    2x ATR. De kost gaat van de rendementskolom naar de risicokolom. Alleen op
    een exit die niet aan de entry hangt (time-stop op de slotkoers) zie je de
    volle round-trip-drag terug in het rendement. `tests/test_backtest.py` pint
    beide paden vast, zodat een latere wijziging die de stop van de fill losmaakt
    niet stil van gedrag verandert.
    """
    return fee_model.slippage_buffer_pct / 2.0


def _interval_ms(candles: list[Candle]) -> int:
    return candles[-1].ts - candles[-2].ts if len(candles) >= 2 else 0


def _exits_params(cfg, fee_model: FeeModel, entry_is_maker: bool = False) -> dict:
    exits = getattr(cfg, "exits", {}) or {}
    be = exits.get("breakeven_stop", {}) or {}
    return {
        "time_stop_candles": int(exits.get("time_stop_candles", 0) or 0),
        "time_stop_min_net_pct": float(exits.get("time_stop_min_net_pct", 0.0)),
        "be_enabled": bool(be.get("enabled", False)),
        "be_binding": bool(be.get("binding", False)),
        "be_trigger_atr": float(be.get("trigger_atr", 1.0)),
        "be_offset_pct": breakeven_offset_pct(be, fee_model, entry_is_maker),
        "rsi_overbought": float((getattr(cfg, "strategy", {}) or {}).get(
            "rsi_overbought", 70)),
    }


def _check_exit_at_bar(pos: _Pos, candles: list[Candle], i: int, snap: MarketSnapshot,
                       ex: dict, round_trip_pct: float,
                       intrabar: bool = True,
                       trend_break: bool = False) -> tuple[float, str] | None:
    """Exit-beslissing voor bar `i`, in dezelfde volgorde als de engine.

    1. stop/target INTRABAR (`strategy.intrabar_exit`): de position guard draait
       live elke minuut, dus een candle die met zijn low door de stop ging telt als
       exit ook al sloot hij erboven. Vult op het niveau zelf.
    2. time-stop op de slotkoers.
    3. breakeven-stop op de slotkoers, alleen als hij in config bindend is; in
       shadow verkoopt de engine niet, dus de backtester ook niet.
    """
    if intrabar:
        what = intrabar_exit(candles[i], pos.stop, pos.target)
    else:
        # Alleen voor attributie (zie `tradebot.calibrate`): het oude, foute model
        # dat de SLOTKOERS met stop en target vergeleek. Nooit voor een echte meting.
        close_ = candles[i].close
        what = ("stop" if close_ <= pos.stop
                else "target" if close_ >= pos.target else None)
    if what == "stop":
        return pos.stop, "stop loss"
    if what == "target":
        return pos.target, "take profit"

    close = candles[i].close
    opened_at = datetime.fromtimestamp(pos.opened_ms / 1000, tz=timezone.utc)
    bar_closed_ms = candles[i].ts + _interval_ms(candles)
    window = candles[: i + 1]
    if ex["time_stop_candles"] > 0:
        hit, why = time_stop_hit(window, opened_at, pos.entry, close, round_trip_pct,
                                 ex["time_stop_candles"], ex["time_stop_min_net_pct"],
                                 now_ms=bar_closed_ms)
        if hit:
            return close, why
    if ex["be_enabled"] and ex["be_binding"]:
        hit, why = breakeven_stop_hit(window, opened_at, pos.entry, close, snap.atr,
                                      ex["be_trigger_atr"], ex["be_offset_pct"])
        if hit:
            return close, why
    if trend_break and snap.ema_fast < snap.ema_slow and snap.rsi > ex["rsi_overbought"]:
        # Alleen voor attributie: de trend-break-exit die tot v0.19.0 in `check_exit`
        # zat en daar geschrapt is omdat hij twee vrijwel disjuncte condities eiste.
        # Hoort in de referentierij van `tradebot.calibrate`, nergens anders.
        return close, "trend break: EMA cross down with overbought RSI"
    return None


def _run(candles_by_market: dict[str, list[Candle]], cfg, fee_model: FeeModel,
         start_eur: float, warmup: int, mode: str, intrabar: bool = True,
         trend_break: bool = False) -> dict:
    """Gedeelde kern voor beide modi: identieke entry- en exitlogica, alleen de
    sizing- en limietregels verschillen."""
    strategy_cfg = cfg.strategy
    decision_cfg = cfg.decision
    risk_cfg = getattr(cfg, "risk", {}) or {}
    ex = _exits_params(cfg, fee_model)
    min_edge = fee_model.min_edge_pct(float(decision_cfg["min_profit_pct"]))
    slip = leg_slippage_pct(fee_model)
    taker = fee_model.taker_pct
    round_trip = fee_model.round_trip_pct()
    atr_mult = float(decision_cfg["atr_stop_multiplier"])
    rr = float(decision_cfg["reward_risk_ratio"])
    portfolio_mode = mode == "portfolio"
    risk = RiskManager(risk_cfg) if portfolio_mode else None
    max_corr = float(risk_cfg.get("max_correlation", 0.85))
    corr_lookback = int(risk_cfg.get("correlation_lookback", 60))
    max_cluster = int(risk_cfg.get("max_correlated_positions", 2))

    markets = sorted(candles_by_market)
    snaps = {m: build_snapshots(m, candles_by_market[m], strategy_cfg) for m in markets}
    idx_by_ts = {m: {c.ts: i for i, c in enumerate(candles_by_market[m])} for m in markets}
    timeline = sorted({c.ts for cs in candles_by_market.values() for c in cs})

    cash = start_eur
    positions: dict[str, _Pos] = {}
    last_close: dict[str, float] = {}
    last_trade_ms: dict[str, int] = {}
    daily_pnl: dict[str, float] = {}
    trades = wins = 0
    total_fees = 0.0
    exit_reasons: dict[str, int] = {}
    equity_curve: list[float] = []

    def equity() -> float:
        return cash + sum(p.amount * last_close.get(p.market, p.entry)
                          for p in positions.values())

    for ts in timeline:
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        for market in markets:
            i = idx_by_ts[market].get(ts)
            if i is None or i < warmup:
                continue
            candles = candles_by_market[market]
            snap = snaps[market][i]
            if snap is None:
                continue
            last_close[market] = snap.price

            pos = positions.get(market)
            if pos is not None:
                if i <= pos.entry_index:
                    continue
                hit = _check_exit_at_bar(pos, candles, i, snap, ex, round_trip,
                                         intrabar, trend_break)
                if hit is None:
                    continue
                raw_price, why = hit
                fill = raw_price * (1 - slip / 100)
                gross = pos.amount * fill
                fee = gross * taker / 100
                cash += gross - fee
                total_fees += fee
                pnl = gross - fee - (pos.amount * pos.entry + pos.fees_paid)
                daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
                trades += 1
                wins += 1 if pnl > 0 else 0
                key = why.split(":")[0].strip()
                exit_reasons[key] = exit_reasons.get(key, 0) + 1
                last_trade_ms[market] = ts
                del positions[market]
                continue

            if not snap.atr > 0:
                continue
            if evaluate_buy(snap, strategy_cfg).action != "buy":
                continue
            stop_dist = snap.atr * atr_mult
            expected_pct = stop_dist * rr / snap.price * 100
            if expected_pct < min_edge:
                continue

            if portfolio_mode:
                open_list = [
                    Position(p.market, p.amount, p.entry, p.stop, p.target,
                             datetime.fromtimestamp(p.opened_ms / 1000, tz=timezone.utc),
                             p.fees_paid)
                    for p in positions.values()]
                last_at = (datetime.fromtimestamp(last_trade_ms[market] / 1000,
                                                  tz=timezone.utc)
                           if market in last_trade_ms else None)
                now = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                ok, _ = _can_open(risk, market, open_list, last_at, equity(),
                                  daily_pnl.get(day, 0.0), now)
                if not ok:
                    continue
                if positions and _cluster_blocked(candles_by_market, positions, market, i,
                                                  idx_by_ts, max_corr, corr_lookback,
                                                  max_cluster):
                    continue
                spend = min(risk.position_size_eur(equity(), cash), cash)
            else:
                if positions:
                    continue
                spend = cash

            if spend < MIN_ORDER_EUR:
                continue
            fill = snap.price * (1 + slip / 100)
            fee = spend * taker / 100
            amount = (spend - fee) / fill
            total_fees += fee
            cash -= spend
            positions[market] = _Pos(
                market=market, amount=amount, entry=fill,
                stop=fill - stop_dist, target=fill + stop_dist * rr,
                fees_paid=fee, entry_index=i, opened_ms=ts)
        equity_curve.append(equity())

    final = equity_curve[-1] if equity_curve else start_eur
    return {
        "mode": mode,
        "markets": len(markets),
        "warmup": warmup,
        "closed_trades": trades,
        "open_at_end": len(positions),
        "win_rate_pct": round(wins / trades * 100, 1) if trades else None,
        "net_return_pct": round((final / start_eur - 1) * 100, 2),
        "total_fees_eur": round(total_fees, 2),
        "max_drawdown_pct": max_drawdown_pct(equity_curve),
        "exit_reasons": exit_reasons,
        "final_eur": round(final, 2),
    }


def _can_open(risk: RiskManager, market: str, open_list: list[Position],
              last_at: datetime | None, portfolio_eur: float, daily_pnl_eur: float,
              now: datetime) -> tuple[bool, str]:
    """`RiskManager.can_open` met de backtest-klok in plaats van de wandklok.

    De cooldown is de enige regel die `datetime.now()` gebruikt; die wordt hier
    expliciet op de bar-tijd getoetst zodat historische runs reproduceerbaar zijn.
    """
    if any(p.market == market for p in open_list):
        return False, "position already open in this market"
    if len(open_list) >= risk.effective_max_positions(portfolio_eur):
        return False, "max open positions reached"
    if last_at is not None and now - last_at < risk.cooldown:
        return False, "cooldown active"
    if portfolio_eur > 0 and daily_pnl_eur < -portfolio_eur * risk.daily_loss_cap_pct / 100:
        return False, "daily loss cap reached"
    return True, "ok"


def _cluster_blocked(candles_by_market: dict[str, list[Candle]], positions: dict[str, _Pos],
                     market: str, i: int, idx_by_ts: dict, max_corr: float,
                     lookback: int, max_cluster: int) -> bool:
    """Correlatie-cluster-cap, zelfde regel als in de engine: vanaf `max_cluster`
    gecorreleerde open posities gaat er geen nieuwe bij."""
    own = [c.close for c in candles_by_market[market][: i + 1]]
    ts = candles_by_market[market][i].ts
    n = 0
    for other in positions:
        j = idx_by_ts[other].get(ts)
        if j is None:
            continue
        corr = correlation_from_closes(own, [c.close for c in candles_by_market[other][: j + 1]],
                                       lookback)
        if corr is not None and corr > max_corr:
            n += 1
    return n >= max_cluster


def run_backtest(candles: list[Candle], cfg, fee_model: FeeModel,
                 start_eur: float = 1000.0, warmup: int = DEFAULT_WARMUP,
                 intrabar: bool = True, trend_break: bool = False) -> dict:
    """Enkelvoudige markt, all-in per positie: modus "single" (signaalonderzoek)."""
    return _run({"BT": candles}, cfg, fee_model, start_eur, warmup, "single", intrabar,
                trend_break)


def run_portfolio_backtest(candles_by_market: dict[str, list[Candle]], cfg,
                           fee_model: FeeModel, start_eur: float = 1000.0,
                           warmup: int = DEFAULT_WARMUP,
                           intrabar: bool = True, trend_break: bool = False) -> dict:
    """Meerdere markten met gedeelde cash en alle risk-gates: modus "portfolio"."""
    return _run(candles_by_market, cfg, fee_model, start_eur, warmup, "portfolio",
                intrabar, trend_break)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("markets", nargs="+")
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--portfolio", action="store_true",
                        help="portfolio-modus: gedeelde cash, buckets, slots, gates")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    args = parser.parse_args()
    cfg = get_config()
    feed = BitvavoClient()
    fetch = feed.get_candles_history if args.limit > 1440 else feed.get_candles
    data = {m: fetch(m, args.interval, args.limit) for m in args.markets}
    fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                         cfg.fees["slippage_buffer_pct"])
    if args.portfolio or len(args.markets) > 1:
        result = run_portfolio_backtest(data, cfg, fee_model, warmup=args.warmup)
    else:
        result = run_backtest(next(iter(data.values())), cfg, fee_model, warmup=args.warmup)
    n = sum(len(v) for v in data.values())
    print(f"\nBacktest {', '.join(args.markets)} ({args.interval}, {n} candles)")
    for k, v in result.items():
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
