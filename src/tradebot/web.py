"""FastAPI dashboard: markten, paper portfolio, echte Bitvavo-balans, trades, signalen, LLM.

Dit bestand is sinds v0.22.0 puur API. De front-end staat als losse bestanden in
`static/` (index.html, app.css, app.js, charts.js, vendor/uPlot). Reden voor de
splitsing: de HTML zat als Python-string in dit bestand, waardoor elke
opmaakwijziging een diff in de API-module gaf en syntax highlighting, linting en
browser-caching allemaal wegvielen.

`static/` ligt bewust BINNEN het package. `Dockerfile` doet `COPY src/ src/` en
`tradebot-addon/sync.sh` doet `cp -r ../src/. src/`, dus de assets reizen mee
zonder dat aan de add-on-build iets hoeft te veranderen.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from . import __version__
from .analysis import (
    analyze_breakeven,
    analyze_chase,
    analyze_regime,
    analyze_vetos,
)
from .backtest import max_drawdown_pct
from .config import gate_fingerprint, get_config, get_secrets
from .correlation import correlation_from_closes
from .db import EquityRow, KVRow, LLMCallRow, PositionRow, SignalRow, TradeRow, session
from .decision import FeeModel, RiskManager, breakeven_win_rate
from .exchange import BitvavoClient
from .indicators import ema
from .lists import get_lists, is_paused, modify, set_paused
from .scanner import scan
from .strategy import build_snapshot, evaluate_buy

app = FastAPI(title="AI Trade Platform", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
# Assets zijn bewust NIET achter `check_token`: ze bevatten geen data, alleen
# opmaak en code. De data-endpoints houden hun token. Zo kan de browser ze ook
# gewoon cachen zonder token in de URL.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_feed: BitvavoClient | None = None

DUST_EUR = 1.0  # assets onder deze waarde worden samengevat als 'overig'
SCANNER_TTL_S = 1800  # scan is duur (ticker/24h + ~40 candle-calls); max 1x per half uur
_scanner_cache: dict = {"ts": 0.0, "data": None}
VETO_TTL_S = 1800  # veto-analyse haalt candle-historie per markt; max 1x per half uur
_veto_cache: dict = {}  # per scope ("current"/"all"): {"ts": float, "data": dict}


def get_feed() -> BitvavoClient:
    global _feed
    if _feed is None:
        s = get_secrets()
        cfg = get_config()
        _feed = BitvavoClient(s.bitvavo_api_key, s.bitvavo_api_secret,
                              cfg.fees["maker_pct"], cfg.fees["taker_pct"])
    return _feed


def check_token(request: Request) -> None:
    token = get_secrets().dashboard_token
    if token and request.headers.get("x-dashboard-token") != token \
            and request.query_params.get("token") != token:
        raise HTTPException(status_code=401, detail="invalid dashboard token")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/markets", dependencies=[Depends(check_token)])
def markets():
    """Actuele koers + indicator-snapshot per geconfigureerde markt: wat de bot ziet."""
    cfg = get_config()
    feed = get_feed()
    out = []
    for market in get_lists(cfg)["markets"]:
        try:
            candles = feed.get_candles(market, cfg.schedule["candle_interval"], 80)
            snap = build_snapshot(market, candles, cfg.strategy)
            out.append({
                "market": market,
                "price": snap.price,
                "rsi": round(snap.rsi, 1),
                "trend": "up" if snap.ema_fast > snap.ema_slow else "down",
                "ema_gap_pct": round((snap.ema_fast / snap.ema_slow - 1) * 100, 2),
                "macd_hist": round(snap.macd_hist, 4),
                "atr_pct": round(snap.atr / snap.price * 100, 2),
                "change_24h_pct": round(snap.change_24c_pct, 2),
            })
        except Exception as exc:  # noqa: BLE001 - één markt mag de tabel niet breken
            out.append({"market": market, "error": str(exc)[:100]})
    return out


@app.get("/api/advice", dependencies=[Depends(check_token)])
def advice():
    """Instap-advies per markt (trading + watchlist). Advies aan de gebruiker;
    de bot gebruikt dit NIET als koop-trigger (zie post-mortem in PROJECTPLAN)."""
    cfg = get_config()
    feed = get_feed()
    fee_model = FeeModel(cfg.fees["maker_pct"], cfg.fees["taker_pct"],
                         cfg.fees["slippage_buffer_pct"],
                         entry_is_maker=get_secrets().trading_mode == "live")
    min_edge = fee_model.min_edge_pct(float(cfg.decision["min_profit_pct"]))
    with session() as s:
        open_markets = [r.market for r in s.execute(
            select(PositionRow).where(PositionRow.mode == get_secrets().trading_mode)
        ).scalars().all()]
    lookback = int(cfg.risk.get("correlation_lookback", 60))
    max_corr = float(cfg.risk.get("max_correlation", 0.85))
    interval = cfg.schedule["candle_interval"]
    active = get_lists(cfg)
    all_markets = list(dict.fromkeys(active["markets"] + active["watchlist"]))
    closes_cache: dict[str, list[float]] = {}

    def closes_for(m: str) -> list[float]:
        if m not in closes_cache:
            closes_cache[m] = [c.close for c in feed.get_candles(m, interval, 80)]
        return closes_cache[m]

    out = []
    for market in all_markets:
        try:
            candles = feed.get_candles(market, interval, 80)
            closes_cache[market] = [c.close for c in candles]
            snap = build_snapshot(market, candles, cfg.strategy)
            cand = evaluate_buy(snap, cfg.strategy)
            stop_dist = snap.atr * float(cfg.decision["atr_stop_multiplier"])
            expected = stop_dist * float(cfg.decision["reward_risk_ratio"]) / snap.price * 100
            fee_ok = expected >= min_edge
            corr_max, corr_with = None, None
            for om in open_markets:
                if om == market:
                    continue
                try:
                    c = correlation_from_closes(closes_cache[market], closes_for(om), lookback)
                except Exception:  # noqa: BLE001, S112 - watchlist-markt zonder data overslaan
                    continue
                if c is not None and (corr_max is None or c > corr_max):
                    corr_max, corr_with = c, om
            corr_ok = corr_max is None or corr_max <= max_corr
            trend_up = snap.ema_fast > snap.ema_slow
            score_needed = int(cfg.strategy["min_signal_score"])
            if market in open_markets:
                label = "positie open"
            elif cand.score >= score_needed and fee_ok and corr_ok and trend_up:
                label = "instappen overwegen"
            elif not corr_ok:
                label = "vermijden (correlatie)"
            elif not trend_up and snap.rsi >= float(cfg.strategy["rsi_overbought"]):
                label = "vermijden"
            else:
                label = "afwachten"
            out.append({
                "market": market,
                "tradeable": market in active["markets"],
                "advies": label,
                "score": cand.score, "score_needed": score_needed,
                "trend": "up" if trend_up else "down",
                "rsi": round(snap.rsi, 0),
                "expected_move_pct": round(expected, 2),
                "min_edge_pct": round(min_edge, 2),
                "breakeven_win_rate_pct": (
                    lambda p: None if p is None else round(p * 100, 1))(
                    breakeven_win_rate(
                        snap.atr / snap.price * 100 if snap.price > 0 else 0.0,
                        float(cfg.decision["atr_stop_multiplier"]),
                        float(cfg.decision["reward_risk_ratio"]),
                        fee_model.round_trip_pct() + fee_model.slippage_buffer_pct)),
                "fee_ok": fee_ok,
                "correlation": round(corr_max, 2) if corr_max is not None else None,
                "correlation_with": corr_with,
                "reasons": cand.reasons,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"market": market, "error": str(exc)[:100]})
    return out


@app.get("/api/scanner", dependencies=[Depends(check_token)])
def scanner(refresh: bool = False):
    """Screent alle Bitvavo EUR-markten. Advies: toevoegen doe je zelf via de
    add-on-configuratie; de bot handelt nooit zelf in een gescande markt."""
    import time as _time
    now = _time.time()
    if not refresh and _scanner_cache["data"] is not None \
            and now - _scanner_cache["ts"] < SCANNER_TTL_S:
        return _scanner_cache["data"]
    cfg = get_config()
    try:
        results, stats = scan(get_feed(), cfg)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200], "results": [], "stats": None, "cached_at": None}
    payload = {"results": results, "stats": stats, "cached_at": now,
               "ttl_s": SCANNER_TTL_S, "error": None}
    _scanner_cache.update(ts=now, data=payload)
    return payload


class ListEdit(BaseModel):
    list_name: str
    market: str
    action: str  # add | remove


def _quiet_markets(markets: list[str], quiet_days: int) -> list[dict]:
    """Gepinde markten met 0 koopsignalen in de laatste `quiet_days` dagen.
    Puur advies voor het dashboard; de bot verplaatst zelf niets."""
    from datetime import datetime, timedelta, timezone
    if not markets or quiet_days <= 0:
        return []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=quiet_days)
    out: list[dict] = []
    with session() as s:
        for m in markets:
            last = s.execute(
                select(SignalRow.ts).where(SignalRow.market == m, SignalRow.action == "buy")
                .order_by(SignalRow.ts.desc()).limit(1)).scalars().first()
            if last is None:
                out.append({"market": m, "days": None})
                continue
            if last.tzinfo is None:  # SQLite geeft tz-naïef terug; behandel als UTC
                last = last.replace(tzinfo=timezone.utc)
            if last < cutoff:
                out.append({"market": m, "days": (now - last).days})
    return out


@app.get("/api/lists", dependencies=[Depends(check_token)])
def lists_get():
    cfg = get_config()
    data = get_lists(cfg)
    data["quiet"] = _quiet_markets(data["markets"],
                                   int((getattr(cfg, "curation", {}) or {}).get("quiet_days", 30)))
    return data


@app.post("/api/lists", dependencies=[Depends(check_token)])
def lists_edit(edit: ListEdit):
    cfg = get_config()
    if edit.action == "add":
        try:
            get_feed().get_price(edit.market.strip().upper())
        except Exception:  # noqa: BLE001
            return {"ok": False, "message": f"{edit.market} bestaat niet op Bitvavo",
                    "lists": get_lists(cfg)}
    ok, message = modify(cfg, edit.list_name, edit.market, edit.action)
    return {"ok": ok, "message": message, "lists": get_lists(cfg)}


def build_chart_payload(market: str, candles, cfg, position=None) -> dict:
    """Puur en testbaar: OHLC + EMA-reeksen + positieniveaus voor de grafiek.

    Open/high/low zitten er sinds v0.22.0 bij zodat de front-end candlesticks kan
    tekenen in plaats van alleen een slotkoerslijn. Een lijn van closes verbergt
    precies wat voor deze bot telt: de intrabar-uitslag waar SL en TP op afgaan.
    """
    closes = [c.close for c in candles]
    ef = ema(closes, int(cfg.strategy["ema_fast"]))
    es = ema(closes, int(cfg.strategy["ema_slow"]))
    return {
        "market": market,
        "interval": cfg.schedule["candle_interval"],
        "ema_fast_period": int(cfg.strategy["ema_fast"]),
        "ema_slow_period": int(cfg.strategy["ema_slow"]),
        "ts": [c.ts for c in candles],
        "open": [round(c.open, 8) for c in candles],
        "high": [round(c.high, 8) for c in candles],
        "low": [round(c.low, 8) for c in candles],
        "close": [round(v, 8) for v in closes],
        "ema_fast": [round(float(v), 8) for v in ef],
        "ema_slow": [round(float(v), 8) for v in es],
        "position": {"entry": position.entry_price, "stop_loss": position.stop_loss,
                     "take_profit": position.take_profit} if position else None,
    }


@app.get("/api/chart", dependencies=[Depends(check_token)])
def chart(market: str):
    cfg = get_config()
    market = market.strip().upper()
    active = get_lists(cfg)
    if market not in active["markets"] + active["watchlist"]:
        raise HTTPException(status_code=400, detail="markt niet in markets/watchlist")
    candles = get_feed().get_candles(market, cfg.schedule["candle_interval"], 140)
    with session() as s:
        pos = s.execute(select(PositionRow).where(
            PositionRow.market == market,
            PositionRow.mode == get_secrets().trading_mode)).scalar_one_or_none()
    return build_chart_payload(market, candles, cfg, pos)


@app.get("/api/mode", dependencies=[Depends(check_token)])
def mode():
    secrets = get_secrets()
    cfg = get_config()
    meta = getattr(cfg, "meta", {}) or {}
    # `version` erbij zodat je op het dashboard ziet welke build er draait. De Pi
    # loopt achter tot de HA-add-on is bijgewerkt, en dan wijkt het gedrag af van
    # wat in git staat; zonder dit getal zie je dat verschil niet.
    #
    # `run_purpose` en `run_until` staan sinds v0.22.0 in de statusbalk bovenaan
    # en niet meer alleen bij de gate-secties. Reden: het dashboard opende met
    # P&L, win-rate en drawdown, en die drie lezen als strategievalidatie. Sinds
    # het "geen edge"-verdict van 2026-08-06 is dat precies de verkeerde lezing;
    # de lopende run is een infrastructuurtest. Het label hoort dus vóór de
    # cijfers te staan, niet eronder.
    return {"mode": secrets.trading_mode, "paused": is_paused(),
            "live_max_capital_eur": secrets.live_max_capital_eur,
            "version": __version__,
            "run_purpose": meta.get("run_purpose"),
            "run_until": str(meta.get("run_until")) if meta.get("run_until") else None,
            "candle_interval": cfg.schedule.get("candle_interval"),
            "analysis_interval_minutes": cfg.schedule.get("analysis_interval_minutes"),
            "sizing": cfg.risk.get("sizing"),
            "bucket_eur": cfg.risk.get("bucket_eur"),
            "gates": {
                "veto": bool(cfg.decision.get("llm_veto_binding")),
                "regime": bool((cfg.regime or {}).get("binding")),
                "breakeven": bool(((cfg.exits or {}).get("breakeven_stop") or {}).get("binding")),
                "chase": bool(cfg.strategy.get("chase_guard_binding")),
                "timestop": bool((cfg.exits or {}).get("time_stop_binding")),
            }}


class PauseEdit(BaseModel):
    paused: bool


@app.post("/api/pause", dependencies=[Depends(check_token)])
def pause(edit: PauseEdit):
    set_paused(edit.paused)
    return {"ok": True, "paused": is_paused()}


@app.get("/api/portfolio", dependencies=[Depends(check_token)])
def portfolio():
    """Portfolio: cash + open posities van de draaiende mode tegen actuele prijzen.

    Posities zijn mode-gefilterd. De cash- en fee-tellers komen uit de KV-store en
    zijn paper-specifiek; in live mode is de echte EUR-balans leidend (zie
    `LiveBroker.cash_eur`) en is het cash-getal hier dus niet gezaghebbend.
    """
    feed = get_feed()
    with session() as s:
        cash_row = s.get(KVRow, "paper_cash_eur")
        fees_row = s.get(KVRow, "paper_fees_cumulative_eur")
        positions = s.execute(
            select(PositionRow).where(PositionRow.mode == get_secrets().trading_mode)
        ).scalars().all()
    cash = float(cash_row.value) if cash_row else 0.0
    fees_cum = float(fees_row.value) if fees_row else 0.0
    out, total = [], cash
    for p in positions:
        try:
            price = feed.get_price(p.market)
        except Exception:  # noqa: BLE001
            price = p.entry_price
        value = p.amount * price
        total += value
        cost = p.amount * p.entry_price + p.fees_paid_eur
        out.append({
            "market": p.market, "amount": p.amount, "entry_price": p.entry_price,
            "current_price": price, "value_eur": round(value, 2),
            "unrealized_pnl_eur": round(value - cost, 2),
            "stop_loss": p.stop_loss, "take_profit": p.take_profit,
        })
    risk = RiskManager(get_config().risk)
    max_pos = risk.effective_max_positions(total)
    return {"cash_eur": round(cash, 2), "total_eur": round(total, 2),
            "fees_cumulative_eur": round(fees_cum, 2), "positions": out,
            "max_positions": max_pos, "open_positions": len(out)}


@app.get("/api/balance", dependencies=[Depends(check_token)])
def real_balance():
    """Echte Bitvavo-balans (available + inOrder). Informatief; de bot handelt hier niet op."""
    secrets = get_secrets()
    if not secrets.bitvavo_api_key:
        return {"enabled": False, "assets": [], "total_eur": 0, "dust": None}
    feed = get_feed()
    try:
        balances = feed.get_balances()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "error": str(exc)[:200], "assets": [], "total_eur": 0,
                "dust": None}
    assets, total = [], 0.0
    for sym, amount in balances.items():
        if amount <= 0:
            continue
        if sym == "EUR":
            value = amount
        else:
            try:
                value = amount * feed.get_price(f"{sym}-EUR")
            except Exception:  # noqa: BLE001 - geen EUR-markt
                value = None
        assets.append({"symbol": sym, "amount": amount,
                       "value_eur": round(value, 2) if value is not None else None})
        total += value or 0.0
    for a in assets:
        a["share_pct"] = round((a["value_eur"] or 0) / total * 100, 1) if total else 0.0
    main = [a for a in assets if (a["value_eur"] or 0) >= DUST_EUR]
    dust = [a for a in assets if (a["value_eur"] or 0) < DUST_EUR]
    main.sort(key=lambda a: -(a["value_eur"] or 0))
    dust_row = {"count": len(dust), "value_eur": round(sum(a["value_eur"] or 0 for a in dust), 2)} \
        if dust else None
    return {"enabled": True, "total_eur": round(total, 2), "assets": main, "dust": dust_row}


@app.get("/api/trades", dependencies=[Depends(check_token)])
def trades(limit: int = 100):
    with session() as s:
        rows = s.execute(select(TradeRow).where(TradeRow.mode == get_secrets().trading_mode)
                         .order_by(TradeRow.ts.desc()).limit(limit)).scalars().all()
    return [{"ts": r.ts.isoformat(), "market": r.market, "side": r.side, "amount": r.amount,
             "price": r.price, "fee_eur": r.fee_eur, "pnl_eur": r.pnl_eur,
             "reason": r.reason} for r in rows]


@app.get("/api/signals", dependencies=[Depends(check_token)])
def signals(limit: int = 100):
    with session() as s:
        rows = s.execute(select(SignalRow).order_by(SignalRow.ts.desc()).limit(limit)).scalars().all()
    return [{"ts": r.ts.isoformat(), "market": r.market, "action": r.action,
             "decision": r.decision, "score": r.score, "reason": r.reason} for r in rows]


@app.get("/api/llm", dependencies=[Depends(check_token)])
def llm_calls(limit: int = 50):
    with session() as s:
        rows = s.execute(select(LLMCallRow).order_by(LLMCallRow.ts.desc()).limit(limit)).scalars().all()
    return [{"ts": r.ts.isoformat(), "provider": r.provider, "market": r.market,
             "verdict": r.verdict, "confidence": r.confidence,
             "reasoning": r.reasoning, "latency_ms": r.latency_ms} for r in rows]


@app.get("/api/equity", dependencies=[Depends(check_token)])
def equity(limit: int = 365):
    with session() as s:
        rows = s.execute(select(EquityRow).order_by(EquityRow.ts.desc()).limit(limit)).scalars().all()
    return [{"ts": r.ts.isoformat(), "total_eur": r.total_eur, "cash_eur": r.cash_eur,
             "fees_cumulative_eur": r.fees_cumulative_eur} for r in reversed(rows)]


@app.get("/api/stats", dependencies=[Depends(check_token)])
def stats():
    current_mode = get_secrets().trading_mode
    with session() as s:
        sells = s.execute(select(TradeRow).where(TradeRow.side == "sell",
                                                 TradeRow.mode == current_mode)).scalars().all()
        fees = sum(r.fee_eur for r in s.execute(
            select(TradeRow).where(TradeRow.mode == current_mode)).scalars().all())
    wins = [t for t in sells if t.pnl_eur > 0]
    with session() as s:
        eq = [r.total_eur for r in s.execute(
            select(EquityRow).order_by(EquityRow.ts.asc())).scalars().all()]
        llm_rows = s.execute(select(LLMCallRow)).scalars().all()
    vetoes = [r for r in llm_rows if r.verdict == "veto"]
    return {
        "closed_trades": len(sells),
        "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "net_pnl_eur": round(sum(t.pnl_eur for t in sells), 2),
        "total_fees_eur": round(fees, 2),
        "max_drawdown_pct": max_drawdown_pct(eq) if len(eq) >= 2 else None,
        "llm_calls": len(llm_rows),
        "llm_veto_rate_pct": round(len(vetoes) / len(llm_rows) * 100, 1) if llm_rows else None,
    }


@app.get("/api/veto-analysis", dependencies=[Depends(check_token)])
def veto_analysis(refresh: bool = False, scope: str = "current"):
    """Veto-gate-analyse: counterfactual (candle-reconstructie) EN echte
    shadow-trade-uitkomst naast elkaar, met precisie plus 95%-marge en
    uitsplitsing per veto-reden. `scope=current` meet alleen de huidige config
    (schone meting), `scope=all` elke veto ooit. Duur, daarom per scope gecachet.
    """
    import time as _time
    scope = "all" if scope == "all" else "current"
    now = _time.time()
    cached = _veto_cache.get(scope)
    if not refresh and cached is not None and now - cached["ts"] < VETO_TTL_S:
        return cached["data"]
    cfg = get_config()
    try:
        config_hash = None if scope == "all" else gate_fingerprint(cfg, "veto")
        data = analyze_vetos(get_feed(), cfg, config_hash=config_hash,
                             mode=get_secrets().trading_mode)
        data["error"] = None
    except Exception as exc:  # noqa: BLE001 - analyse mag het dashboard niet breken
        return {"error": str(exc)[:200], "n_vetos": 0}
    data["cached_at"] = now
    data["ttl_s"] = VETO_TTL_S
    _veto_cache[scope] = {"ts": now, "data": data}
    return data


def _gate_analysis(gate: str, fn, scope: str):
    """Gedeelde afhandeling voor de drie gecodeerde shadow-gates: scope op de
    per-gate config-hash en op de huidige mode, en laat een analysefout nooit het
    dashboard breken. DB-only en licht, dus niet gecachet."""
    cfg = get_config()
    try:
        gate_hash = None if scope == "all" else gate_fingerprint(cfg, gate)
        return fn(cfg, gate_hash=gate_hash, mode=get_secrets().trading_mode)
    except Exception as exc:  # noqa: BLE001 - analyse mag het dashboard niet breken
        return {"error": str(exc)[:200], "n_events": 0, "gate": gate}


@app.get("/api/regime-analysis", dependencies=[Depends(check_token)])
def regime_analysis(scope: str = "current"):
    """Regime-gate: netto gate-waarde van de markt-brede filter, gemeten uit de
    echte round-trip-P&L van regime-down shadow-buys."""
    return _gate_analysis("regime", analyze_regime, scope)


@app.get("/api/breakeven-analysis", dependencies=[Depends(check_token)])
def breakeven_analysis(scope: str = "current"):
    """Breakeven-stop: netto gate-waarde als (hypothetische exit) min (werkelijke
    exit), gededupliceerd op de eerste treffer per positie."""
    return _gate_analysis("breakeven", analyze_breakeven, scope)


@app.get("/api/chase-analysis", dependencies=[Depends(check_token)])
def chase_analysis(scope: str = "current"):
    """Chase-guard: netto gate-waarde van het overslaan van entries waarbij de live
    prijs te ver van de signaalclose af stond."""
    return _gate_analysis("chase", analyze_chase, scope)


@app.get("/")
def dashboard(request: Request):
    """Serveert de single-page front-end uit `static/index.html`.

    De tokencheck staat hier expliciet in de body en niet als `Depends`, zodat een
    ontbrekend token een leesbare pagina kan opleveren in plaats van een kale 401
    van FastAPI. `check_token` gooit nog steeds bij een FOUT token; alleen het
    ontbreken van elk token levert de uitleg-pagina.
    """
    check_token(request)
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
