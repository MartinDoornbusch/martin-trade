"""FastAPI dashboard: markten, paper portfolio, echte Bitvavo-balans, trades, signalen, LLM."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from .backtest import max_drawdown_pct
from .config import get_config, get_secrets
from .correlation import correlation_from_closes
from .db import EquityRow, KVRow, LLMCallRow, PositionRow, SignalRow, TradeRow, session
from .decision import FeeModel
from .exchange import BitvavoClient
from .lists import get_lists, modify
from .scanner import scan
from .strategy import build_snapshot, evaluate_buy

app = FastAPI(title="AI Trade Platform", docs_url=None, redoc_url=None)

_feed: BitvavoClient | None = None

DUST_EUR = 1.0  # assets onder deze waarde worden samengevat als 'overig'
SCANNER_TTL_S = 1800  # scan is duur (ticker/24h + ~40 candle-calls); max 1x per half uur
_scanner_cache: dict = {"ts": 0.0, "data": None}


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
                         cfg.fees["slippage_buffer_pct"])
    min_edge = fee_model.min_edge_pct(float(cfg.decision["min_profit_pct"]))
    with session() as s:
        open_markets = [r.market for r in s.execute(select(PositionRow)).scalars().all()]
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


@app.get("/api/lists", dependencies=[Depends(check_token)])
def lists_get():
    return get_lists(get_config())


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


@app.get("/api/portfolio", dependencies=[Depends(check_token)])
def portfolio():
    """Paper-portfolio: cash + open posities tegen actuele prijzen."""
    feed = get_feed()
    with session() as s:
        cash_row = s.get(KVRow, "paper_cash_eur")
        fees_row = s.get(KVRow, "paper_fees_cumulative_eur")
        positions = s.execute(select(PositionRow)).scalars().all()
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
    return {"cash_eur": round(cash, 2), "total_eur": round(total, 2),
            "fees_cumulative_eur": round(fees_cum, 2), "positions": out}


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
        rows = s.execute(select(TradeRow).order_by(TradeRow.ts.desc()).limit(limit)).scalars().all()
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
    with session() as s:
        sells = s.execute(select(TradeRow).where(TradeRow.side == "sell")).scalars().all()
        fees = sum(r.fee_eur for r in s.execute(select(TradeRow)).scalars().all())
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


DASHBOARD_HTML = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<title>AI Trade Platform</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b1220;--panel:#151e2e;--line:#26334a;--txt:#e2e8f0;--sub:#8ea0b8}
body{font-family:system-ui,sans-serif;margin:0;background:var(--bg);color:var(--txt)}
header{padding:14px 24px;background:var(--panel);border-bottom:1px solid var(--line);
display:flex;justify-content:space-between;align-items:center}
h1{font-size:17px;margin:0}#upd{font-size:12px;color:var(--sub)}
main{padding:20px;display:grid;gap:16px;max-width:1200px;margin:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card span{font-size:12px;color:var(--sub)}.card b{font-size:20px;display:block;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{color:var(--sub);font-weight:500;font-size:12px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th.num,td.num{text-align:right}
h2{font-size:14px;margin:0 0 10px;color:var(--txt)}
.pos{color:#4ade80}.neg{color:#f87171}.muted{color:var(--sub)}
.up{color:#4ade80}.down{color:#f87171}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto}
.bar{display:inline-block;height:6px;background:#3b82f6;border-radius:3px;vertical-align:middle;margin-right:6px}
.chip{display:inline-flex;align-items:center;gap:6px;background:#0b1220;border:1px solid var(--line);border-radius:14px;padding:3px 10px;margin:2px 4px 2px 0;font-size:13px}
.chip button,.rowbtn{background:#26334a;border:0;color:var(--txt);border-radius:6px;padding:1px 7px;font-size:12px;cursor:pointer}
.chip button:hover,.rowbtn:hover{background:#3b82f6}
#addmarket{background:#0b1220;border:1px solid var(--line);color:var(--txt);border-radius:6px;padding:5px 8px;width:130px}
#listmsg{font-size:12px;margin-left:8px}
</style></head><body>
<header><h1>AI Trade Platform — paper trading</h1><span id="upd"></span></header>
<main>
<div class="cards" id="cards"></div>
<section><h2>Instellingen — markten</h2>
<div id="listbox"></div>
<div style="margin-top:10px">
  <input id="addmarket" placeholder="bijv. SOL-EUR">
  <button class="rowbtn" onclick="act('watchlist', document.getElementById('addmarket').value, 'add')">+ watchlist</button>
  <button class="rowbtn" onclick="act('markets', document.getElementById('addmarket').value, 'add')">+ trading</button>
  <span id="listmsg" class="muted"></span>
</div>
<p class="muted" style="margin-bottom:0">Wijzigingen gelden direct (volgende analysecyclus), geen herstart nodig. Trading max 5 markten (bewust), watchlist max 15. Frequentie, candle-interval, positiegrootte, cooldown en API-keys beheer je in HA: Add-on → Configuratie.</p>
</section>
<section><h2>Equity-verloop (paper)</h2><svg id="equity" width="100%" height="80" preserveAspectRatio="none"></svg></section>
<section><h2>Markten (wat de bot ziet)</h2><table id="markets"></table></section>
<section><h2>Instap-advies <span class="muted">(watchlist wordt niet door de bot verhandeld)</span></h2><table id="advice"></table></section>
<section><h2>Scanner — alle Bitvavo-markten <span class="muted">(kandidaten; toevoegen via add-on-config, elk half uur ververst)</span></h2><table id="scanner"></table></section>
<section><h2>Paper portfolio — open posities</h2><table id="positions"></table></section>
<section><h2>Echte Bitvavo-balans <span class="muted">(incl. in order; bot handelt hier niet op)</span></h2><table id="balance"></table></section>
<section><h2>Beslissingen / signalen</h2><table id="signals"></table></section>
<section><h2>Trades (P&amp;L na fees)</h2><table id="trades"></table></section>
<section><h2>LLM second opinions</h2><table id="llm"></table></section>
</main>
<script>
const T = new URLSearchParams(location.search).get('token') || '';
const B = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
const q = p => fetch(B + p + (p.includes('?')?'&':'?') + 'token=' + T).then(r=>r.json());
const fmt = (n,d=2) => n==null?'—':Number(n).toLocaleString('nl-NL',{minimumFractionDigits:d,maximumFractionDigits:d});
function renderLists(l){
  const chip = (m, listName) => `<span class="chip">${m}` +
    (listName==='watchlist' ? ` <button title="promoveer naar trading" onclick="act('markets','${m}','add')">→ trade</button>` : '') +
    ((listName==='markets' && l.markets.length<=1) ? '' : ` <button title="verwijder" onclick="act('${listName}','${m}','remove')">✕</button>`) + '</span>';
  document.getElementById('listbox').innerHTML =
    `<div><b>Trading</b> (${l.markets.length}/${l.max_markets}): ` + l.markets.map(m=>chip(m,'markets')).join('') + '</div>' +
    `<div style="margin-top:6px"><b>Watchlist</b> (${l.watchlist.length}/${l.max_watchlist}): ` + (l.watchlist.length? l.watchlist.map(m=>chip(m,'watchlist')).join('') : '<span class="muted">leeg</span>') + '</div>';
}
async function act(listName, market, action){
  if(!market){ return; }
  const r = await fetch(B + 'api/lists?token=' + T, {method:'POST',
    headers:{'content-type':'application/json'},
    body: JSON.stringify({list_name:listName, market:market.trim().toUpperCase(), action:action})}).then(x=>x.json());
  const msg = document.getElementById('listmsg');
  msg.textContent = r.message; msg.className = r.ok ? 'pos' : 'neg';
  renderLists(r.lists);
  if(r.ok){ document.getElementById('addmarket').value=''; load(); }
}
const cls = n => n>=0?'pos':'neg';
async function load(){
  const [s, pf, bal, mkts, adv, lst] = await Promise.all([
    q('api/stats'), q('api/portfolio'), q('api/balance'), q('api/markets'), q('api/advice'), q('api/lists')]);
  renderLists(lst);
  document.getElementById('cards').innerHTML = [
    ['Paper portfolio', '€ '+fmt(pf.total_eur)],
    ['Cash', '€ '+fmt(pf.cash_eur)],
    ['Closed trades', s.closed_trades],
    ['Win-rate', s.win_rate_pct==null?'—':s.win_rate_pct+'%'],
    ['Netto P&L', '€ '+fmt(s.net_pnl_eur)],
    ['Totaal fees', '€ '+fmt(s.total_fees_eur)],
    ['Max drawdown', s.max_drawdown_pct==null?'—':s.max_drawdown_pct+'%'],
    ['LLM veto-rate', s.llm_veto_rate_pct==null?'—':s.llm_veto_rate_pct+'% ('+s.llm_calls+')'],
  ].map(([k,v])=>`<div class="card"><span>${k}</span><b>${v}</b></div>`).join('');
  document.getElementById('markets').innerHTML =
    '<tr><th>markt</th><th class="num">koers</th><th class="num">24h</th><th class="num">RSI</th><th>trend</th><th class="num">EMA-gap</th><th class="num">MACD-hist</th><th class="num">ATR</th></tr>' +
    mkts.map(m=> m.error
      ? `<tr><td>${m.market}</td><td colspan="7" class="muted">${m.error}</td></tr>`
      : `<tr><td>${m.market}</td><td class="num">€ ${fmt(m.price)}</td><td class="num ${cls(m.change_24h_pct)}">${fmt(m.change_24h_pct,1)}%</td><td class="num">${fmt(m.rsi,0)}</td><td class="${m.trend}">${m.trend==='up'?'▲ up':'▼ down'}</td><td class="num">${fmt(m.ema_gap_pct)}%</td><td class="num ${cls(m.macd_hist)}">${fmt(m.macd_hist,4)}</td><td class="num">${fmt(m.atr_pct,1)}%</td></tr>`).join('');
  document.getElementById('advice').innerHTML =
    '<tr><th>markt</th><th>type</th><th>advies</th><th class="num">score</th><th class="num">verw. move</th><th class="num">vereist</th><th class="num">correlatie</th><th>toelichting</th></tr>' +
    adv.map(a=> a.error
      ? `<tr><td>${a.market}</td><td colspan="7" class="muted">${a.error}</td></tr>`
      : `<tr><td>${a.market}</td><td class="muted">${a.tradeable?'trade':'watch'}</td><td class="${a.advies.startsWith('instappen')?'pos':(a.advies.startsWith('vermijden')?'neg':'muted')}">${a.advies}</td><td class="num">${a.score}/${a.score_needed}</td><td class="num ${a.fee_ok?'pos':'neg'}">${fmt(a.expected_move_pct)}%</td><td class="num">${fmt(a.min_edge_pct)}%</td><td class="num">${a.correlation==null?'—':fmt(a.correlation)+(a.correlation_with?' ('+a.correlation_with+')':'')}</td><td>${(a.reasons||[]).join('; ')||'—'}</td></tr>`).join('');
  document.getElementById('positions').innerHTML =
    '<tr><th>markt</th><th class="num">aantal</th><th class="num">entry</th><th class="num">nu</th><th class="num">waarde</th><th class="num">ongereal. P&L</th><th class="num">SL</th><th class="num">TP</th></tr>' +
    (pf.positions.length ? pf.positions.map(p=>`<tr><td>${p.market}</td><td class="num">${p.amount.toFixed(6)}</td><td class="num">${fmt(p.entry_price)}</td><td class="num">${fmt(p.current_price)}</td><td class="num">€ ${fmt(p.value_eur)}</td><td class="num ${cls(p.unrealized_pnl_eur)}">€ ${fmt(p.unrealized_pnl_eur)}</td><td class="num">${fmt(p.stop_loss)}</td><td class="num">${fmt(p.take_profit)}</td></tr>`).join('')
      : '<tr><td colspan="8" class="muted">geen open posities — de bot wacht op een signaal dat door alle gates komt</td></tr>');
  let balRows;
  if (bal.enabled) {
    balRows = '<tr><th>asset</th><th class="num">aantal</th><th class="num">waarde</th><th>aandeel</th></tr>' +
      bal.assets.map(a=>`<tr><td>${a.symbol}</td><td class="num">${a.amount}</td><td class="num">€ ${fmt(a.value_eur)}</td><td><span class="bar" style="width:${Math.max(2,a.share_pct)}px"></span>${fmt(a.share_pct,1)}%</td></tr>`).join('');
    if (bal.dust) balRows += `<tr><td class="muted">overig (${bal.dust.count} assets &lt; €1)</td><td></td><td class="num muted">€ ${fmt(bal.dust.value_eur)}</td><td></td></tr>`;
    balRows += `<tr><td><b>Totaal</b></td><td></td><td class="num"><b>€ ${fmt(bal.total_eur)}</b></td><td></td></tr>`;
  } else {
    balRows = `<tr><td class="muted">${bal.error ? 'fout: '+bal.error : 'geen Bitvavo API-key geconfigureerd'}</td></tr>`;
  }
  document.getElementById('balance').innerHTML = balRows;
  const sig = await q('api/signals?limit=20');
  document.getElementById('signals').innerHTML =
    '<tr><th>tijd</th><th>markt</th><th>signaal</th><th>besluit</th><th>reden</th></tr>' +
    sig.map(r=>`<tr><td>${r.ts.slice(0,16).replace('T',' ')}</td><td>${r.market}</td><td>${r.action}</td><td>${r.decision}</td><td>${r.reason}</td></tr>`).join('');
  const tr = await q('api/trades?limit=20');
  document.getElementById('trades').innerHTML =
    '<tr><th>tijd</th><th>markt</th><th>kant</th><th class="num">prijs</th><th class="num">fee</th><th class="num">P&L</th></tr>' +
    (tr.length ? tr.map(r=>`<tr><td>${r.ts.slice(0,16).replace('T',' ')}</td><td>${r.market}</td><td>${r.side}</td><td class="num">${fmt(r.price)}</td><td class="num">${fmt(r.fee_eur)}</td><td class="num ${cls(r.pnl_eur)}">${fmt(r.pnl_eur)}</td></tr>`).join('')
      : '<tr><td colspan="6" class="muted">nog geen trades</td></tr>');
  const llm = await q('api/llm?limit=15');
  document.getElementById('llm').innerHTML =
    '<tr><th>tijd</th><th>provider</th><th>markt</th><th>verdict</th><th class="num">conf</th><th>reden</th></tr>' +
    (llm.length ? llm.map(r=>`<tr><td>${r.ts.slice(0,16).replace('T',' ')}</td><td>${r.provider}</td><td>${r.market}</td><td>${r.verdict}</td><td class="num">${fmt(r.confidence)}</td><td>${r.reasoning}</td></tr>`).join('')
      : '<tr><td colspan="6" class="muted">nog geen LLM-calls — die volgen zodra een koopsignaal alle mechanische gates passeert</td></tr>');
  const sc = await q('api/scanner');
  const scStats = sc.stats ? `<tr><td colspan="9" class="muted">trechter: ${sc.stats.eur_markets} EUR-markten gescand → ${sc.stats.liquid} door liquiditeitsfilter (volume ≥ € ${Number(sc.stats.min_volume_eur).toLocaleString('nl-NL')}, spread ≤ ${sc.stats.max_spread_pct}%) → ${sc.stats.analyzed} geanalyseerd → top ${sc.stats.shown} getoond</td></tr>` : '';
  document.getElementById('scanner').innerHTML = sc.error
    ? `<tr><td class="muted">fout: ${sc.error}</td></tr>`
    : (scStats + '<tr><th>markt</th><th class="num">24h volume</th><th class="num">spread</th><th class="num">score</th><th>trend</th><th class="num">RSI</th><th class="num">verw. move</th><th class="num">vereist</th><th>actie</th></tr>' +
       sc.results.map(r=>`<tr><td>${r.market}</td><td class="num">€ ${Number(r.volume_eur).toLocaleString('nl-NL')}</td><td class="num">${fmt(r.spread_pct)}%</td><td class="num">${r.score}/${r.score_needed}</td><td class="${r.trend}">${r.trend==='up'?'▲':'▼'}</td><td class="num">${fmt(r.rsi,0)}</td><td class="num ${r.fee_ok?'pos':'neg'}">${fmt(r.expected_move_pct)}%</td><td class="num">${fmt(r.required_pct)}%</td><td>${r.in_markets?'<span class="muted">in trading</span>':(r.in_watchlist?`<span class="muted">in watchlist</span> <button class="rowbtn" onclick="act('markets','${r.market}','add')">→ trade</button>`:`<button class="rowbtn" onclick="act('watchlist','${r.market}','add')">+ watch</button> <button class="rowbtn" onclick="act('markets','${r.market}','add')">+ trade</button>`)}</td></tr>`).join(''));
  const eq = await q('api/equity');
  if (eq.length >= 2) {
    const vals = eq.map(e=>e.total_eur), mn = Math.min(...vals), mx = Math.max(...vals);
    const w = document.getElementById('equity').clientWidth || 600;
    const pts = vals.map((v,i)=>`${(i/(vals.length-1)*w).toFixed(1)},${(72-(mx>mn?(v-mn)/(mx-mn):0.5)*64).toFixed(1)}`).join(' ');
    document.getElementById('equity').innerHTML =
      `<polyline points="${pts}" fill="none" stroke="#3b82f6" stroke-width="2"/>`;
  } else {
    document.getElementById('equity').outerHTML = '<span class="muted">nog te weinig equity-snapshots (elke 6 uur één)</span>';
  }
  document.getElementById('upd').textContent = 'bijgewerkt ' + new Date().toLocaleTimeString('nl-NL');
}
load(); setInterval(load, 60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_token)])
def dashboard():
    return DASHBOARD_HTML
