"""FastAPI dashboard: positions, trades, signals, LLM verdicts, equity (all P&L net of fees)."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .config import get_secrets
from .db import EquityRow, LLMCallRow, SignalRow, TradeRow, session

app = FastAPI(title="AI Trade Platform", docs_url=None, redoc_url=None)


def check_token(request: Request) -> None:
    token = get_secrets().dashboard_token
    if token and request.headers.get("x-dashboard-token") != token \
            and request.query_params.get("token") != token:
        raise HTTPException(status_code=401, detail="invalid dashboard token")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


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
    return {
        "closed_trades": len(sells),
        "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "net_pnl_eur": round(sum(t.pnl_eur for t in sells), 2),
        "total_fees_eur": round(fees, 2),
    }


DASHBOARD_HTML = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<title>AI Trade Platform</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
header{padding:16px 24px;background:#1e293b;display:flex;justify-content:space-between;align-items:center}
h1{font-size:18px;margin:0}main{padding:24px;display:grid;gap:24px;max-width:1100px;margin:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:#1e293b;border-radius:8px;padding:16px}.card b{font-size:22px;display:block}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #334155}
h2{font-size:15px;margin:0 0 8px}.pos{color:#4ade80}.neg{color:#f87171}
section{background:#1e293b;border-radius:8px;padding:16px;overflow-x:auto}
</style></head><body>
<header><h1>AI Trade Platform — paper trading</h1><span id="mode"></span></header>
<main>
<div class="cards" id="cards"></div>
<section><h2>Open beslissingen / signalen</h2><table id="signals"></table></section>
<section><h2>Trades (P&amp;L na fees)</h2><table id="trades"></table></section>
<section><h2>LLM second opinions</h2><table id="llm"></table></section>
</main>
<script>
const T = new URLSearchParams(location.search).get('token') || '';
// Relatieve basis zodat het dashboard ook achter HA ingress (pad-prefix) werkt.
const B = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
const q = p => fetch(B + p + (p.includes('?')?'&':'?') + 'token=' + T).then(r=>r.json());
const fmt = n => n==null?'—':Number(n).toFixed(2);
async function load(){
  const s = await q('api/stats');
  document.getElementById('cards').innerHTML = [
    ['Closed trades', s.closed_trades],
    ['Win-rate', s.win_rate_pct==null?'—':s.win_rate_pct+'%'],
    ['Netto P&L (EUR)', fmt(s.net_pnl_eur)],
    ['Totaal fees (EUR)', fmt(s.total_fees_eur)],
  ].map(([k,v])=>`<div class="card">${k}<b>${v}</b></div>`).join('');
  const sig = await q('api/signals?limit=20');
  document.getElementById('signals').innerHTML =
    '<tr><th>tijd</th><th>markt</th><th>signaal</th><th>besluit</th><th>reden</th></tr>' +
    sig.map(r=>`<tr><td>${r.ts.slice(0,16)}</td><td>${r.market}</td><td>${r.action}</td><td>${r.decision}</td><td>${r.reason}</td></tr>`).join('');
  const tr = await q('api/trades?limit=20');
  document.getElementById('trades').innerHTML =
    '<tr><th>tijd</th><th>markt</th><th>kant</th><th>prijs</th><th>fee</th><th>P&L</th></tr>' +
    tr.map(r=>`<tr><td>${r.ts.slice(0,16)}</td><td>${r.market}</td><td>${r.side}</td><td>${fmt(r.price)}</td><td>${fmt(r.fee_eur)}</td><td class="${r.pnl_eur>=0?'pos':'neg'}">${fmt(r.pnl_eur)}</td></tr>`).join('');
  const llm = await q('api/llm?limit=15');
  document.getElementById('llm').innerHTML =
    '<tr><th>tijd</th><th>provider</th><th>markt</th><th>verdict</th><th>conf</th><th>reden</th></tr>' +
    llm.map(r=>`<tr><td>${r.ts.slice(0,16)}</td><td>${r.provider}</td><td>${r.market}</td><td>${r.verdict}</td><td>${fmt(r.confidence)}</td><td>${r.reasoning}</td></tr>`).join('');
}
load(); setInterval(load, 60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_token)])
def dashboard():
    return DASHBOARD_HTML
