"""Fee-aware decision engine and risk management.

Hard lesson from the previous attempt: 27% correct calls + fees = -15% capital.
Therefore every BUY must pass the fee gate BEFORE any LLM is consulted:

    expected_move_pct >= round_trip_fee + slippage_buffer + min_profit

The LLM can only veto a candidate that already passed all mechanical gates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .strategy import Candidate

log = logging.getLogger(__name__)


@dataclass
class FeeModel:
    maker_pct: float
    taker_pct: float
    slippage_buffer_pct: float

    def round_trip_pct(self, use_taker: bool = True) -> float:
        fee = self.taker_pct if use_taker else self.maker_pct
        return 2 * fee  # buy + sell

    def min_edge_pct(self, min_profit_pct: float, use_taker: bool = True) -> float:
        return self.round_trip_pct(use_taker) + self.slippage_buffer_pct + min_profit_pct


@dataclass
class Position:
    market: str
    amount: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    fees_paid_eur: float = 0.0


@dataclass
class Decision:
    market: str
    action: str                     # "buy" | "sell" | "skip"
    reason: str
    amount_quote_eur: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    details: dict = field(default_factory=dict)


class RiskManager:
    def __init__(self, cfg: dict):
        self.max_position_pct = float(cfg["max_position_pct"])
        self.max_open_positions = int(cfg["max_open_positions"])
        self.cooldown = timedelta(hours=float(cfg["cooldown_hours_after_trade"]))
        self.daily_loss_cap_pct = float(cfg["daily_loss_cap_pct"])

    def can_open(self, market: str, open_positions: list[Position],
                 last_trade_at: datetime | None, portfolio_eur: float,
                 daily_pnl_eur: float) -> tuple[bool, str]:
        if any(p.market == market for p in open_positions):
            return False, "position already open in this market"
        if len(open_positions) >= self.max_open_positions:
            return False, f"max open positions ({self.max_open_positions}) reached"
        if last_trade_at and datetime.now(timezone.utc) - last_trade_at < self.cooldown:
            return False, f"cooldown active until {(last_trade_at + self.cooldown).isoformat()}"
        if portfolio_eur > 0 and daily_pnl_eur < -portfolio_eur * self.daily_loss_cap_pct / 100:
            return False, f"daily loss cap ({self.daily_loss_cap_pct}%) reached"
        return True, "ok"

    def position_size_eur(self, portfolio_eur: float, free_eur: float) -> float:
        return min(portfolio_eur * self.max_position_pct / 100, free_eur)


class DecisionEngine:
    def __init__(self, fee_model: FeeModel, risk: RiskManager, decision_cfg: dict):
        self.fees = fee_model
        self.risk = risk
        self.cfg = decision_cfg

    def expected_move_pct(self, candidate: Candidate) -> float:
        """ATR-based expected favourable move to the take-profit level."""
        snap = candidate.snapshot
        stop_dist = snap.atr * float(self.cfg["atr_stop_multiplier"])
        target_dist = stop_dist * float(self.cfg["reward_risk_ratio"])
        return target_dist / snap.price * 100

    def levels(self, candidate: Candidate) -> tuple[float, float]:
        snap = candidate.snapshot
        stop_dist = snap.atr * float(self.cfg["atr_stop_multiplier"])
        stop = snap.price - stop_dist
        target = snap.price + stop_dist * float(self.cfg["reward_risk_ratio"])
        return stop, target

    def evaluate_buy(self, candidate: Candidate, open_positions: list[Position],
                     last_trade_at: datetime | None, portfolio_eur: float,
                     free_eur: float, daily_pnl_eur: float) -> Decision:
        market = candidate.market
        if candidate.action != "buy":
            return Decision(market, "skip", f"no signal (score {candidate.score})")

        # Gate 1: risk limits
        ok, why = self.risk.can_open(market, open_positions, last_trade_at,
                                     portfolio_eur, daily_pnl_eur)
        if not ok:
            return Decision(market, "skip", f"risk gate: {why}")

        # Gate 2: fee gate — the core protection against fee bleed
        expected = self.expected_move_pct(candidate)
        min_edge = self.fees.min_edge_pct(float(self.cfg["min_profit_pct"]))
        if expected < min_edge:
            return Decision(market, "skip",
                            f"fee gate: expected move {expected:.2f}% < required {min_edge:.2f}%",
                            details={"expected_pct": expected, "min_edge_pct": min_edge})

        size = self.risk.position_size_eur(portfolio_eur, free_eur)
        if size < 10:  # Bitvavo minimum order ~5 EUR; below 10 fees dominate
            return Decision(market, "skip", f"position size too small ({size:.2f} EUR)")

        stop, target = self.levels(candidate)
        return Decision(market, "buy",
                        "; ".join(candidate.reasons),
                        amount_quote_eur=round(size, 2),
                        stop_loss=stop, take_profit=target,
                        details={"expected_pct": expected, "min_edge_pct": min_edge,
                                 "score": candidate.score})


def apply_second_opinion(decision: Decision, verdict, min_conf: float,
                         binding: bool = True) -> Decision:
    """Pas het LLM-tweede-oordeel toe op een buy-besluit.

    binding=True (normaal): een veto (LLM oneens of confidence < drempel) blokkeert
    de koop en wordt een skip.

    binding=False (shadow-mode): het veto wordt nog steeds door de LLM-laag gelogd,
    maar is niet bindend. De koop blijft staan, geannoteerd met de veto-reden, zodat
    de waarde van de gate gemeten kan worden zonder dat hij trades kost. LLM
    onbereikbaar telt in shadow-mode niet als veto (de koop gaat door).

    `verdict` is duck-typed (velden agree, confidence, reasoning, provider) of None.
    """
    if verdict is None:
        if binding:
            return Decision(decision.market, "skip", "LLM unavailable; conservative skip")
        return decision
    vetoed = (not verdict.agree) or (verdict.confidence < min_conf)
    if not vetoed:
        return decision
    veto_reason = (f"LLM veto ({verdict.provider}, conf {verdict.confidence:.2f}): "
                   f"{verdict.reasoning}")
    if binding:
        return Decision(decision.market, "skip", veto_reason)
    return Decision(
        decision.market, "buy",
        f"{decision.reason} | SHADOW-VETO genegeerd: {veto_reason}",
        amount_quote_eur=decision.amount_quote_eur,
        stop_loss=decision.stop_loss, take_profit=decision.take_profit,
        details={**decision.details, "shadow_veto": veto_reason})
