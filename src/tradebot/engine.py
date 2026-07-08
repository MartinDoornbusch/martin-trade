"""Orchestrator: runs one full analysis/trade cycle across all configured markets."""
from __future__ import annotations

import logging

from .config import AppConfig, Secrets
from .correlation import correlation_from_closes
from .db import SignalRow, session
from .decision import Decision, DecisionEngine, FeeModel, RiskManager
from .exchange import BitvavoClient
from .lists import get_lists
from .llm import LLMRouter, build_router
from .notify import Notifier
from .paper import PaperBroker
from .strategy import build_snapshot, check_exit, evaluate_buy

log = logging.getLogger(__name__)


class TradingCycle:
    def __init__(self, cfg: AppConfig, secrets: Secrets):
        self.cfg = cfg
        self.secrets = secrets
        if secrets.trading_mode == "live":
            raise NotImplementedError(
                "Live mode is intentionally locked until phase 2 validation passes. "
                "See PROJECTPLAN.md — go/no-go criteria first.")
        self.feed = BitvavoClient(secrets.bitvavo_api_key, secrets.bitvavo_api_secret,
                                  cfg.fees["maker_pct"], cfg.fees["taker_pct"])
        self.fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                                  cfg.fees["slippage_buffer_pct"])
        self.broker = PaperBroker(self.feed, self.fee_model, cfg.risk["paper_start_eur"])
        self.decider = DecisionEngine(self.fee_model, RiskManager(cfg.risk), cfg.decision)
        self.llm: LLMRouter = build_router(cfg.llm_providers, secrets,
                                           int(cfg.llm.get("timeout_seconds", 20)))
        self.notify = Notifier(secrets.telegram_bot_token, secrets.telegram_chat_id)

    def run_once(self) -> list[Decision]:
        decisions: list[Decision] = []
        interval = self.cfg.schedule["candle_interval"]
        limit = int(self.cfg.schedule["candle_limit"])
        positions = self.broker.open_positions()
        portfolio = self.broker.portfolio_value_eur()
        free = self.broker.cash_eur()
        daily_pnl = self.broker.daily_pnl_eur()

        active_markets = get_lists(self.cfg)["markets"]
        candles_map = {}
        for market in active_markets:
            try:
                candles_map[market] = self.feed.get_candles(market, interval, limit)
            except Exception:  # noqa: BLE001
                log.exception("candles ophalen mislukt voor %s", market)

        for market, candles in candles_map.items():
            try:
                snap = build_snapshot(market, candles, self.cfg.strategy)

                # 1) Mechanical exits first — no AI involved.
                pos = next((p for p in positions if p.market == market), None)
                if pos:
                    should_exit, why = check_exit(pos.entry_price, pos.stop_loss,
                                                  pos.take_profit, snap)
                    if should_exit:
                        self.broker.sell(market, why)
                        self._log_signal(market, "sell", "executed", 0, why, {})
                        self.notify.send(f"🔴 SELL {market} @ {snap.price:.2f}: {why}")
                        decisions.append(Decision(market, "sell", why))
                        continue

                # 2) Candidate generation (deterministic).
                candidate = evaluate_buy(snap, self.cfg.strategy)
                decision = self.decider.evaluate_buy(candidate, positions,
                                                     self.broker.last_trade_at(market),
                                                     portfolio, free, daily_pnl)

                # 3) Correlatie-gate: geen 2e positie in een sterk gecorreleerde markt.
                if decision.action == "buy" and positions:
                    max_corr = float(self.cfg.risk.get("max_correlation", 0.85))
                    lookback = int(self.cfg.risk.get("correlation_lookback", 60))
                    closes = [c.close for c in candles]
                    for pos in positions:
                        other = candles_map.get(pos.market)
                        if other is None:
                            continue
                        corr = correlation_from_closes(closes, [c.close for c in other], lookback)
                        if corr is not None and corr > max_corr:
                            decision = Decision(
                                market, "skip",
                                f"correlatie-gate: {corr:.2f} met open positie {pos.market} "
                                f"> {max_corr}")
                            break

                # 4) LLM second opinion only for BUYs that passed every gate.
                if decision.action == "buy" and self.cfg.decision.get("use_llm_second_opinion"):
                    verdict = self.llm.second_opinion(candidate)
                    min_conf = float(self.cfg.decision["llm_min_confidence"])
                    if verdict is None:
                        decision = Decision(market, "skip",
                                            "LLM unavailable; conservative skip")
                    elif not verdict.agree or verdict.confidence < min_conf:
                        decision = Decision(
                            market, "skip",
                            f"LLM veto ({verdict.provider}, conf {verdict.confidence:.2f}): "
                            f"{verdict.reasoning}")

                # 5) Execute.
                if decision.action == "buy":
                    self.broker.buy(market, decision.amount_quote_eur,
                                    decision.stop_loss, decision.take_profit, decision.reason)
                    free -= decision.amount_quote_eur
                    self.notify.send(
                        f"🟢 BUY {market} voor {decision.amount_quote_eur:.2f} EUR @ "
                        f"{snap.price:.2f}\nSL {decision.stop_loss:.2f} / "
                        f"TP {decision.take_profit:.2f}\n{decision.reason}")

                self._log_signal(market, candidate.action, decision.action,
                                 candidate.score, decision.reason, decision.details)
                decisions.append(decision)
            except Exception:  # noqa: BLE001 - one market must not kill the cycle
                log.exception("cycle failed for %s", market)
        return decisions

    @staticmethod
    def _log_signal(market: str, action: str, decision: str, score: int,
                    reason: str, details: dict) -> None:
        with session() as s:
            s.add(SignalRow(market=market, action=action, decision=decision,
                            score=score, reason=reason[:1000], details=details))
            s.commit()
