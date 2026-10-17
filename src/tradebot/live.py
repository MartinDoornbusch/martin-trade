"""Live broker: echte Bitvavo-orders. Fase 3-code, dubbel vergrendeld: activering
vereist TRADING_MODE=live én de letterlijke bevestigingszin (engine.LIVE_CONFIRM_PHRASE).

Ontwerpkeuzes (sinds v0.11.0):
- Entries: limit postOnly (maker) op de beste bied. Niet gevuld binnen de timeout =
  annuleren en overslaan; een gemiste entry kost niets.
- Exits: market order (taker). Een gemiste exit kost kapitaal; snelheid wint.
- Exposure hard begrensd op `live_max_capital_eur`, los van wat er op de rekening staat.

Herbouwd in v0.25.0 rond vier live-blockers uit de reviews:

- L1/N2 Alleen BEVESTIGDE fills worden geboekt. Bitvavo vult een market order bij
  slippage-bescherming gedeeltelijk en annuleert de rest; de oude `sell` boekte dan
  de hele positie weg (gereproduceerd: 40% fill boekte -60,25 i.p.v. -0,25).
- L4/N4 Elke order staat eerst als `OrderRow` met een eigen `clientOrderId` in de
  database (outbox) en gaat pas daarna naar Bitvavo. Na een timeout wordt hij met
  die id opgevraagd in plaats van blind opnieuw geplaatst; `reconcile` maakt elke
  regel af die niet final is en legt de administratie tegen de saldi.
- L2 Na elke entry staat er een `stopLoss`-order op de exchange. Die reserveert de
  coins, dus het koersdoel blijft in de bot: elke exit annuleert eerst de stop,
  boekt wat de stop al verkocht had en verkoopt dan pas de rest.
- N3 Hoeveelheden en prijzen worden afgerond op de regels van de markt
  (`quantityDecimals`, `tickSize`) en getoetst aan het minimum. Tot v0.25.0 ging
  alles met 8 decimalen de deur uit.

Elke order loopt binnen het slot van zijn markt (`locks.MARKET_LOCKS`, L3).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import or_, select

from .db import OrderRow, PositionRow, TradeRow, session
from .decision import FeeModel, Position, PositionGone
from .exchange import FINAL_STATUSES, BitvavoApiError, MarketInfo, fmt_decimal
from .locks import MARKET_LOCKS, MarketBusy

log = logging.getLogger(__name__)

MODE = "live"
ACTIVE_STOP_STATUSES = frozenset({"new", "awaitingTrigger", "partiallyFilled"})
EPS = 1e-12


class OrderRejectedError(RuntimeError):
    """Bitvavo weigerde de order definitief: hij is zeker niet geplaatst."""


class OrderStateUnknownError(RuntimeError):
    """Niet vast te stellen of de order bestaat; `reconcile` lost dit later op."""


class ExitNotFilledError(RuntimeError):
    """De exit-order is geplaatst maar niets is verkocht (expired of geannuleerd)."""


class PartialExitError(RuntimeError):
    """Een deel is verkocht; de rest staat open en heeft weer een stop."""


def _base(market: str) -> str:
    return market.split("-", 1)[0]


class LiveBroker:
    mode = MODE
    entry_is_maker = True   # limit postOnly entry, market exit

    def __init__(self, client, fee_model: FeeModel, max_capital_eur: float,
                 entry_timeout_s: float = 90.0, poll_interval_s: float = 3.0,
                 exit_timeout_s: float = 15.0, stop_trigger_reference: str = "lastTrade"):
        self.client = client
        self.fees = fee_model
        self.max_capital = float(max_capital_eur)
        self.entry_timeout_s = entry_timeout_s
        self.poll_interval_s = poll_interval_s
        self.exit_timeout_s = exit_timeout_s
        self.stop_reference = stop_trigger_reference

    # --- state -------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        with session() as s:
            rows = s.execute(select(PositionRow).where(PositionRow.mode == MODE)
                             ).scalars().all()
        return [Position(r.market, r.amount, r.entry_price, r.stop_loss,
                         r.take_profit, r.opened_at, r.fees_paid_eur,
                         r.stop_order_cid or "") for r in rows]

    def exposure_eur(self) -> float:
        """Ingelegd kapitaal in open live-posities (op entry-basis, conservatief)."""
        return sum(p.amount * p.entry_price + p.fees_paid_eur for p in self.open_positions())

    def cash_eur(self) -> float:
        """Besteedbaar voor de bot: BESCHIKBARE EUR, begrensd door het live-plafond.

        Sinds v0.25.0 zonder `inOrder` (N8): EUR die vastzit in een open buy-order
        is wel van jou maar niet uit te geven, en telde mee waardoor Bitvavo de
        order weigerde op onvoldoende saldo.
        """
        try:
            eur = float(self.client.get_balances(available_only=True).get("EUR", 0.0))
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
        return sum(t.pnl_eur for t in self._live_trades("sell") if t.ts.date() == today)

    def last_trade_at(self, market: str) -> datetime | None:
        rows = [t for t in self._live_trades() if t.market == market]
        return max(t.ts for t in rows) if rows else None

    @staticmethod
    def _position_row(s, market: str) -> PositionRow | None:
        return s.execute(select(PositionRow).where(PositionRow.market == market,
                                                   PositionRow.mode == MODE)
                         ).scalar_one_or_none()

    def _has_position(self, market: str) -> bool:
        with session() as s:
            return self._position_row(s, market) is not None

    # --- order-administratie (outbox) -----------------------------------------
    def _new_order(self, purpose: str, market: str, side: str, order_type: str,
                   amount, price=0, trigger=0, meta: dict | None = None) -> str:
        cid = str(uuid.uuid4())
        with session() as s:
            s.add(OrderRow(market=market, mode=MODE, purpose=purpose, side=side,
                           order_type=order_type, client_order_id=cid, status="pending",
                           amount=float(amount), price=float(price), trigger=float(trigger),
                           meta=dict(meta or {})))
            s.commit()
        return cid

    @staticmethod
    def _fills(resp: dict, info: MarketInfo) -> tuple[float, float, float, float]:
        """(gevuld in base, gevuld in quote, fee in EUR, fee in base) uit een orderantwoord."""
        fills = resp.get("fills") or []
        if resp.get("filledAmount") is not None:
            filled = float(resp.get("filledAmount") or 0)
            quote = float(resp.get("filledAmountQuote") or 0)
        else:
            filled = sum(float(f["amount"]) for f in fills)
            quote = sum(float(f["amount"]) * float(f["price"]) for f in fills)
        if resp.get("feePaid") is not None:
            fee = float(resp.get("feePaid") or 0)
        else:
            fee = sum(float(f.get("fee", 0)) for f in fills)
        currency = resp.get("feeCurrency") or info.quote or "EUR"
        if info.base and currency == info.base:
            avg = quote / filled if filled > EPS else 0.0
            return filled, quote, fee * avg, fee
        return filled, quote, fee, 0.0

    def _update_order(self, cid: str, resp: dict | None = None, info: MarketInfo | None = None,
                      status: str | None = None, error: str | None = None) -> None:
        with session() as s:
            row = s.execute(select(OrderRow).where(OrderRow.client_order_id == cid)
                            ).scalar_one()
            if resp:
                row.exchange_order_id = str(resp.get("orderId") or row.exchange_order_id)
                row.status = str(resp.get("status") or row.status)
                if info is not None:
                    filled, quote, fee_eur, fee_base = self._fills(resp, info)
                    # Nooit terug: een later antwoord zonder fills mag een eerder
                    # gezien fill-bedrag niet wissen.
                    if filled >= row.filled_amount:
                        row.filled_amount, row.filled_quote, row.fee_eur = filled, quote, fee_eur
                        row.meta = {**(row.meta or {}), "fee_base": fee_base}
            if status:
                row.status = status
            if error:
                row.error = error[:300]
            s.commit()

    def _resolve_after_error(self, cid: str, market: str, info: MarketInfo,
                             exc: Exception) -> dict:
        """Na een timeout of serverfout: bestaat de order? Opvragen, niet opnieuw plaatsen."""
        time.sleep(self.poll_interval_s)
        try:
            resp = self.client.get_order(market, client_order_id=cid)
        except BitvavoApiError as lookup:
            if lookup.definitive:
                self._update_order(cid, status="rejected",
                                   error=f"niet geplaatst na {type(exc).__name__}: {lookup}")
                raise OrderRejectedError(f"order {cid} niet geplaatst ({exc})") from exc
            self._update_order(cid, status="unknown", error=str(exc))
            raise OrderStateUnknownError(
                f"order {cid} op {market}: status onbekend ({exc})") from exc
        except httpx.HTTPError as lookup:
            self._update_order(cid, status="unknown", error=str(exc))
            raise OrderStateUnknownError(
                f"order {cid} op {market}: status onbekend ({exc}; {lookup})") from exc
        log.warning("live: order %s bestond na %s toch: status %s",
                    cid, type(exc).__name__, resp.get("status"))
        return resp

    def _submit(self, cid: str, market: str, body: dict, info: MarketInfo) -> dict:
        try:
            resp = self.client.create_order({**body, "clientOrderId": cid})
        except BitvavoApiError as exc:
            if exc.definitive:
                self._update_order(cid, status="rejected", error=str(exc))
                raise OrderRejectedError(str(exc)) from exc
            resp = self._resolve_after_error(cid, market, info, exc)
        except httpx.HTTPError as exc:
            resp = self._resolve_after_error(cid, market, info, exc)
        self._update_order(cid, resp, info)
        return resp

    def _poll(self, market: str, cid: str, info: MarketInfo, timeout_s: float,
              until: frozenset = FINAL_STATUSES) -> dict:
        deadline = time.monotonic() + timeout_s
        resp: dict = {}
        while True:
            resp = self.client.get_order(market, client_order_id=cid)
            self._update_order(cid, resp, info)
            if resp.get("status") in until or time.monotonic() >= deadline:
                return resp
            time.sleep(self.poll_interval_s)

    # --- boeken (idempotent via booked_amount) -------------------------------
    def _book_entry(self, cid: str, info: MarketInfo) -> float:
        with session() as s:
            order = s.execute(select(OrderRow).where(OrderRow.client_order_id == cid)
                              ).scalar_one()
            unbooked = order.filled_amount - order.booked_amount
            if unbooked <= EPS:
                return 0.0
            share = unbooked / order.filled_amount
            quote = order.filled_quote * share
            fee = order.fee_eur * share
            fee_base = float((order.meta or {}).get("fee_base", 0.0)) * share
            amount = unbooked - fee_base
            avg = quote / unbooked
            meta = order.meta or {}
            pos = self._position_row(s, order.market)
            if pos is None:
                s.add(PositionRow(market=order.market, mode=MODE, amount=amount, entry_price=avg,
                                  stop_loss=float(meta.get("stop_loss", 0.0)),
                                  take_profit=float(meta.get("take_profit", 0.0)),
                                  fees_paid_eur=fee))
            else:  # alleen via reconcile: een tweede boeking op dezelfde entry
                totaal = pos.amount + amount
                pos.entry_price = (pos.amount * pos.entry_price + amount * avg) / totaal
                pos.amount = totaal
                pos.fees_paid_eur += fee
            s.add(TradeRow(market=order.market, side="buy", amount=amount, price=avg,
                           fee_eur=fee, mode=MODE, reason=str(meta.get("reason", ""))[:500]))
            order.booked_amount = order.filled_amount
            s.commit()
        log.info("LIVE BUY %s: %.8f @ %.6g (fee %.4f EUR)", order.market, amount, avg, fee)
        return unbooked

    def _book_exit(self, cid: str, info: MarketInfo, reason: str) -> float:
        """Boek het nog niet geboekte deel van de fills van een exit- of stoporder."""
        with session() as s:
            order = s.execute(select(OrderRow).where(OrderRow.client_order_id == cid)
                              ).scalar_one()
            unbooked = order.filled_amount - order.booked_amount
            if unbooked <= EPS:
                return 0.0
            pos = self._position_row(s, order.market)
            order.booked_amount = order.filled_amount
            if pos is None:
                s.commit()
                log.error("live: fills van %s (%s) zonder open positie in %s",
                          cid, order.purpose, order.market)
                return 0.0
            share = unbooked / order.filled_amount
            quote = order.filled_quote * share
            fee = order.fee_eur * share
            price = quote / unbooked
            sold = min(unbooked, pos.amount)
            frac = sold / pos.amount if pos.amount > EPS else 1.0
            cost = frac * (pos.amount * pos.entry_price + pos.fees_paid_eur)
            s.add(TradeRow(market=order.market, side="sell", amount=sold, price=price,
                           fee_eur=fee, pnl_eur=quote - fee - cost, mode=MODE,
                           reason=reason[:500]))
            rest = pos.amount - sold
            if rest <= EPS or info.below_minimum(info.amount(rest), price):
                if rest > EPS:
                    self._book_dust(s, pos, rest, price, frac)
                s.delete(pos)
            else:
                pos.amount = rest
                pos.fees_paid_eur *= (1 - frac)
            s.commit()
        log.info("LIVE SELL %s: %.8f @ %.6g (fee %.4f EUR, %s)",
                 order.market, sold, price, fee, reason)
        return sold

    @staticmethod
    def _book_dust(s, pos: PositionRow, rest: float, price: float, frac_sold: float) -> None:
        """Restant onder het marktminimum: blijft als stof op de exchange staan en
        wordt tegen de koers afgeboekt, zodat de P&L de kostprijs niet verliest."""
        cost = rest * pos.entry_price + pos.fees_paid_eur * (1 - frac_sold)
        s.add(TradeRow(market=pos.market, side="sell", amount=rest, price=price, fee_eur=0.0,
                       pnl_eur=rest * price - cost, mode=MODE,
                       reason="stof: restant onder het marktminimum, blijft op de exchange"))
        log.warning("live: %s restant %.10f onder het minimum afgeboekt als stof",
                    pos.market, rest)

    # --- stop-loss op de exchange (L2) ---------------------------------------
    def _place_stop(self, market: str, info: MarketInfo) -> str:
        with session() as s:
            pos = self._position_row(s, market)
            if pos is None:
                return ""
            amount = info.amount(pos.amount)
            trigger = info.price(pos.stop_loss)
        if info.below_minimum(amount, float(trigger)):
            log.warning("live: %s te klein voor een stop op de exchange", market)
            return ""
        cid = self._new_order("stop", market, "sell", "stopLoss", amount, trigger=trigger)
        try:
            self._submit(cid, market, {
                "market": market, "side": "sell", "orderType": "stopLoss",
                "amount": fmt_decimal(amount), "triggerType": "price",
                "triggerReference": self.stop_reference, "triggerAmount": fmt_decimal(trigger),
            }, info)
        except OrderStateUnknownError:
            # Misschien staat hij er wel. Toch koppelen: dan annuleert de volgende
            # exit hem op id in plaats van tegen gereserveerde coins aan te lopen,
            # en ziet `_protect` na een opvraag of hij echt bestaat.
            self._link_stop(market, cid)
            raise
        self._link_stop(market, cid)
        return cid

    def _link_stop(self, market: str, cid: str) -> None:
        with session() as s:
            pos = self._position_row(s, market)
            if pos is not None:
                pos.stop_order_cid = cid
                s.commit()

    def _get_stop(self, market: str, cid: str, info: MarketInfo, poll: bool) -> dict | None:
        """Stand van een stop; None als Bitvavo hem niet kent (nooit geplaatst)."""
        try:
            if poll:
                return self._poll(market, cid, info, self.exit_timeout_s)
            resp = self.client.get_order(market, client_order_id=cid)
        except BitvavoApiError as exc:
            if not exc.definitive:
                raise
            self._update_order(cid, status="rejected", error=f"onvindbaar bij Bitvavo: {exc}")
            self._link_stop_clear(market, cid)
            return None
        self._update_order(cid, resp, info)
        return resp

    def _link_stop_clear(self, market: str, cid: str) -> None:
        with session() as s:
            pos = self._position_row(s, market)
            if pos is not None and pos.stop_order_cid == cid:
                pos.stop_order_cid = ""
                s.commit()

    def _place_stop_safe(self, market: str, info: MarketInfo) -> bool:
        try:
            return bool(self._place_stop(market, info))
        except Exception:  # noqa: BLE001 - de guard dekt de stop zolang het proces draait
            log.exception("live: stop plaatsen mislukt voor %s", market)
            return False

    def _retire_stop(self, market: str, info: MarketInfo) -> None:
        """Annuleer de stop van een positie en boek wat hij al verkocht had.

        Volgorde telt: eerst annuleren, dan de eindstand opvragen. Faalt het
        annuleren omdat de stop net vuurde, dan zegt de eindstand dat en worden
        die fills geboekt in plaats van nog eens dezelfde coins te verkopen.
        """
        with session() as s:
            pos = self._position_row(s, market)
            cid = pos.stop_order_cid if pos is not None else ""
        if not cid:
            return
        try:
            self.client.cancel_order(market, client_order_id=cid)
        except BitvavoApiError as exc:
            log.info("live: stop %s niet te annuleren (%s); eindstand opvragen", cid, exc)
        resp = self._get_stop(market, cid, info, poll=True)
        if resp is None:
            return   # stop bestond nooit; koppeling is gewist
        if resp.get("status") not in FINAL_STATUSES:
            raise OrderStateUnknownError(f"stop {cid} op {market} niet final na annuleren: "
                                    f"{resp.get('status')}")
        self._book_exit(cid, info, "exchange-stop geraakt")
        self._link_stop_clear(market, cid)

    # --- execution -----------------------------------------------------------
    def buy(self, market: str, amount_quote_eur: float, stop_loss: float,
            take_profit: float, reason: str) -> None:
        with MARKET_LOCKS.hold(market):
            self._buy(market, amount_quote_eur, stop_loss, take_profit, reason)

    def _buy(self, market: str, amount_quote_eur: float, stop_loss: float,
             take_profit: float, reason: str) -> None:
        if self._has_position(market):
            raise ValueError(f"live-positie in {market} staat al open")
        if self.exposure_eur() + amount_quote_eur > self.max_capital + 0.01:
            raise ValueError(
                f"live exposure-plafond: {self.exposure_eur():.2f} + {amount_quote_eur:.2f} "
                f"> {self.max_capital:.2f} EUR")
        info = self.client.get_market_info(market)
        if info.status and info.status != "trading":
            raise OrderRejectedError(f"markt {market} staat op '{info.status}', geen entry")
        book = self.client.get_book_ticker(market)
        price = info.price(float(book["bid"]))
        # Ruimte voor de maker-fee laten: Bitvavo zet bedrag + fee on hold.
        amount = info.amount(amount_quote_eur / float(price) * (1 - self.fees.maker_pct / 100))
        te_klein = info.below_minimum(amount, float(price))
        if te_klein:
            raise OrderRejectedError(f"entry {market} onder het marktminimum: {te_klein}")
        cid = self._new_order("entry", market, "buy", "limit", amount, price, meta={
            "stop_loss": stop_loss, "take_profit": take_profit, "reason": reason[:300]})
        resp = self._submit(cid, market, {
            "market": market, "side": "buy", "orderType": "limit",
            "amount": fmt_decimal(amount), "price": fmt_decimal(price), "postOnly": True,
        }, info)
        if resp.get("status") not in FINAL_STATUSES:
            resp = self._poll(market, cid, info, self.entry_timeout_s)
        if resp.get("status") not in FINAL_STATUSES:
            try:
                self.client.cancel_order(market, client_order_id=cid)
            except BitvavoApiError as exc:   # kan net gevuld zijn
                log.warning("live: cancel van entry %s faalde (%s)", cid, exc)
            resp = self._poll(market, cid, info, self.exit_timeout_s)
        if self._book_entry(cid, info) <= EPS:
            raise TimeoutError(f"live entry {market} niet gevuld binnen "
                               f"{self.entry_timeout_s:.0f}s; order {resp.get('status')}")
        if not self._place_stop_safe(market, info):
            log.error("live: %s staat open ZONDER stop op de exchange; "
                      "ensure_protection probeert het elke guard-run opnieuw", market)

    def sell(self, market: str, reason: str) -> None:
        with MARKET_LOCKS.hold(market):
            self._sell(market, reason)

    def _sell(self, market: str, reason: str) -> None:
        if not self._has_position(market):
            raise PositionGone(f"geen open live-positie in {market}")
        info = self.client.get_market_info(market)
        self._retire_stop(market, info)
        with session() as s:
            pos = self._position_row(s, market)
            if pos is None:
                return   # de stop had alles al verkocht
            amount = info.amount(pos.amount)
        price_hint = float(self.client.get_price(market))
        te_klein = info.below_minimum(amount, price_hint)
        if te_klein:
            with session() as s:
                pos = self._position_row(s, market)
                self._book_dust(s, pos, pos.amount, price_hint, 0.0)
                s.delete(pos)
                s.commit()
            return
        cid = self._new_order("exit", market, "sell", "market", amount,
                              meta={"reason": reason[:300]})
        sold = 0.0
        try:
            resp = self._submit(cid, market, {"market": market, "side": "sell",
                                              "orderType": "market",
                                              "amount": fmt_decimal(amount)}, info)
            if resp.get("status") not in FINAL_STATUSES:
                resp = self._poll(market, cid, info, self.exit_timeout_s)
            sold = self._book_exit(cid, info, reason)
        finally:
            # Wat (deels) open blijft, krijgt meteen weer een stop: de oude is
            # hierboven geannuleerd en een mislukte exit mag geen onbeschermde
            # positie achterlaten.
            if self._has_position(market):
                self._place_stop_safe(market, info)
        if sold <= EPS:
            raise ExitNotFilledError(
                f"exit {market}: niets verkocht (status {resp.get('status')}); "
                "positie staat open met een nieuwe stop")
        if self._has_position(market):
            raise PartialExitError(f"exit {market}: {sold:.8f} van {amount} verkocht; "
                              "rest staat open met een nieuwe stop")

    # --- bewaking ------------------------------------------------------------
    def ensure_protection(self) -> list[dict]:
        """Elke guard-run: staat er voor elke positie een stop, en vuurde er een?

        Een stop die op de exchange vuurde terwijl de bot niets deed (of plat lag)
        wordt hier gezien en geboekt. Een positie zonder stop krijgt er een. Geeft
        events terug voor de engine: `{"market", "sold"}` of `{"market", "error"}`.
        """
        events: list[dict] = []
        for pos in self.open_positions():
            try:
                with MARKET_LOCKS.hold(pos.market, timeout=5):
                    events.extend(self._protect(pos.market))
            except MarketBusy:
                continue   # er loopt een order in deze markt; volgende minuut
            except Exception as exc:  # noqa: BLE001 - één markt mag de rest niet blokkeren
                log.exception("live: bescherming controleren mislukt voor %s", pos.market)
                events.append({"market": pos.market, "error": f"{type(exc).__name__}: {exc}"})
        return events

    def _protect(self, market: str) -> list[dict]:
        out: list[dict] = []
        with session() as s:
            pos = self._position_row(s, market)
            cid = pos.stop_order_cid if pos is not None else ""
        if pos is None:
            return out
        info = self.client.get_market_info(market)
        if cid:
            resp = self._get_stop(market, cid, info, poll=False)
            if resp is not None:
                sold = self._book_exit(cid, info, "exchange-stop geraakt")
                if sold > EPS:
                    out.append({"market": market, "sold": sold})
                if not self._has_position(market):
                    return out
                if resp.get("status") in ACTIVE_STOP_STATUSES:
                    return out
                self._link_stop_clear(market, cid)   # final, maar er staat nog iets open
        if not self._place_stop_safe(market, info):
            out.append({"market": market, "error": "stop op de exchange plaatsen mislukt"})
        return out

    def reconcile(self) -> tuple[list[str], list[str]]:
        """Administratie tegen de exchange leggen (L4). Geeft (problemen, notities).

        1. Elke entry- of exit-order die niet final is, of final maar met fills die
           nog niet geboekt zijn (crash tussen fill en boeking), wordt afgemaakt:
           een gevulde entry zonder positie wordt alsnog geboekt (SL/TP uit `meta`),
           een exit met ongeboekte fills ook. Een entry die te lang open staat wordt
           geannuleerd. Onvindbaar bij Bitvavo = nooit geplaatst.
        2. Per positie moet het saldo van die coin minstens de geadministreerde
           hoeveelheid zijn. Alleen een TEKORT is een probleem: deelt de bot het
           account met eigen coins van de gebruiker, dan is een overschot normaal.
        """
        problems: list[str] = []
        notes: list[str] = []
        with session() as s:
            open_orders = s.execute(select(OrderRow).where(
                OrderRow.mode == MODE, OrderRow.purpose.in_(("entry", "exit")),
                or_(OrderRow.status.not_in(tuple(FINAL_STATUSES)),
                    OrderRow.filled_amount > OrderRow.booked_amount + EPS))).scalars().all()
        for order in open_orders:
            try:
                with MARKET_LOCKS.hold(order.market, timeout=30):
                    note = self._finish_order(order.market, order.client_order_id, order.purpose,
                                              order.ts)
                if note:
                    notes.append(note)
            except Exception as exc:  # noqa: BLE001 - verzamelen, niet stoppen
                problems.append(f"order {order.client_order_id} ({order.purpose} "
                                f"{order.market}) niet af te maken: {exc}")
        try:
            balances = self.client.get_balances()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"saldi niet op te halen: {exc}")
            return problems, notes
        per_coin: dict[str, float] = {}
        for p in self.open_positions():
            per_coin[_base(p.market)] = per_coin.get(_base(p.market), 0.0) + p.amount
        for coin, amount in per_coin.items():
            held = float(balances.get(coin, 0.0))
            if held + 1e-8 < amount * (1 - 1e-6):
                problems.append(f"tekort {coin}: exchange {held:.8f} < administratie {amount:.8f}")
        return problems, notes

    def _finish_order(self, market: str, cid: str, purpose: str, created) -> str | None:
        info = self.client.get_market_info(market)
        try:
            resp = self.client.get_order(market, client_order_id=cid)
        except BitvavoApiError as exc:
            if exc.definitive:
                self._update_order(cid, status="rejected", error=f"onvindbaar bij Bitvavo: {exc}")
                return f"order {cid} ({purpose} {market}) bleek nooit geplaatst"
            raise
        self._update_order(cid, resp, info)
        if purpose == "entry" and resp.get("status") not in FINAL_STATUSES:
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age < 2 * self.entry_timeout_s:
                return None   # loopt nog in een andere thread
            try:
                self.client.cancel_order(market, client_order_id=cid)
            except BitvavoApiError as exc:
                log.warning("live: cancel van verweesde entry %s faalde (%s)", cid, exc)
            resp = self._poll(market, cid, info, self.exit_timeout_s)
        if purpose == "entry":
            booked = self._book_entry(cid, info)
            if booked > EPS:
                if not self._place_stop_safe(market, info):
                    log.error("live: alsnog geboekte entry %s heeft geen stop", market)
                return f"entry {market} alsnog geboekt: {booked:.8f} (order {cid})"
            return None
        sold = self._book_exit(cid, info, "exit alsnog geboekt bij reconciliatie")
        return f"exit {market} alsnog geboekt: {sold:.8f} (order {cid})" if sold > EPS else None
