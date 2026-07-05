"""FastAPI dashboard: paper portfolio, echte Bitvavo-balans, trades, signalen, LLM verdicts."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .config import get_config, get_secrets
from .db import EquityRow, KVRow, LLMCallRow, PositionRow, SignalRow, TradeRow, session
from .exchange import BitvavoClient

app = FastAPI(title="AI Trade Platform", docs_url=None, redoc_url=None)

_feed: BitvavoClient | None = None


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
        except Exception:  # noqa: BLE001 - dashboard mag niet omvallen op een prijsfout
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
    """Echte Bitvavo-balans (read-only key). Puur informatief; de bot handelt hier niet op."""
    secrets = get_secrets()
    if not secrets.bitvavo_api_key:
        return {"enabled": False, "assets": [], "total_eur": 0}
    feed = get_feed()
    try:
        balances = feed.get_balances()
    except Exception as exc:  # noqa: BLE001 - toon fout i.p.v. 500
        return {"enabled": False, "error": str(exc)[:200], "assets": [], "total_eur": 0}
    assets, total = [], 0.0
    for sym, amount in balances.items():
        if amount <= 0:
            continue
        if sym == "EUR":
            value = amount
        else:
            try:
                value = amount * feed.get_price(f"{sym}-EUR")
            except Exception:  # noqa: BLE001 - geen EUR-markt voor dit asset
                value = None
        assets.append({"symbol": sym, "amount": amount,
                       "value_eur": round(value, 2) if value is not None else None})
        total += value or 0.0
    assets.sort(key=lambda a: -(a["value_eur"] or 0))
    return {"enabled": True, "total_eur": round(total, 2), "assets": assets}


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
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:#1e293b;border-radius:8px;padding:16px}.card b{font-size:22px;display:block}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #334155}
h2{font-size:15px;margin:0 0 8px}.pos{color:#4ade80}.neg{color:#f87171}.muted{color:#94a3b8}
section{background:#1e293b;border-radius:8px;padding:16px;overflow-x:auto}
</style></head><body>
<header><h1>AI Trade Platform — paper trading</h1><span id="mode"></span></header>
<main>
<div class="cards" id="cards"></div>
<section><h2>Paper portfolio — open posities</h2><table id="positions"></table></section>
<section><h2>Echte Bitvavo-balans <span class="muted">(read-only, bot handelt hier niet op)</span></h2><table id="balance"></table></section>
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
const cls = n => n>=0?'pos':'neg';
async function load(){
  const [s, pf, bal] = await Promise.all([q('api/stats'), q('api/portfolio'), q('api/balance')]);
  document.getElementById('cards').innerHTML = [
    ['Paper portfolio (EUR)', fmt(pf.total_eur)],
    ['Cash (EUR)', fmt(pf.cash_eur)],
    ['Closed trades', s.closed_trades],
    ['Win-rate', s.win_rate_pct==null?'—':s.win_rate_pct+'%'],
    ['Netto P&L (EUR)', fmt(s.net_pnl_eur)],
    ['Totaal fees (EUR)', fmt(s.total_fees_eur)],
  ].map(([k,v])=>`<div class="card">${k}<b>${v}</b></div>`).join('');
  document.getElementById('positions').innerHTML =
    '<tr><th>markt</th><th>aantal</th><th>entry</th><th>nu</th><th>waarde</th><th>ongereal. P&L</th><th>SL</th><th>TP</th></tr>' +
    (pf.positions.length ? pf.positions.map(p=>`<tr><td>${p.market}</td><td>${p.amount.toFixed(6)}</td><td>${fmt(p.entry_price)}</td><td>${fmt(p.current_price)}</td><td>${fmt(p.value_eur)}</td><td class="${cls(p.unrealized_pnl_eur)}">${fmt(p.unrealized_pnl_eur)}</td><td>${fmt(p.stop_loss)}</td><td>${fmt(p.take_profit)}</td></tr>`).join('')
      : '<tr><td colspan="8" class="muted">geen open posities — de bot wacht op een signaal dat door alle gates komt</td></tr>');
  document.getElementById('balance').innerHTML = bal.enabled
    ? ('<tr><th>asset</th><th>aantal</th><th>waarde (EUR)</th></tr>' +
       bal.assets.map(a=>`<tr><td>${a.symbol}</td><td>${a.amount}</td><td>${fmt(a.value_eur)}</td></tr>`).join('') +
       `<tr><td><b>Totaal</b></td><td></td><td><b>${fmt(bal.total_eur)}</b></td></tr>`)
    : `<tr><td class="muted">${bal.error ? 'fout: '+bal.error : 'geen Bitvavo API-key geconfigureerd'}</td></tr>`;
  const sig = await q('api/signals?limit=20');
  document.getElementById('signals').innerHTML =
    '<tr><th>tijd</th><th>markt</th><th>signaal</th><th>besluit</th><th>reden</th></tr>' +
    sig.map(r=>`<tr><td>${r.ts.slice(0,16)}</td><td>${r.market}</td><td>${r.action}</td><td>${r.decision}</td><td>${r.reason}</td></tr>`).join('');
  const tr = await q('api/trades?limit=20');
  document.getElementById('trades').innerHTML =
    '<tr><th>tijd</th><th>markt</th><th>kant</th><th>prijs</th><th>fee</th><th>P&L</th></tr>' +
    tr.map(r=>`<tr><td>${r.ts.slice(0,16)}</td><td>${r.market}</td><td>${r.side}</td><td>${fmt(r.price)}</td><td>${fmt(r.fee_eur)}</td><td class="${cls(r.pnl_eur)}">${fmt(r.pnl_eur)}</td></tr>`).join('');
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
