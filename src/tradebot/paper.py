"""Paper trading engine: real market data, simulated fills with real fee percentages.

Conservative by design: fills at current market price with TAKER fee, so results
underestimate what maker (limit) orders would achieve live.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from .db import KVRow, PositionRow, TradeRow, session
from .decision import FeeModel, Position
from .exchange import ExchangeAdapter, OrderResult

log = logging.getLogger(__name__)

CASH_KEY = "paper_cash_eur"
FEES_KEY = "paper_fees_cumulative_eur"


class PaperBroker:
    """Implements order execution against live prices from a real data feed."""

    def __init__(self, data_feed: ExchangeAdapter, fee_model: FeeModel, start_eur: float):
        self.feed = data_feed
        self.fees = fee_model
        with session() as s:
            if s.get(KVRow, CASH_KEY) is None:
                s.add(KVRow(key=CASH_KEY, value=str(start_eur)))
                s.add(KVRow(key=FEES_KEY, value="0"))
                s.commit()

    # --- state ------------------------------------------------------------
    def cash_eur(self) -> float:
        with session() as s:
            return float(s.get(KVRow, CASH_KEY).value)

    def _set_cash(self, s, value: float) -> None:
        s.get(KVRow, CASH_KEY).value = str(round(value, 8))

    def _add_fees(self, s, fee: float) -> None:
        row = s.get(KVRow, FEES_KEY)
        row.value = str(round(float(row.value) + fee, 8))

    def fees_cumulative_eur(self) -> float:
        with session() as s:
            return float(s.get(KVRow, FEES_KEY).value)

    def open_positions(self) -> list[Position]:
        with session() as s:
            rows = s.execute(select(PositionRow)).scalars().all()
        return [Position(r.market, r.amount, r.entry_price, r.stop_loss,
                         r.take_profit, r.opened_at, r.fees_paid_eur) for r in rows]

    def portfolio_value_eur(self) -> float:
        total = self.cash_eur()
        for p in self.open_positions():
            total += p.amount * self.feed.get_price(p.market)
        return total

    def last_trade_at(self, market: str) -> datetime | None:
        with session() as s:
            row = s.execute(select(TradeRow).where(TradeRow.market == market)
                            .order_by(TradeRow.ts.desc()).limit(1)).scalar_one_or_none()
        if row is None:
            return None
        ts = row.ts
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def daily_pnl_eur(self) -> float:
        today = datetime.now(timezone.utc).date()
        with session() as s:
            rows = s.execute(select(TradeRow).where(TradeRow.side == "sell")).scalars().all()
        return sum(r.pnl_eur for r in rows
                   if (r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)).date() == today)

    # --- execution ----------------------------------------------------------
    def buy(self, market: str, amount_quote_eur: float, stop_loss: float,
            take_profit: float, reason: str) -> OrderResult:
        price = self.feed.get_price(market)
        fee = amount_quote_eur * self.fees.taker_pct / 100
        amount = (amount_quote_eur - fee) / price
        with session() as s:
            cash = float(s.get(KVRow, CASH_KEY).value)
            if cash < amount_quote_eur:
                raise ValueError(f"insufficient paper cash: {cash:.2f} < {amount_quote_eur:.2f}")
            self._set_cash(s, cash - amount_quote_eur)
            self._add_fees(s, fee)
            s.add(PositionRow(market=market, amount=amount, entry_price=price,
                              stop_loss=stop_loss, take_profit=take_profit, fees_paid_eur=fee))
            s.add(TradeRow(market=market, side="buy", amount=amount, price=price,
                           fee_eur=fee, mode="paper", reason=reason[:500]))
            s.commit()
        log.info("PAPER BUY %s: %.8f @ %.2f (fee %.2f EUR)", market, amount, price, fee)
        return OrderResult(str(uuid.uuid4()), market, "buy", amount, price, fee)

    def sell(self, market: str, reason: str) -> OrderResult:
        price = self.feed.get_price(market)
        with session() as s:
            pos = s.execute(select(PositionRow).where(PositionRow.market == market)
                            ).scalar_one_or_none()
            if pos is None:
                raise ValueError(f"no open paper position in {market}")
            gross = pos.amount * price
            fee = gross * self.fees.taker_pct / 100
            net = gross - fee
            cost_basis = pos.amount * pos.entry_price + pos.fees_paid_eur
            pnl = net - cost_basis
            cash = float(s.get(KVRow, CASH_KEY).value)
            self._set_cash(s, cash + net)
            self._add_fees(s, fee)
            s.add(TradeRow(market=market, side="sell", amount=pos.amount, price=price,
                           fee_eur=fee, pnl_eur=pnl, mode="paper", reason=reason[:500]))
            s.delete(pos)
            s.commit()
        log.info("PAPER SELL %s: %.8f @ %.2f (fee %.2f, pnl %.2f EUR)",
                 market, pos.amount, price, fee, pnl)
        return OrderResult(str(uuid.uuid4()), market, "sell", pos.amount, price, fee)
