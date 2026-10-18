"""Eén slot per markt rond elke order (L3 en N13, v0.25.0).

De position guard (elke 60 s) en de analysecyclus (elk uur) draaien in
parallelle threads van dezelfde `BackgroundScheduler`; `max_instances=1` geldt
per job, niet tussen jobs. Beide konden dezelfde positie tegelijk sluiten. In
paper ving SQLAlchemy dat toevallig op (StaleDataError op de tweede delete), live
niet: de exchange-order gaat vóór de database, dus twee market sells. Deelt het
account coins met de gebruiker, dan verkocht de tweede order zijn eigen coins.

Het slot zit in de BROKER en niet in de engine, zodat elke aanroeper beschermd
is: guard, cyclus en straks de "alles sluiten"-knop uit de webthread.

Per markt en niet globaal: een live entry pollt tot 90 s op zijn fill. Met één
globaal slot zou de guard in die tijd geen enkele andere positie kunnen sluiten.
`RLock`, zodat een broker-methode die een andere broker-methode aanroept op
dezelfde markt (sell die eerst een stop annuleert) zichzelf niet blokkeert.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_TIMEOUT_S = 180.0


class MarketBusy(RuntimeError):  # noqa: N818 - leest als toestand
    """Het slot op deze markt kwam niet vrij binnen de timeout."""


class MarketLocks:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def _lock(self, market: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(market)
            if lock is None:
                lock = self._locks[market] = threading.RLock()
            return lock

    @contextmanager
    def hold(self, market: str, timeout: float = DEFAULT_TIMEOUT_S) -> Iterator[None]:
        lock = self._lock(market)
        if not lock.acquire(timeout=timeout):
            raise MarketBusy(f"slot op {market} niet vrij binnen {timeout:.0f}s")
        try:
            yield
        finally:
            lock.release()


# Eén instantie per proces: guard, cyclus en webthread moeten hetzelfde slot zien.
MARKET_LOCKS = MarketLocks()
