"""Live broker: echte Bitvavo-orders. Fase 3-code, dubbel vergrendeld: activering
vereist TRADING_MODE=live én de letterlijke bevestigingszin (engine.LIVE_CONFIRM_PHRASE).

Ontwerpkeuzes (sinds v0.11.0):
- Entries: limit postOnly (maker) op de beste bied. Niet gevuld binnen de timeout =
  annuleren en overslaan; een gemiste entry kost niets.
- Exits: market order (taker). Een gemiste exit kost kapitaal; snelheid wint.
- Exposure hard begrensd op `live_max_capital_eur`, los van wat er op de rekening staat.

Herbouwd in v0.25.0 rond de live-blockers uit de reviews:

- L1/N2 Alleen BEVESTIGDE fills worden geboekt, idempotent via `booked_amount`.
- L4/N4 Elke order staat eerst als `OrderRow` met een eigen `clientOrderId` in de
  database (outbox) en gaat pas daarna naar Bitvavo.
- L2 Na elke entry staat er een `stopLoss`-order op de exchange. Elke exit annuleert
  eerst de stop, boekt wat de stop al verkocht had en verkoopt dan pas de rest.
- N3 Hoeveelheden en prijzen volgen de regels van de markt.

Drie invarianten, aangescherpt na een onafhankelijke review van het orderpad:

1. Een order is pas "bestaat niet" als dat bevestigd is: Bitvavo meldt errorCode 240,
   hij staat niet tussen de open orders, en hij is minstens `ghost_age_s` oud. Een
   403 (buiten het toegangsvenster), een 503 ("may or may not have succeeded") of
   een 404 vlak na een timeout bewijzen niets; zo'n order blijft `unknown`.
2. Zolang er in een markt een entry of exit openstaat met onbekende of lopende status,
   gaat er in die markt GEEN nieuwe exit, entry of stop de deur uit. Het account kan
   coins van de gebruiker zelf bevatten; een tweede verkoop of stop op basis van een
   verkeerde aanname verkoopt dan zijn coins.
3. Per positie hooguit één stop, en die is gekoppeld VOORDAT hij wordt verstuurd.
   Een stop die toch zonder koppeling op de exchange staat (oude crash) wordt door
   `reconcile` geannuleerd en afgeboekt.

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
GHOST_AGE_S = 300.0


class OrderRejectedError(RuntimeError):
    """Bitvavo weigerde de order definitief: hij is zeker niet geplaatst."""


class OrderStateUnknownError(RuntimeError):
    """Niet vast te stellen of en hoe de order bestaat; er gebeurt niets tot dat duidelijk is."""


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
                 exit_timeout_s: float = 15.0, stop_trigger_reference: str = "lastTrade",
                 ghost_age_s: float = GHOST_AGE_S):
        self.client = client
        self.fees = fee_model
        self.max_capital = float(max_capital_eur)
        self.entry_timeout_s = entry_timeout_s
        self.poll_interval_s = poll_interval_s
        self.exit_timeout_s = exit_timeout_s
        self.stop_reference = stop_trigger_reference
        self.ghost_age_s = ghost_age_s

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
        is wel van jou maar niet uit te geven.
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

    def _linked_stop(self, market: str) -> str:
        with session() as s:
            pos = self._position_row(s, market)
            return (pos.stop_order_cid or "") if pos is not None else ""

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
    def _order(cid: str) -> OrderRow:
        with session() as s:
            return s.execute(select(OrderRow).where(OrderRow.client_order_id == cid)
                             ).scalar_one()

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

    def _submit(self, cid: str, market: str, body: dict, info: MarketInfo) -> dict:
        """Verstuur een order die al in het orderboek staat.

        Alleen een definitieve weigering OP DE PLAATSING (4xx behalve 429) of een
        verbinding die nooit tot stand kwam, betekent "niet geplaatst". Bij al het
        andere (timeout, 5xx, rate limit) wordt de order opgevraagd; lukt dat niet,
        dan blijft hij `unknown` (invariant 1).
        """
        try:
            resp = self.client.create_order({**body, "clientOrderId": cid})
        except BitvavoApiError as exc:
            if exc.definitive:
                self._update_order(cid, status="rejected", error=str(exc))
                raise OrderRejectedError(str(exc)) from exc
            resp = self._lookup_after_error(cid, market, info, exc)
        except httpx.ConnectError as exc:   # verzoek is de deur niet uitgegaan
            self._update_order(cid, status="rejected", error=f"niet verzonden: {exc}")
            raise OrderRejectedError(f"order {cid} niet verzonden ({exc})") from exc
        except httpx.HTTPError as exc:
            resp = self._lookup_after_error(cid, market, info, exc)
        self._update_order(cid, resp, info)
        return resp

    def _lookup_after_error(self, cid: str, market: str, info: MarketInfo,
                            exc: Exception) -> dict:
        time.sleep(self.poll_interval_s)
        try:
            resp = self.client.get_order(market, client_order_id=cid)
        except httpx.HTTPError as lookup:   # ook 240: kan nog onderweg zijn
            self._update_order(cid, status="unknown",
                               error=f"{type(exc).__name__}: {exc} | opvraag: {lookup}")
            raise OrderStateUnknownError(
                f"order {cid} op {market}: status onbekend ({exc})") from exc
        log.warning("live: order %s bestond na %s: status %s",
                    cid, type(exc).__name__, resp.get("status"))
        return resp

    def _poll(self, market: str, cid: str, info: MarketInfo, timeout_s: float,
              until: frozenset = FINAL_STATUSES) -> dict:
        """Pollen tot een eindstatus. Een netwerkfout wordt OrderStateUnknownError:
        de order staat er, alleen zijn stand is even niet te zien."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                resp = self.client.get_order(market, client_order_id=cid)
            except httpx.HTTPError as exc:
                raise OrderStateUnknownError(f"order {cid} op {market}: opvragen faalde "
                                             f"({exc})") from exc
            self._update_order(cid, resp, info)
            if resp.get("status") in until or time.monotonic() >= deadline:
                return resp
            time.sleep(self.poll_interval_s)

    def _confirm_missing(self, market: str, cid: str) -> bool:
        """Invariant 1: alleen errorCode 240, niet bij de open orders, en oud genoeg."""
        order = self._order(cid)
        age = (datetime.now(timezone.utc) - order.ts).total_seconds()
        if age < self.ghost_age_s:
            return False
        try:
            self.client.get_order(market, client_order_id=cid)
            return False
        except BitvavoApiError as exc:
            if not exc.not_found:
                return False
        except httpx.HTTPError:
            return False
        try:
            open_ids = {o.get("clientOrderId") for o in self.client.get_open_orders(market)}
        except httpx.HTTPError:
            return False
        if cid in open_ids:
            return False
        self._update_order(cid, status="rejected",
                           error=f"bevestigd onvindbaar na {age:.0f}s (240, niet open)")
        return True

    # --- openstaande orders per markt (invariant 2) ----------------------------
    def _unsettled(self, market: str) -> list[OrderRow]:
        with session() as s:
            return list(s.execute(select(OrderRow).where(
                OrderRow.mode == MODE, OrderRow.market == market,
                OrderRow.purpose.in_(("entry", "exit")),
                or_(OrderRow.status.not_in(tuple(FINAL_STATUSES)),
                    OrderRow.filled_amount > OrderRow.booked_amount + EPS))).scalars().all())

    def _settle(self, market: str, info: MarketInfo) -> list[str]:
        """Probeer elke openstaande entry of exit in deze markt af te maken.

        Geeft notities terug; gooit OrderStateUnknownError als er daarna nog iets
        openstaat. Aanroepers plaatsen dan niets nieuws in deze markt.
        """
        notes: list[str] = []
        for order in self._unsettled(market):
            note = self._finish_order(market, order.client_order_id, order.purpose, info)
            if note:
                notes.append(note)
        rest = self._unsettled(market)
        if rest:
            beschrijving = ", ".join(f"{o.purpose} {o.client_order_id} ({o.status})"
                                     for o in rest)
            raise OrderStateUnknownError(f"{market}: nog niet afgeronde order(s): "
                                         f"{beschrijving}; geen nieuwe order in deze markt")
        return notes

    def _finish_order(self, market: str, cid: str, purpose: str,
                      info: MarketInfo) -> str | None:
        try:
            resp = self.client.get_order(market, client_order_id=cid)
        except BitvavoApiError as exc:
            if exc.not_found and self._confirm_missing(market, cid):
                return f"order {cid} ({purpose} {market}) bleek nooit geplaatst"
            return None
        except httpx.HTTPError:
            return None
        self._update_order(cid, resp, info)
        if purpose == "entry" and resp.get("status") not in FINAL_STATUSES:
            age = (datetime.now(timezone.utc) - self._order(cid).ts).total_seconds()
            if age >= self.entry_timeout_s:
                try:
                    self.client.cancel_order(market, client_order_id=cid)
                except BitvavoApiError as exc:
                    log.warning("live: cancel van verweesde entry %s faalde (%s)", cid, exc)
                try:
                    self._poll(market, cid, info, self.exit_timeout_s)
                except OrderStateUnknownError:
                    pass
        if purpose == "entry":
            booked = self._book_entry(cid, info)
            if booked > EPS:
                # Er kan al een (kleinere) stop staan: eerst die weg, dan één nieuwe
                # voor de hele positie (invariant 3).
                self._retire_stop(market, info)
                if not self._place_stop_safe(market, info):
                    log.error("live: alsnog geboekte entry %s heeft geen stop", market)
                return f"entry {market} alsnog geboekt: {booked:.8f} (order {cid})"
            return None
        sold = self._book_exit(cid, info, "exit afgerond na onbekende status")
        return f"exit {market} alsnog geboekt: {sold:.8f} (order {cid})" if sold > EPS else None

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
                # Openingstijd = tijd van de order, niet van de boeking: een entry die
                # pas bij de reconciliatie wordt geboekt, liep anders tot een uur achter
                # in de time-stop en de breakeven-stop.
                s.add(PositionRow(market=order.market, mode=MODE, amount=amount, entry_price=avg,
                                  stop_loss=float(meta.get("stop_loss", 0.0)),
                                  take_profit=float(meta.get("take_profit", 0.0)),
                                  fees_paid_eur=fee, opened_at=order.ts))
            else:  # late fills op dezelfde entry
                totaal = pos.amount + amount
                pos.entry_price = (pos.amount * pos.entry_price + amount * avg) / totaal
                pos.amount = totaal
                pos.fees_paid_eur += fee
            s.add(TradeRow(market=order.market, side="buy", amount=amount, price=avg,
                           fee_eur=fee, mode=MODE, ts=order.ts,
                           reason=str(meta.get("reason", ""))[:500]))
            order.booked_amount = order.filled_amount
            market = order.market
            s.commit()
        log.info("LIVE BUY %s: %.8f @ %.6g (fee %.4f EUR)", market, amount, avg, fee)
        return unbooked

    def _book_exit(self, cid: str, info: MarketInfo, reason: str) -> float:
        """Boek het nog niet geboekte deel van de fills van een exit- of stoporder.

        Een restant onder het marktminimum wordt pas als stof afgeboekt als deze
        order final is: een stop die nog loopt, kan het restant zelf nog verkopen.
        """
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
            final = order.status in FINAL_STATUSES
            if rest <= EPS:
                s.delete(pos)
            elif final and info.below_minimum(info.amount(rest), price):
                self._book_dust(s, pos, rest, price, frac)
                s.delete(pos)
            else:
                pos.amount = rest
                pos.fees_paid_eur *= (1 - frac)
            market = order.market
            s.commit()
        log.info("LIVE SELL %s: %.8f @ %.6g (fee %.4f EUR, %s)",
                 market, sold, price, fee, reason)
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

    def _write_off_if_dust(self, market: str, info: MarketInfo, price: float) -> bool:
        with session() as s:
            pos = self._position_row(s, market)
            if pos is None or pos.stop_order_cid:
                return False
            if not info.below_minimum(info.amount(pos.amount), price):
                return False
            self._book_dust(s, pos, pos.amount, price, 0.0)
            s.delete(pos)
            s.commit()
        return True

    # --- stop-loss op de exchange (L2, invariant 3) ---------------------------
    def _place_stop(self, market: str, info: MarketInfo) -> str:
        """Plaats DE stop van een positie. Leeg resultaat = bewust geen stop
        (te klein, of de koers staat al onder het niveau: dan verkoopt de guard)."""
        with session() as s:
            pos = self._position_row(s, market)
            if pos is None:
                return ""
            if pos.stop_order_cid:
                raise RuntimeError(f"{market} heeft al een gekoppelde stop "
                                   f"{pos.stop_order_cid}; eerst afhandelen")
            amount = info.amount(pos.amount)
            trigger = info.price(pos.stop_loss)
        if info.below_minimum(amount, float(trigger)):
            log.warning("live: %s te klein voor een stop op de exchange", market)
            return ""
        if float(self.client.get_price(market)) <= float(trigger):
            log.warning("live: %s staat al onder het stopniveau; geen stop, de guard verkoopt",
                        market)
            return ""
        cid = self._new_order("stop", market, "sell", "stopLoss", amount, trigger=trigger)
        self._link_stop(market, cid)          # koppelen VOOR versturen
        try:
            self._submit(cid, market, {
                "market": market, "side": "sell", "orderType": "stopLoss",
                "amount": fmt_decimal(amount), "triggerType": "price",
                "triggerReference": self.stop_reference, "triggerAmount": fmt_decimal(trigger),
            }, info)
        except OrderRejectedError:
            self._link_stop_clear(market, cid)  # zeker niet geplaatst
            raise
        return cid

    def _link_stop(self, market: str, cid: str) -> None:
        with session() as s:
            pos = self._position_row(s, market)
            if pos is not None:
                pos.stop_order_cid = cid
                s.commit()

    def _link_stop_clear(self, market: str, cid: str) -> None:
        with session() as s:
            pos = self._position_row(s, market)
            if pos is not None and pos.stop_order_cid == cid:
                pos.stop_order_cid = ""
                s.commit()

    def _place_stop_safe(self, market: str, info: MarketInfo) -> bool:
        """True als er niets misging (ook als er bewust geen stop kwam)."""
        try:
            self._place_stop(market, info)
            return True
        except Exception:  # noqa: BLE001 - de guard dekt de stop zolang het proces draait
            log.exception("live: stop plaatsen mislukt voor %s", market)
            return False

    def _stop_state(self, market: str, cid: str, info: MarketInfo) -> dict | None:
        """Stand van een gekoppelde stop. None alleen als bevestigd is dat hij niet
        bestaat (invariant 1); dan is de koppeling gewist. Elke andere fout gooit."""
        try:
            resp = self.client.get_order(market, client_order_id=cid)
        except BitvavoApiError as exc:
            if exc.not_found and self._confirm_missing(market, cid):
                self._link_stop_clear(market, cid)
                return None
            raise OrderStateUnknownError(f"stop {cid} op {market}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OrderStateUnknownError(f"stop {cid} op {market}: {exc}") from exc
        self._update_order(cid, resp, info)
        return resp

    def _retire_stop(self, market: str, info: MarketInfo) -> None:
        """Annuleer de stop van een positie en boek wat hij al verkocht had.

        Eerst annuleren, dan de eindstand opvragen. Faalt het annuleren omdat de
        stop net vuurde, dan zegt de eindstand dat en worden die fills geboekt in
        plaats van nog eens dezelfde coins te verkopen. Is de eindstand niet vast te
        stellen, dan gooit dit: de aanroeper verkoopt dan niets.
        """
        cid = self._linked_stop(market)
        if not cid:
            return
        try:
            self.client.cancel_order(market, client_order_id=cid)
        except httpx.HTTPError as exc:
            log.info("live: stop %s niet te annuleren (%s); eindstand opvragen", cid, exc)
        resp = self._stop_state(market, cid, info)
        if resp is None:
            return
        if resp.get("status") not in FINAL_STATUSES:
            resp = self._poll(market, cid, info, self.exit_timeout_s)
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
        info = self.client.get_market_info(market)
        self._settle(market, info)                       # invariant 2
        if self._has_position(market):
            raise ValueError(f"live-positie in {market} staat al open")
        if self.exposure_eur() + amount_quote_eur > self.max_capital + 0.01:
            raise ValueError(
                f"live exposure-plafond: {self.exposure_eur():.2f} + {amount_quote_eur:.2f} "
                f"> {self.max_capital:.2f} EUR")
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
        try:
            if resp.get("status") not in FINAL_STATUSES:
                resp = self._poll(market, cid, info, self.entry_timeout_s)
            if resp.get("status") not in FINAL_STATUSES:
                try:
                    self.client.cancel_order(market, client_order_id=cid)
                except httpx.HTTPError as exc:   # kan net gevuld zijn
                    log.warning("live: cancel van entry %s faalde (%s)", cid, exc)
                resp = self._poll(market, cid, info, self.exit_timeout_s)
        finally:
            # Wat tot hier gevuld is, hoort in de administratie en onder een stop,
            # ook als het pollen faalde. Late fills boekt de reconciliatie.
            if self._book_entry(cid, info) > EPS and not self._place_stop_safe(market, info):
                log.error("live: %s staat open ZONDER stop op de exchange; "
                          "ensure_protection probeert het elke guard-run opnieuw", market)
        if resp.get("status") not in FINAL_STATUSES:
            raise OrderStateUnknownError(f"entry {cid} op {market} nog actief na annuleren "
                                         f"({resp.get('status')})")
        if not self._has_position(market):
            raise TimeoutError(f"live entry {market} niet gevuld binnen "
                               f"{self.entry_timeout_s:.0f}s; order {resp.get('status')}")

    def sell(self, market: str, reason: str) -> None:
        with MARKET_LOCKS.hold(market):
            self._sell(market, reason)

    def _sell(self, market: str, reason: str) -> None:
        if not self._has_position(market):
            raise PositionGone(f"geen open live-positie in {market}")
        info = self.client.get_market_info(market)
        self._settle(market, info)                       # invariant 2
        if not self._has_position(market):
            return   # een eerder onbekende exit had alles al verkocht
        self._retire_stop(market, info)
        if not self._has_position(market):
            return   # de stop had alles al verkocht
        cid = ""
        sold = 0.0
        status = ""
        try:
            with session() as s:
                amount = info.amount(self._position_row(s, market).amount)
            price_hint = float(self.client.get_price(market))
            if self._write_off_if_dust(market, info, price_hint):
                return
            cid = self._new_order("exit", market, "sell", "market", amount,
                                  meta={"reason": reason[:300]})
            resp = self._submit(cid, market, {"market": market, "side": "sell",
                                              "orderType": "market",
                                              "amount": fmt_decimal(amount)}, info)
            if resp.get("status") not in FINAL_STATUSES:
                resp = self._poll(market, cid, info, self.exit_timeout_s)
            status = str(resp.get("status"))
            sold = self._book_exit(cid, info, reason)
        finally:
            # Wat (deels) open blijft, krijgt weer een stop, MAAR alleen als vaststaat
            # wat er verkocht is (invariant 2): bij een exit met onbekende stand kan een
            # nieuwe stop coins van de gebruiker zelf reserveren en verkopen.
            if self._has_position(market) and not self._unsettled(market):
                self._place_stop_safe(market, info)
        if sold <= EPS:
            raise ExitNotFilledError(f"exit {market}: niets verkocht (status {status}); "
                                     "positie staat open met een nieuwe stop")
        if self._has_position(market):
            raise PartialExitError(f"exit {market}: {sold:.8f} van {amount} verkocht; "
                                   "rest staat open met een nieuwe stop")

    # --- bewaking ------------------------------------------------------------
    def ensure_protection(self) -> list[dict]:
        """Elke guard-run: staat er voor elke positie een stop, en vuurde er een?

        Geeft events terug voor de engine: `{"market", "sold"}` of `{"market", "error"}`.
        """
        events: list[dict] = []
        for pos in self.open_positions():
            try:
                with MARKET_LOCKS.hold(pos.market, timeout=5):
                    events.extend(self._protect(pos.market))
            except MarketBusy:
                continue   # er loopt een order in deze markt; volgende minuut
            except Exception as exc:  # noqa: BLE001 - één markt mag de rest niet blokkeren
                log.warning("live: bescherming %s niet vast te stellen: %s", pos.market, exc)
                events.append({"market": pos.market, "error": f"{type(exc).__name__}: {exc}"})
        return events

    def _protect(self, market: str) -> list[dict]:
        out: list[dict] = []
        if not self._has_position(market):
            return out
        info = self.client.get_market_info(market)
        try:
            for note in self._settle(market, info):   # een onbekende exit binnen de minuut
                log.warning("live: %s", note)        # afmaken, niet pas bij de
        except OrderStateUnknownError as exc:        # volgende reconciliatie
            out.append({"market": market, "error": str(exc)})
        if not self._has_position(market):
            return out
        cid = self._linked_stop(market)
        if cid:
            resp = self._stop_state(market, cid, info)
            if resp is not None:
                sold = self._book_exit(cid, info, "exchange-stop geraakt")
                if sold > EPS:
                    out.append({"market": market, "sold": sold})
                if not self._has_position(market):
                    return out
                if resp.get("status") in ACTIVE_STOP_STATUSES:
                    return out
                self._link_stop_clear(market, cid)   # final, maar er staat nog iets open
        if self._unsettled(market):
            if not out:
                out.append({"market": market, "error": "order met onbekende stand; "
                            "nieuwe stop wacht tot die duidelijk is"})
            return out
        if self._write_off_if_dust(market, info, float(self.client.get_price(market))):
            return out
        if not self._place_stop_safe(market, info):
            out.append({"market": market, "error": "stop op de exchange plaatsen mislukt"})
        return out

    def reconcile(self) -> tuple[list[str], list[str]]:
        """Administratie tegen de exchange leggen (L4). Geeft (problemen, notities).

        Volgorde telt: eerst afmaken wat openstaat en gevuurde stops boeken, pas dan
        saldi vergelijken. Anders levert een stop die vuurde terwijl de bot plat lag
        bij elke herstart een vals tekort op.

        1. Per markt: openstaande entries en exits afmaken (een gevulde entry krijgt
           SL/TP uit de outbox-meta), losse stops annuleren en afboeken, en per
           positie de stop nalopen. Wat niet af te maken is, is een probleem.
        2. Per positie moet het saldo van die coin minstens de geadministreerde
           hoeveelheid zijn. Een overschot is normaal bij een gedeeld account.
        """
        problems: list[str] = []
        notes: list[str] = []
        with session() as s:
            markets = set(s.execute(select(OrderRow.market).where(
                OrderRow.mode == MODE,
                or_(OrderRow.status.not_in(tuple(FINAL_STATUSES)),
                    OrderRow.filled_amount > OrderRow.booked_amount + EPS))).scalars().all())
        markets |= {p.market for p in self.open_positions()}
        for market in sorted(markets):
            try:
                with MARKET_LOCKS.hold(market, timeout=30):
                    info = self.client.get_market_info(market)
                    notes.extend(self._settle(market, info))
                    notes.extend(self._sweep_orphan_stops(market, info))
                    for ev in self._protect(market):
                        if ev.get("error"):
                            problems.append(f"{market}: {ev['error']}")
                        else:
                            notes.append(f"{market}: exchange-stop geraakt, {ev['sold']:.8f} "
                                         "geboekt")
            except Exception as exc:  # noqa: BLE001 - verzamelen, niet stoppen
                problems.append(f"{market}: {exc}")
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

    def _sweep_orphan_stops(self, market: str, info: MarketInfo) -> list[str]:
        """Stops die niet (meer) aan een positie hangen: annuleren en afboeken.

        Ontstaan bij een crash tussen versturen en koppelen (oude code) of als een
        koppeling ooit onterecht werd gewist. Blijft er zo'n stop staan, dan vuren
        straks twee stops op dezelfde coins.
        """
        notes: list[str] = []
        linked = self._linked_stop(market)
        with session() as s:
            stops = s.execute(select(OrderRow).where(
                OrderRow.mode == MODE, OrderRow.market == market, OrderRow.purpose == "stop",
                or_(OrderRow.status.not_in(tuple(FINAL_STATUSES)),
                    OrderRow.filled_amount > OrderRow.booked_amount + EPS))).scalars().all()
        for stop in stops:
            cid = stop.client_order_id
            if cid == linked:
                continue
            try:
                self.client.cancel_order(market, client_order_id=cid)
            except httpx.HTTPError as exc:
                log.info("live: losse stop %s niet te annuleren (%s)", cid, exc)
            try:
                resp = self.client.get_order(market, client_order_id=cid)
            except BitvavoApiError as exc:
                if exc.not_found and self._confirm_missing(market, cid):
                    continue
                raise OrderStateUnknownError(f"losse stop {cid}: {exc}") from exc
            self._update_order(cid, resp, info)
            if resp.get("status") not in FINAL_STATUSES:
                raise OrderStateUnknownError(f"losse stop {cid} op {market} blijft actief")
            sold = self._book_exit(cid, info, "losse exchange-stop geraakt")
            notes.append(f"losse stop {cid} op {market} geannuleerd"
                         + (f", {sold:.8f} verkocht en geboekt" if sold > EPS else ""))
        return notes
