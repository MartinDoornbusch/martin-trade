/* Dashboard-logica. Haalt de API-endpoints op en vult de panelen.
   Formatters en grafiekhelpers komen uit charts.js (dat eerder geladen wordt). */

const T = new URLSearchParams(location.search).get('token') || '';
/* Achter HA-ingress zit de app onder een prefix; alle URL's zijn daarom
   relatief aan het huidige pad. */
const B = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
const q = p => fetch(B + p + (p.includes('?') ? '&' : '?') + 'token=' + T).then(r => r.json());
const post = (p, body) => fetch(B + p + '?token=' + T, {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
}).then(r => r.json());

const $ = id => document.getElementById(id);
const html = (id, s) => { const el = $(id); if (el) el.innerHTML = s; };
const cls = n => (n >= 0 ? 'pos' : 'neg');
const esc = s => String(s == null ? '' : s).replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
const short = ts => String(ts).slice(0, 16).replace('T', ' ');

/* ---------- tabs ---------- */

$('tabs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-tab]');
  if (!btn) return;
  for (const b of $('tabs').querySelectorAll('button')) b.classList.toggle('on', b === btn);
  for (const s of document.querySelectorAll('.tab')) {
    s.classList.toggle('on', s.id === 'tab-' + btn.dataset.tab);
  }
  /* Een grafiek die in een verborgen tab is gemaakt kent zijn breedte niet;
     bij het tonen alsnog opmeten. */
  requestAnimationFrame(() => {
    for (const host of document.querySelectorAll('.chart')) {
      if (host._u && host.clientWidth > 1 && Math.abs(host.clientWidth - host._u.width) > 1) {
        host._u.setSize({ width: host.clientWidth, height: host._u.height });
      }
    }
  });
});

/* ---------- kop en run-banner ---------- */

let botMode = 'paper', botPaused = false, botVersion = '?', botCfg = {};

function renderMode(md) {
  botMode = md.mode; botPaused = md.paused; botVersion = md.version || '?'; botCfg = md;
  $('ver').textContent = 'v' + botVersion;

  const badge = $('modebadge');
  badge.textContent = md.mode + (md.paused ? ' · gepauzeerd' : '');
  badge.className = 'pill ' + (md.mode === 'live' ? 'neg' : (md.paused ? 'warn' : 'pos'));

  $('pausebtn').textContent = md.paused ? '▶ kopen hervatten' : '⏸ kill-switch';
  $('kpitag').textContent = md.mode;

  const sizing = md.sizing === 'bucket'
    ? eur(md.bucket_eur, 0) + ' per positie'
    : fmt(md.max_position_pct, 0) + '% per positie';
  $('runsub').textContent =
    `${md.candle_interval} candles · analyse elke ${md.analysis_interval_minutes} min · ${sizing}`;

  const bindend = Object.entries(md.gates || {}).filter(([, v]) => v).map(([k]) => k);
  $('gatecnt').textContent = bindend.length ? bindend.length + ' bindend' : 'shadow';

  renderBanner(md);
}

function renderBanner(md) {
  const el = $('runbanner');
  const doel = md.run_purpose || 'onbekend';
  const infra = /infrastructuur/i.test(doel);
  let rest = '';
  let verstreken = false;
  if (md.run_until) {
    const days = Math.round((Date.parse(md.run_until + 'T00:00:00Z') - Date.now()) / 864e5);
    verstreken = days < 0;
    rest = verstreken
      ? ` Venster tot <b>${md.run_until}</b> is <b>verstreken</b>: stop de run of verleng hem met een opgeschreven reden.`
      : ` Venster loopt tot <b>${md.run_until}</b> (nog ${days} dagen).`;
  }
  if (infra || verstreken) {
    el.className = 'banner ' + (verstreken ? 'neg' : 'warn');
    el.innerHTML = `<span class="ico">${verstreken ? '⛔' : '⚠'}</span><div>` +
      `Doel van deze run: <b>${esc(doel)}</b>.` +
      (infra ? ' Dit is <b>geen strategievalidatie</b>. De fase 2-vraag is beantwoord met "geen edge" ' +
        '(tweejarige backtest, stijgend én dalend venster, alfa gecorrigeerd voor blootstelling). ' +
        'Lees P&amp;L, win-rate en drawdown hieronder als uitvoeringscijfers, niet als bewijs van een edge.' : '') +
      rest + '</div>';
  } else {
    el.className = 'banner';
    el.innerHTML = `<span class="ico">●</span><div>Doel van deze run: <b>${esc(doel)}</b>.${rest}</div>`;
  }
}

$('pausebtn').onclick = async () => {
  const r = await post('api/pause', { paused: !botPaused });
  botPaused = r.paused;
  renderMode(Object.assign({}, botCfg, { paused: r.paused }));
};
$('refreshbtn').onclick = () => load();

/* ---------- kerncijfers ---------- */

function renderKpis(s, pf) {
  const feeShare = s.total_fees_eur && s.net_pnl_eur !== null
    ? `${fmt(Math.abs(s.total_fees_eur) / Math.max(1e-9, Math.abs(s.net_pnl_eur)) * 100, 0)}% van |netto|`
    : '';
  const unreal = pf.positions.reduce((a, p) => a + p.unrealized_pnl_eur, 0);
  const cards = [
    ['Portfolio', eur(pf.total_eur), `cash ${eur(pf.cash_eur)}`, ''],
    ['Netto P&L', eur(s.net_pnl_eur), `${s.closed_trades} gesloten trades`, cls(s.net_pnl_eur)],
    ['Fees betaald', eur(s.total_fees_eur), feeShare, 'neg'],
    ['Win-rate', s.win_rate_pct == null ? '—' : pct(s.win_rate_pct), 'van gesloten trades', ''],
    ['Max drawdown', s.max_drawdown_pct == null ? '—' : pct(s.max_drawdown_pct), 'piek naar dal', 'neg'],
    ['Slots', `${pf.open_positions}/${pf.max_positions}`, 'open posities', ''],
    ['Ongerealiseerd', eur(unreal), 'in open posities', cls(unreal)],
    ['LLM veto-rate', s.llm_veto_rate_pct == null ? '—' : pct(s.llm_veto_rate_pct),
      `${s.llm_calls} calls`, ''],
  ];
  html('kpis', cards.map(([k, v, sub, c]) =>
    `<div class="kpi"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${sub}</div></div>`).join(''));
  $('kpiscope').textContent = `alleen ${botMode}-modus`;
}

/* ---------- grafieken overzicht ---------- */

function renderEquity(eq) {
  if (!eq || eq.length < 2) { $('eqempty').style.display = ''; $('eqchart').style.display = 'none'; return; }
  $('eqempty').style.display = 'none'; $('eqchart').style.display = '';
  const x = eq.map(e => Date.parse(e.ts));
  const total = eq.map(e => e.total_eur);
  const cash = eq.map(e => e.cash_eur);
  const first = total[0], last = total[total.length - 1];
  const chg = first ? (last / first - 1) * 100 : 0;
  html('eqleg', legend([
    ['totaal', C.acc, eur(last)],
    ['cash', C.dim, eur(cash[cash.length - 1])],
    ['sinds start', chg >= 0 ? C.pos : C.neg, (chg >= 0 ? '+' : '') + pct(chg)],
  ]));
  lineChart($('eqchart'), {
    x, height: 240, yfmt: v => fmt(v, 0),
    series: [
      { label: 'totaal', data: total, color: C.acc, width: 2, fill: 'rgba(56,189,248,.10)', fmt: v => eur(v) },
      { label: 'cash', data: cash, color: C.dim, width: 1.2, dash: [4, 4], fmt: v => eur(v) },
    ],
  });
}

function renderFeeDrag(eq) {
  if (!eq || eq.length < 2) { $('feeempty').style.display = ''; $('feechart').style.display = 'none'; return; }
  $('feeempty').style.display = 'none'; $('feechart').style.display = '';
  const x = eq.map(e => Date.parse(e.ts));
  const base = eq[0].total_eur;
  const fees = eq.map(e => e.fees_cumulative_eur);
  const net = eq.map(e => e.total_eur - base);
  html('feeleg', legend([
    ['fees cumulatief', C.neg, eur(fees[fees.length - 1])],
    ['netto resultaat', C.pos, eur(net[net.length - 1])],
  ]));
  lineChart($('feechart'), {
    x, height: 240, yfmt: v => fmt(v, 0),
    series: [
      { label: 'netto', data: net, color: C.pos, width: 2, fmt: v => eur(v) },
      { label: 'fees', data: fees, color: C.neg, width: 2, fmt: v => eur(v) },
    ],
  });
}

/* ---------- posities en balans ---------- */

function renderPositions(pf) {
  $('posmax').textContent = pf.max_positions == null ? '' : `${pf.open_positions}/${pf.max_positions} slots`;
  html('positions',
    '<tr><th>markt</th><th class="num">aantal</th><th class="num">entry</th><th class="num">nu</th>' +
    '<th class="num">waarde</th><th class="num">ongereal.</th><th class="num">SL</th><th class="num">TP</th></tr>' +
    (pf.positions.length
      ? pf.positions.map(p => {
        const dist = p.entry_price ? (p.current_price / p.entry_price - 1) * 100 : 0;
        return `<tr><td><span class="dot ${cls(p.unrealized_pnl_eur)}"></span>${p.market}</td>` +
          `<td class="num dim">${p.amount.toFixed(6)}</td><td class="num">${fmtp(p.entry_price)}</td>` +
          `<td class="num">${fmtp(p.current_price)} <span class="${cls(dist)}">${dist >= 0 ? '+' : ''}${fmt(dist, 1)}%</span></td>` +
          `<td class="num">${eur(p.value_eur)}</td>` +
          `<td class="num ${cls(p.unrealized_pnl_eur)}">${eur(p.unrealized_pnl_eur)}</td>` +
          `<td class="num neg">${fmtp(p.stop_loss)}</td><td class="num pos">${fmtp(p.take_profit)}</td></tr>`;
      }).join('')
      : '<tr><td colspan="8" class="empty">geen open posities — de bot wacht op een setup die door alle gates komt</td></tr>'));
}

function renderBalance(bal) {
  if (!bal.enabled) {
    html('balance', `<tr><td class="empty">${bal.error ? 'fout: ' + esc(bal.error) : 'geen Bitvavo API-key geconfigureerd'}</td></tr>`);
    return;
  }
  let rows = '<tr><th>asset</th><th class="num">aantal</th><th class="num">waarde</th><th>aandeel</th></tr>' +
    bal.assets.map(a =>
      `<tr><td>${a.symbol}</td><td class="num dim">${a.amount}</td><td class="num">${eur(a.value_eur)}</td>` +
      `<td><div class="bar-wrap"><i class="bar" style="width:${Math.max(3, Math.min(100, a.share_pct))}%"></i>` +
      `<span class="dim">${fmt(a.share_pct, 1)}%</span></div></td></tr>`).join('');
  if (bal.dust) {
    rows += `<tr><td class="dim">overig (${bal.dust.count} assets &lt; € 1)</td><td></td>` +
      `<td class="num dim">${eur(bal.dust.value_eur)}</td><td></td></tr>`;
  }
  rows += `<tr><td><b>Totaal</b></td><td></td><td class="num"><b>${eur(bal.total_eur)}</b></td><td></td></tr>`;
  html('balance', rows);
}

/* ---------- markten ---------- */

let chartMarket = null;
let lastAdvice = [];

function renderMarkets(mkts) {
  const th = (label, title, num) =>
    `<th class="${num ? 'num' : ''}" title="${title}">${label}</th>`;
  html('markets',
    '<tr><th>markt</th>' +
    th('koers', 'laatste prijs', 1) +
    th('24h', 'koersverandering laatste 24 uur', 1) +
    th('RSI', 'momentum 0-100: onder 30 oversold, boven 70 overbought', 1) +
    th('trend', 'EMA-snel boven EMA-traag = uptrend', 0) +
    th('EMA-gap', 'verschil snelle en trage EMA in procenten; groter = sterkere trend', 1) +
    th('MACD-hist', 'momentum-omslag: van negatief naar positief is een koopconditie', 1) +
    th('ATR', 'gemiddelde beweging per candle; bepaalt de SL/TP-afstand', 1) + '</tr>' +
    mkts.map(m => m.error
      ? `<tr><td>${m.market}</td><td colspan="7" class="dim">${esc(m.error)}</td></tr>`
      : `<tr><td>${m.market}</td><td class="num">€ ${fmtp(m.price)}</td>` +
        `<td class="num ${cls(m.change_24h_pct)}">${fmt(m.change_24h_pct, 1)}%</td>` +
        `<td class="num">${fmt(m.rsi, 0)}</td>` +
        `<td class="${m.trend}">${m.trend === 'up' ? '▲ up' : '▼ down'}</td>` +
        `<td class="num">${fmt(m.ema_gap_pct)}%</td>` +
        `<td class="num ${cls(m.macd_hist)}">${fmt(m.macd_hist, 4)}</td>` +
        `<td class="num">${fmt(m.atr_pct, 1)}%</td></tr>`).join(''));
}

function renderAdvice(adv) {
  lastAdvice = adv;
  html('advice',
    '<tr><th>markt</th><th>type</th><th>advies</th><th class="num">score</th>' +
    '<th class="num" title="break-even-trefkans die deze setup nodig heeft om na kosten quitte te spelen">p*</th>' +
    '<th class="num">verw. move</th><th class="num">vereist</th><th class="num">correlatie</th><th>toelichting</th></tr>' +
    adv.map(a => a.error
      ? `<tr><td>${a.market}</td><td colspan="8" class="dim">${esc(a.error)}</td></tr>`
      : `<tr><td>${a.market}</td><td class="dim">${a.tradeable ? 'trade' : 'watch'}</td>` +
        `<td class="${a.advies.startsWith('instappen') ? 'pos' : (a.advies.startsWith('vermijden') ? 'neg' : 'muted')}">${a.advies}</td>` +
        `<td class="num">${a.score}/${a.score_needed}</td>` +
        `<td class="num ${a.breakeven_win_rate_pct > 50 ? 'neg' : 'pos'}">${a.breakeven_win_rate_pct == null ? '—' : pct(a.breakeven_win_rate_pct)}</td>` +
        `<td class="num ${a.fee_ok ? 'pos' : 'neg'}">${pct(a.expected_move_pct, 2)}</td>` +
        `<td class="num dim">${pct(a.min_edge_pct, 2)}</td>` +
        `<td class="num">${a.correlation == null ? '—' : fmt(a.correlation) + (a.correlation_with ? ' <span class="dim">(' + a.correlation_with + ')</span>' : '')}</td>` +
        `<td class="wrap">${esc((a.reasons || []).join('; ')) || '—'}</td></tr>`).join(''));
  renderScoreBox();
}

/* Signaalopbouw van de markt die in de grafiek staat. */
function renderScoreBox() {
  const a = lastAdvice.find(r => r.market === chartMarket);
  const box = $('scorebox');
  if (!a || a.error) {
    $('scoretag').textContent = chartMarket || '';
    box.innerHTML = '<div class="empty">geen adviesdata voor deze markt</div>';
    return;
  }
  $('scoretag').textContent = `${a.market} · score ${a.score}/${a.score_needed}`;
  const bars = Array.from({ length: a.score_needed }, (_, i) =>
    `<i class="bar" style="width:${100 / a.score_needed}%;opacity:${i < a.score ? 1 : .18}"></i>`).join('');
  const kv = (k, v, c) => `<div class="r" style="display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px solid #16203590;font-size:12.5px">` +
    `<span class="dim">${k}</span><b class="${c || ''}">${v}</b></div>`;
  box.innerHTML =
    `<div class="bar-wrap" style="margin-bottom:8px">${bars}</div>` +
    `<div class="${a.advies.startsWith('instappen') ? 'pos' : (a.advies.startsWith('vermijden') ? 'neg' : 'muted')}" ` +
    `style="font-size:14px;font-weight:600;margin-bottom:8px">${a.advies}</div>` +
    kv('trend', a.trend === 'up' ? '▲ up' : '▼ down', a.trend === 'up' ? 'pos' : 'neg') +
    kv('RSI', fmt(a.rsi, 0)) +
    kv('p* nodig', a.breakeven_win_rate_pct == null ? '—' : pct(a.breakeven_win_rate_pct),
      a.breakeven_win_rate_pct > 50 ? 'neg' : 'pos') +
    kv('verwachte move', pct(a.expected_move_pct, 2), a.fee_ok ? 'pos' : 'neg') +
    kv('vereist (kosten)', pct(a.min_edge_pct, 2)) +
    kv('hoogste correlatie', a.correlation == null ? '—' : fmt(a.correlation) + (a.correlation_with ? ' (' + a.correlation_with + ')' : '')) +
    `<div style="margin-top:9px">${(a.reasons || []).length
      ? a.reasons.map(r => `<span class="chip plain"><span class="dot pos"></span>${esc(r)}</span>`).join('')
      : '<span class="dim" style="font-size:12px">geen bevestigende condities</span>'}</div>`;
}

async function loadChart(market) {
  if (!market) return;
  chartMarket = market;
  const d = await q('api/chart?market=' + encodeURIComponent(market));
  if (d.detail) { html('chartleg', `<span class="neg">${esc(d.detail)}</span>`); return; }
  const last = d.close[d.close.length - 1];
  const first = d.close[0];
  const chg = first ? (last / first - 1) * 100 : 0;
  html('chartleg', legend([
    ['slot', last >= d.open[d.open.length - 1] ? C.pos : C.neg, fmtp(last)],
    [`EMA${d.ema_fast_period}`, C.acc, fmtp(d.ema_fast[d.ema_fast.length - 1])],
    [`EMA${d.ema_slow_period}`, C.warn, fmtp(d.ema_slow[d.ema_slow.length - 1])],
    ['periode', chg >= 0 ? C.pos : C.neg, (chg >= 0 ? '+' : '') + pct(chg)],
  ]));
  $('chartinfo').textContent =
    `${d.close.length} candles (${d.interval}) sinds ${dateNL(d.ts[0])}` + (d.position ? ' · positie open' : '');
  candleChart($('pricechart'), d, { height: 340 });
  renderScoreBox();
}

$('chartsel').onchange = e => loadChart(e.target.value);

function fillChartSel(l) {
  const sel = $('chartsel');
  const all = [...l.markets, ...l.watchlist];
  sel.innerHTML = all.map(m => `<option value="${m}"${m === chartMarket ? ' selected' : ''}>${m}</option>`).join('');
  if (!chartMarket && all.length) loadChart(all[0]);
}

function renderLists(l) {
  fillChartSel(l);
  const chip = (m, listName) => `<span class="chip">${m}` +
    (listName === 'watchlist' ? ` <button data-act="markets|${m}|add" title="promoveer naar trading">→ trade</button>` : '') +
    ((listName === 'markets' && l.markets.length <= 1) ? '' : ` <button data-act="${listName}|${m}|remove" title="verwijder">✕</button>`) +
    '</span>';
  let out =
    `<div class="chiprow"><b>Trading ${l.markets.length}/${l.max_markets}</b>` +
    l.markets.map(m => chip(m, 'markets')).join('') + '</div>' +
    `<div class="chiprow"><b>Watchlist ${l.watchlist.length}/${l.max_watchlist}</b>` +
    (l.watchlist.length ? l.watchlist.map(m => chip(m, 'watchlist')).join('') : '<span class="dim">leeg</span>') + '</div>';
  if (l.auto_fill && l.auto_fill.length) {
    out += `<div class="chiprow"><b>Auto-fill deze cyclus</b>` +
      l.auto_fill.map(m => `<span class="chip plain">${m}</span>`).join('') + '</div>';
  }
  if (l.quiet && l.quiet.length) {
    out += `<div class="chiprow"><b class="neg">Stil, overweeg naar watchlist</b>` +
      l.quiet.map(x => `<span class="chip plain">${x.market} <span class="dim">${x.days == null ? 'nooit' : x.days + 'd'}</span></span>`).join('') + '</div>';
  }
  html('listbox', out);
}

async function act(listName, market, action) {
  if (!market) return;
  const r = await post('api/lists', { list_name: listName, market: market.trim().toUpperCase(), action });
  const msg = $('listmsg');
  msg.textContent = r.message;
  msg.className = r.ok ? 'pos' : 'neg';
  renderLists(r.lists);
  if (r.ok) { $('addmarket').value = ''; load(); }
}

document.addEventListener('click', e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const [list, market, action] = btn.dataset.act.split('|');
  act(list, market, action);
});
$('addwatch').onclick = () => act('watchlist', $('addmarket').value, 'add');
$('addtrade').onclick = () => act('markets', $('addmarket').value, 'add');
$('scanrefresh').onclick = async () => {
  $('scanrefresh').textContent = 'bezig…';
  renderScanner(await q('api/scanner?refresh=true'));
  $('scanrefresh').textContent = 'nu scannen';
};

function renderScanner(sc) {
  if (sc.error) { html('scanner', `<tr><td class="empty">fout: ${esc(sc.error)}</td></tr>`); return; }
  const st = sc.stats
    ? `<tr><td colspan="10" class="dim" style="white-space:normal">Trechter: ${sc.stats.eur_markets} EUR-markten gescand → ` +
      `${sc.stats.liquid} door het liquiditeitsfilter (volume ≥ € ${Number(sc.stats.min_volume_eur).toLocaleString('nl-NL')}, ` +
      `spread ≤ ${sc.stats.max_spread_pct}%) → ${sc.stats.analyzed} geanalyseerd → top ${sc.stats.shown} getoond</td></tr>`
    : '';
  html('scanner', st +
    '<tr><th>markt</th><th class="num">24h volume</th><th class="num">spread</th><th class="num">score</th>' +
    '<th>trend</th><th class="num">RSI</th><th class="num">verw. move</th><th class="num">vereist</th><th>actie</th></tr>' +
    sc.results.map(r =>
      `<tr><td>${r.market}</td><td class="num dim">€ ${Number(r.volume_eur).toLocaleString('nl-NL')}</td>` +
      `<td class="num">${pct(r.spread_pct, 2)}</td><td class="num">${r.score}/${r.score_needed}</td>` +
      `<td class="${r.trend}">${r.trend === 'up' ? '▲' : '▼'}</td><td class="num">${fmt(r.rsi, 0)}</td>` +
      `<td class="num ${r.fee_ok ? 'pos' : 'neg'}">${pct(r.expected_move_pct, 2)}</td>` +
      `<td class="num dim">${pct(r.required_pct, 2)}</td><td>` +
      (r.in_markets ? '<span class="dim">in trading</span>'
        : r.in_watchlist ? `<span class="dim">watchlist</span> <button class="btn small" data-act="markets|${r.market}|add">→ trade</button>`
          : `<button class="btn small" data-act="watchlist|${r.market}|add">+ watch</button> ` +
            `<button class="btn small" data-act="markets|${r.market}|add">+ trade</button>`) +
      '</td></tr>').join(''));
}

/* ---------- handel ---------- */

function renderTrades(tr) {
  html('trades',
    '<tr><th>tijd</th><th>markt</th><th>kant</th><th class="num">prijs</th><th class="num">fee</th><th class="num">P&L</th></tr>' +
    (tr.length ? tr.map(r =>
      `<tr><td class="dim">${short(r.ts)}</td><td>${r.market}</td>` +
      `<td class="${r.side === 'buy' ? 'muted' : ''}">${r.side}</td>` +
      `<td class="num">${fmtp(r.price)}</td><td class="num neg">${fmt(r.fee_eur)}</td>` +
      `<td class="num ${r.side === 'sell' ? cls(r.pnl_eur) : 'dim'}">${r.side === 'sell' ? eur(r.pnl_eur) : '—'}</td></tr>`).join('')
      : '<tr><td colspan="6" class="empty">nog geen trades</td></tr>'));

  const sells = tr.filter(r => r.side === 'sell');
  const vals = sells.slice().reverse().map(r => r.pnl_eur);
  const labels = sells.slice().reverse().map(r => ({ short: r.market.split('-')[0], full: `${short(r.ts)} · ${r.market}` }));
  if (vals.length) {
    $('pnlempty').style.display = 'none'; $('pnlchart').style.display = '';
    barChart($('pnlchart'), { values: vals, labels, height: 240, yfmt: v => eur(v) });
  } else {
    $('pnlempty').style.display = ''; $('pnlchart').style.display = 'none';
  }

  const wins = vals.filter(v => v > 0), losses = vals.filter(v => v <= 0);
  const avg = a => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  const aw = avg(wins), al = avg(losses);
  const payoff = aw != null && al != null && al !== 0 ? Math.abs(aw / al) : null;
  const cards = [
    ['Gem. winnaar', aw == null ? '—' : eur(aw), `${wins.length} trades`, 'pos'],
    ['Gem. verliezer', al == null ? '—' : eur(al), `${losses.length} trades`, 'neg'],
    ['Payoff-ratio', payoff == null ? '—' : fmt(payoff), 'winnaar / verliezer', payoff != null && payoff >= 1 ? 'pos' : 'neg'],
    ['Grootste verlies', losses.length ? eur(Math.min(...losses)) : '—', 'één trade', 'neg'],
  ];
  html('execkpis', cards.map(([k, v, s, c]) =>
    `<div class="kpi"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${s}</div></div>`).join(''));
}

function renderSignals(sig) {
  html('signals',
    '<tr><th>tijd</th><th>markt</th><th>signaal</th><th>besluit</th><th>reden</th></tr>' +
    (sig.length ? sig.map(r =>
      `<tr><td class="dim">${short(r.ts)}</td><td>${r.market}</td><td>${r.action}</td>` +
      `<td class="${r.decision === 'executed' ? 'pos' : 'muted'}">${r.decision}</td>` +
      `<td class="wrap">${esc(r.reason)}</td></tr>`).join('')
      : '<tr><td colspan="5" class="empty">nog geen beslissingen vastgelegd</td></tr>'));
}

function renderLlm(llm) {
  $('llmtag').textContent = botCfg.gates && botCfg.gates.veto ? 'bindend' : 'shadow';
  html('llm',
    '<tr><th>tijd</th><th>provider</th><th>markt</th><th>verdict</th><th class="num">conf</th><th>reden</th></tr>' +
    (llm.length ? llm.map(r =>
      `<tr><td class="dim">${short(r.ts)}</td><td>${r.provider}</td><td>${r.market}</td>` +
      `<td class="${r.verdict === 'veto' ? 'neg' : 'pos'}">${r.verdict}</td>` +
      `<td class="num">${fmt(r.confidence)}</td><td class="wrap">${esc(r.reasoning)}</td></tr>`).join('')
      : '<tr><td colspan="6" class="empty">geen LLM-calls — de second opinion staat uit tijdens de infrastructuurtest</td></tr>'));
}

/* ---------- gates ---------- */

const gateRow = r =>
  '<tr><th>groep</th><th class="num">n</th><th class="num">precisie</th><th class="num">vermeden</th>' +
  '<th class="num">gemist</th><th class="num">netto gate</th></tr>' +
  r.map(x => `<tr><td>${esc(x.group)}</td><td class="num">${x.n}</td>` +
    `<td class="num">${pct(x.veto_precision_pct)} <span class="dim">±${fmt(x.precision_margin_pp, 1)}</span></td>` +
    `<td class="num pos">${eur(x.avoided_eur)}</td><td class="num neg">${eur(x.missed_eur)}</td>` +
    `<td class="num ${cls(x.net_gate_eur)}">${eur(x.net_gate_eur)}</td></tr>`).join('');

function summaryCards(s, title) {
  if (!s) {
    return `<div class="kpi"><div class="k">${title}</div><div class="s" style="margin-top:6px">` +
      'nog geen afgewikkelde uitkomst gekoppeld</div></div>';
  }
  const g = s.net_gate_eur;
  return [
    ['Netto gate', eur(g), g >= 0 ? 'voegt waarde toe' : 'kost geld', cls(g)],
    ['Precisie', pct(s.veto_precision_pct), `${s.n_avoided}/${s.n} ±${fmt(s.precision_margin_pp, 1)}pp`, ''],
    ['Vermeden verlies', eur(s.avoided_eur), 'gate had gelijk', 'pos'],
    ['Gemiste winst', eur(s.missed_eur), 'gate had ongelijk', 'neg'],
  ].map(([k, v, sub, c]) =>
    `<div class="kpi"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${sub}</div></div>`).join('');
}

function progressBar(done, target) {
  const p = Math.min(100, (done / Math.max(1, target)) * 100);
  return `<div class="progress ${done >= target ? 'full' : ''}"><i style="width:${p}%"></i></div>` +
    `<div class="s dim" style="font-size:11.5px">voortgang naar een uitspraak: <b>${done}/${target}</b> afgewikkelde trades` +
    `${done < target ? ' (nog te weinig)' : ' (genoeg voor een eerste uitspraak)'}</div>`;
}

const gateState = {};

function renderGate(elId, gate, d, opts) {
  const el = $(elId);
  if (!el) return;
  gateState[gate] = d;
  if (!d || d.error) {
    el.innerHTML = `<div class="empty">${d && d.error ? 'fout: ' + esc(d.error) : 'geen data'}</div>`;
    return;
  }
  const mode = d.binding ? 'bindend' : 'shadow';
  const tag = $(opts.tagId);
  if (tag) { tag.textContent = mode; tag.className = 'tag ' + (d.binding ? 'pos' : 'warn'); }
  if (!d.n_events) {
    el.innerHTML = `<div class="empty">${opts.leeg}</div>`;
    return;
  }
  const dedup = d.n_deduped ? ` · ${d.n_deduped} herhaalde treffers samengevoegd tot de eerste per positie` : '';
  el.innerHTML =
    `<div class="gatecard">${summaryCards(d.summary, 'Netto gate')}</div>` +
    progressBar(d.n_resolved || 0, d.target_resolved || 20) +
    `<p class="hint" style="margin-top:10px">${opts.params} · positie ${eur(d.position_size_eur)} · ` +
    `${d.n_events} events, ${d.n_unresolved} nog niet afgewikkeld${dedup}</p>` +
    ((d.per_market && d.per_market.length)
      ? '<div class="scroll"><table>' + gateRow(d.per_market) + '</table></div>' : '');
}

function renderVeto(d) {
  const el = $('vetoanalysis');
  gateState.veto = d;
  const tag = $('vetomode');
  tag.textContent = botCfg.gates && botCfg.gates.veto ? 'bindend' : 'shadow';
  tag.className = 'tag ' + (botCfg.gates && botCfg.gates.veto ? 'pos' : 'warn');
  if (!d || d.error) {
    el.innerHTML = `<div class="empty">${d && d.error ? 'fout: ' + esc(d.error) : 'geen data'}</div>`;
    return;
  }
  if (!d.n_vetos) {
    el.innerHTML = '<div class="empty">nog geen veto\'s om te analyseren — die verschijnen zodra een ' +
      'koopsignaal alle mechanische gates passeert en de LLM blokkeert</div>';
    return;
  }
  const suspect = d.suspect_reason_count
    ? `<p class="hint neg">⚠ ${d.suspect_reason_count} van de ${d.n_vetos} veto's blokkeren op ` +
      '"koers bij de onderste Bollinger-band", terwijl de strategie datzelfde signaal juist als koopreden telt. ' +
      'Richting-technisch tegenstrijdig.</p>'
    : '<p class="hint">Geen richting-verdachte veto-redenen gevonden.</p>';
  el.innerHTML =
    `<div class="gatecard">${summaryCards(d.real_outcome, 'Echte shadow-uitkomst')}</div>` +
    progressBar(d.n_real_matched || 0, d.target_resolved || 20) +
    `<p class="hint" style="margin-top:10px">${d.n_vetos} veto's · positie ${eur(d.position_size_eur)} · ` +
    `round-trip kosten ${pct(d.cost_pct, 2)} · config-scope <b>${esc(d.config_hash || 'alle')}</b></p>` +
    suspect +
    '<h2 style="font-size:12px;margin:12px 0 6px" class="dim">COUNTERFACTUAL PER VETO-REDEN</h2>' +
    '<div class="scroll"><table>' + gateRow(d.by_reason || []) + '</table></div>' +
    '<h2 style="font-size:12px;margin:12px 0 6px" class="dim">PER MARKT</h2>' +
    '<div class="scroll"><table>' + gateRow(d.by_market || []) + '</table></div>';
}

function renderGateSummary() {
  const rows = [
    ['LLM-veto', 'veto', gateState.veto, d => d && d.n_vetos, d => d && d.real_outcome, d => d && d.n_real_matched],
    ['Regime-filter', 'regime', gateState.regime, d => d && d.n_events, d => d && d.summary, d => d && d.n_resolved],
    ['Breakeven-stop', 'breakeven', gateState.breakeven, d => d && d.n_events, d => d && d.summary, d => d && d.n_resolved],
    ['Chase-guard', 'chase', gateState.chase, d => d && d.n_events, d => d && d.summary, d => d && d.n_resolved],
  ];
  html('gatesum',
    '<tr><th>gate</th><th>modus</th><th class="num">events</th><th class="num">afgewikkeld</th>' +
    '<th class="num">netto gate</th><th>oordeel</th></tr>' +
    rows.map(([label, key, d, nEvents, sum, resolved]) => {
      const binding = botCfg.gates ? botCfg.gates[key] : false;
      const s = sum(d);
      const n = nEvents(d) || 0;
      const done = resolved(d) || 0;
      const target = (d && d.target_resolved) || 20;
      let verdict = '<span class="dim">te weinig data</span>';
      if (s && done >= target) {
        verdict = s.net_gate_eur >= 0
          ? '<span class="pos">bewezen waardevol, kandidaat om bindend te maken</span>'
          : '<span class="neg">kost geld, kandidaat om te schrappen</span>';
      } else if (s) {
        verdict = `<span class="muted">richting ${s.net_gate_eur >= 0 ? 'positief' : 'negatief'}, nog niet hard</span>`;
      }
      return `<tr><td>${label}</td>` +
        `<td><span class="tag ${binding ? 'pos' : 'warn'}">${binding ? 'bindend' : 'shadow'}</span></td>` +
        `<td class="num">${n}</td><td class="num">${done}/${target}</td>` +
        `<td class="num ${s ? cls(s.net_gate_eur) : 'dim'}">${s ? eur(s.net_gate_eur) : '—'}</td>` +
        `<td class="wrap">${verdict}</td></tr>`;
    }).join(''));
}

/* ---------- orchestratie ---------- */

async function load() {
  const md = await q('api/mode');
  renderMode(md);

  const [s, pf, bal, mkts, adv, lst] = await Promise.all([
    q('api/stats'), q('api/portfolio'), q('api/balance'),
    q('api/markets'), q('api/advice'), q('api/lists'),
  ]);
  renderKpis(s, pf);
  renderPositions(pf);
  renderBalance(bal);
  renderMarkets(mkts);
  renderLists(lst);
  renderAdvice(adv);

  const [eq, tr, sig, llm] = await Promise.all([
    q('api/equity'), q('api/trades?limit=40'), q('api/signals?limit=40'), q('api/llm?limit=20'),
  ]);
  renderEquity(eq);
  renderFeeDrag(eq);
  renderTrades(tr);
  renderSignals(sig);
  renderLlm(llm);

  /* Traag en niet blokkerend: gate-analyses en scanner komen na. */
  q('api/veto-analysis').then(d => { renderVeto(d); renderGateSummary(); });
  q('api/regime-analysis').then(d => {
    renderGate('regimeanalysis', 'regime', d, {
      tagId: 'regimemode',
      params: `proxy <b>${(d && d.proxy_market) || 'BTC-EUR'}</b>`,
      leeg: 'nog geen regime-down entries om te meten — die verschijnen zodra de bot koopt terwijl de proxy in down-trend staat',
    });
    renderGateSummary();
  });
  q('api/breakeven-analysis').then(d => {
    renderGate('breakevenanalysis', 'breakeven', d, {
      tagId: 'bemode',
      params: `trigger <b>${fmt(d && d.trigger_atr, 2)}× ATR</b> · offset <b>${pct(d && d.offset_pct, 2)}</b>`,
      leeg: 'nog geen breakeven-treffers — die verschijnen zodra een positie ver genoeg in de winst stond en terugzakt tot de drempel',
    });
    renderGateSummary();
  });
  q('api/chase-analysis').then(d => {
    renderGate('chaseanalysis', 'chase', d, {
      tagId: 'chasemode',
      params: `drempel <b>${fmt(d && d.max_chase_atr, 2)}× ATR</b>`,
      leeg: 'nog geen chase-treffers — die verschijnen zodra de live prijs te ver van de signaalclose staat bij een koopsignaal',
    });
    renderGateSummary();
  });
  q('api/scanner').then(renderScanner);

  $('upd').textContent = 'bijgewerkt ' + new Date().toLocaleTimeString('nl-NL');
}

renderGateSummary();
load();
setInterval(load, 60000);
