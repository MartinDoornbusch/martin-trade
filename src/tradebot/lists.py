"""Runtime-beheer van markets/watchlist vanuit de GUI.

Overrides staan in de database en winnen van de HA-opties/yaml zodra ze bestaan.
Caps beschermen tegen alt-sprawl (post-mortem: veel trading-markten = verlies).
"""
from __future__ import annotations

import re

from .db import KVRow, session

MARKETS_KEY = "override_markets"
WATCHLIST_KEY = "override_watchlist"
MAX_MARKETS = 5
MAX_WATCHLIST = 15
_MARKET_RE = re.compile(r"^[A-Z0-9]{1,10}-EUR$")


def _get_kv(key: str) -> str | None:
    with session() as s:
        row = s.get(KVRow, key)
        return row.value if row else None


def _set_kv(key: str, value: str) -> None:
    with session() as s:
        row = s.get(KVRow, key)
        if row is None:
            s.add(KVRow(key=key, value=value))
        else:
            row.value = value
        s.commit()


def get_lists(cfg) -> dict:
    """Actuele lijsten: DB-override als die bestaat, anders HA-opties/yaml."""
    m_raw = _get_kv(MARKETS_KEY)
    w_raw = _get_kv(WATCHLIST_KEY)
    markets = [m for m in m_raw.split(",") if m] if m_raw is not None else list(cfg.markets)
    watchlist = [m for m in w_raw.split(",") if m] if w_raw is not None else list(cfg.watchlist)
    return {"markets": markets, "watchlist": watchlist,
            "source": "gui" if (m_raw is not None or w_raw is not None) else "config",
            "max_markets": MAX_MARKETS, "max_watchlist": MAX_WATCHLIST}


def modify(cfg, list_name: str, market: str, action: str) -> tuple[bool, str]:
    """Voegt toe of verwijdert. Verplaatsen tussen lijsten gaat automatisch
    (toevoegen aan de één haalt hem uit de ander). Returns (ok, melding)."""
    market = market.strip().upper()
    if list_name not in ("markets", "watchlist"):
        return False, f"onbekende lijst: {list_name}"
    if not _MARKET_RE.match(market):
        return False, f"ongeldige marktnotatie: {market} (verwacht bijv. SOL-EUR)"
    state = get_lists(cfg)
    markets, watchlist = state["markets"], state["watchlist"]

    if action == "remove":
        target = markets if list_name == "markets" else watchlist
        if market not in target:
            return False, f"{market} staat niet in {list_name}"
        if list_name == "markets" and len(markets) <= 1:
            return False, "minimaal 1 trading-markt vereist"
        target.remove(market)
    elif action == "add":
        if list_name == "markets":
            if market in markets:
                return False, f"{market} staat al in markets"
            if len(markets) >= MAX_MARKETS:
                return False, (f"max {MAX_MARKETS} trading-markten — bewust hard: veel "
                               "alt-markten was een verliesoorzaak van de oude bot")
            markets.append(market)
            if market in watchlist:
                watchlist.remove(market)
        else:
            if market in watchlist:
                return False, f"{market} staat al in de watchlist"
            if market in markets:
                return False, f"{market} staat al in markets"
            if len(watchlist) >= MAX_WATCHLIST:
                return False, f"max {MAX_WATCHLIST} watchlist-markten (API-budget)"
            watchlist.append(market)
    else:
        return False, f"onbekende actie: {action}"

    _set_kv(MARKETS_KEY, ",".join(markets))
    _set_kv(WATCHLIST_KEY, ",".join(watchlist))
    return True, f"{market} {'toegevoegd aan' if action == 'add' else 'verwijderd uit'} {list_name}"


PAUSED_KEY = "trading_paused"


def is_paused() -> bool:
    return _get_kv(PAUSED_KEY) == "1"


def set_paused(paused: bool) -> None:
    _set_kv(PAUSED_KEY, "1" if paused else "0")
