"""Live broker: echte Bitvavo-orders. Fase 3-code, gebouwd tijdens fase 2 maar
dubbel vergrendeld: activering vereist TRADING_MODE=live én de letterlijke
bevestigingszin in de configuratie (zie engine.LIVE_CONFIRM_PHRASE).

Ontwerpkeuzes:
- Entries: limit postOnly (maker, 0,15%) op de beste bied. Niet gevuld binnen de
  timeout = annuleren en overslaan; een gemiste entry kost niets.
- Exits: market order (taker). Een gemiste exit kost kapitaal; snelheid wint.
- Exposure hard begrensd op live_max_capital_eur, los van wat er op de rekening staat.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .db import PositionRow, TradeRow, session
from .decision import FeeModel, Position

log = logging.getLogger(__name__)

MODE = "live"


class LiveBroker:
    def __init__(self, client, fee_model: FeeModel, max_capital_eur: float,
                 entry_timeout_s: float = 90.0, poll_interval_s: float = 3.0):
        self.client = client
        self.fees = fee_model
        self.max_capital = float(max_capital_eur)
        self.entry_timeout_s = entry_timeout_s
        self.poll_interval_s = poll_interval_s

    # --- state -------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        with session() as s:
            rows = s.execute(select(PositionRow).where(PositionRow.mode == MODE)
                             ).scalars().all()
        return [Position(r.market, r.amount, r.entry_price, r.stop_loss,
                         r.take_profit, r.opened_at, r.fees_paid_eur) for r in rows]

    def exposure_eur(self) -> float:
        """Ingelegd kapitaal in open live-posities (op entry-basis, conservatief)."""
        return sum(p.amount * p.entry_price + p.fees_paid_eur for p in self.open_positions())

    def cash_eur(self) -> float:
        """Besteedbaar voor de bot: echte EUR-balans, begrensd door het live-plafond."""
        try:
            eur = float(self.client.get_balances().get("EUR", 0.0))
        except Exception:  # noqa: BLE001 - geen balans = niets besteedbaar
            log.exception("live: balans ophalen mislukt")
            return 0.0
        headroom = max(0.0, self.max_capital - self.exposure_eur())
        return min(eur, headroom)

    def portfolio_value_eur(self) -> float:
        total = self.cash_eur()
        for p in self.open_positions():
            try:
                total += p.amount * self.client.get_price(p.market)
            except Exception:  # noqa: BLE001
                total += p.amount * p.entry_price
        return total

    def _live_trades(self, side: str | None = None) -> list:
        with session() as s:
            q = select(TradeRow).where(TradeRow.mode == MODE)
            if side:
                q = q.where(TradeRow.side == side)
            return s.execute(q).scalars().all()

    def fees_cumulative_eur(self) -> float:
        return round(sum(t.fee_eur for t in self._live_trades()), 8)

    def daily_pnl_eur(self) -> float:
        today = datetime.now(timezone.utc).date()
        return sum(t.pnl_eur for t in self._live_trades("sell")
                   if (t.ts if t.ts.tzinfo else t.ts.replace(tzinfo=timezone.utc)).date() == today)

    def last_trade_at(self, market: str) -> datetime | None:
        rows = [t for t in self._live_trades() if t.market == market]
        if not rows:
            return None
        ts = max(t.ts for t in rows)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    # --- execution -----------------------------------------------------------
    def buy(self, market: str, amount_quote_eur: float, stop_loss: float,
            take_profit: float, reason: str):
        if self.exposure_eur() + amount_quote_eur > self.max_capital + 0.01:
            raise ValueError(
                f"live exposure-plafond: {self.exposure_eur():.2f} + {amount_quote_eur:.2f} "
                f"> {self.max_capital:.2f} EUR")
        book = self.client.get_book_ticker(market)
        bid = str(book["bid"])
        amount = f"{(amount_quote_eur / float(bid)) * (1 - self.fees.maker_pct / 100):.8f}"
        order = self.client.place_limit_order(market, "buy", amount, bid, post_only=True)
        order_id = order["orderId"]
        deadline = time.monotonic() + self.entry_timeout_s
        status = order
        while time.monotonic() < deadline:
            status = self.client.get_order(market, order_id)
            if status.get("status") == "filled":
                break
            time.sleep(self.poll_interval_s)
        filled = float(status.get("filledAmount") or 0)
        if status.get("status") != "filled":
            try:
                self.client.cancel_order(market, order_id)
            except Exception:  # noqa: BLE001 - order kan net gevuld zijn
                log.warning("live: cancel van %s faalde (mogelijk net gevuld)", order_id)
            status = self.client.get_order(market, order_id)
            filled = float(status.get("filledAmount") or 0)
        if filled <= 0:
            raise TimeoutError(f"live entry {market} niet gevuld binnen "
                               f"{self.entry_timeout_s:.0f}s; order geannuleerd")
        quote = float(status.get("filledAmountQuote") or 0)
        avg = quote / filled if filled else float(bid)
        fee = float(status.get("feePaid") or 0)
        with session() as s:
            s.add(PositionRow(market=market, mode=MODE, amount=filled, entry_price=avg,
                              stop_loss=stop_loss, take_profit=take_profit, fees_paid_eur=fee))
            s.add(TradeRow(market=market, side="buy", amount=filled, price=avg,
                           fee_eur=fee, mode=MODE, reason=reason[:500]))
            s.commit()
        log.info("LIVE BUY %s: %.8f @ %.4f (maker fee %.4f EUR)", market, filled, avg, fee)

    def sell(self, market: str, reason: str):
        with session() as s:
            pos = s.execute(select(PositionRow).where(PositionRow.market == market,
                                                      PositionRow.mode == MODE)
                            ).scalar_one_or_none()
            if pos is None:
                raise ValueError(f"geen open live-positie in {market}")
        result = self.client.place_market_order(market, "sell", pos.amount)
        gross = result.amount * result.price
        net = gross - result.fee_eur
        cost = pos.amount * pos.entry_price + pos.fees_paid_eur
        pnl = net - cost
        with session() as s:
            row = s.execute(select(PositionRow).where(PositionRow.market == market,
                                                      PositionRow.mode == MODE)).scalar_one()
            s.add(TradeRow(market=market, side="sell", amount=result.amount,
                           price=result.price, fee_eur=result.fee_eur, pnl_eur=pnl,
                           mode=MODE, reason=reason[:500]))
            s.delete(row)
            s.commit()
        log.info("LIVE SELL %s: %.8f @ %.4f (fee %.4f, pnl %.2f EUR)",
                 market, result.amount, result.price, result.fee_eur, pnl)
