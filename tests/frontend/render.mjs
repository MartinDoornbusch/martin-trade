/* Headless rendertest van het dashboard tegen een gemockte feed.
 *
 * Draait NIET in pytest en NIET in CI: hij heeft jsdom nodig en dat is de enige
 * npm-afhankelijkheid in dit project. Bewust zo gehouden; de Python-suite blijft
 * de poort waar de build op faalt. Handmatig draaien:
 *
 *     mkdir -p ~/dashboardtest && cd ~/dashboardtest && npm install jsdom
 *     node <repo>/tests/frontend/render.mjs <repo>
 *
 * jsdom wordt vanuit de WERKMAP opgelost, dus je draait hem vanuit de map waar je
 * jsdom hebt geinstalleerd en geeft de repo als argument mee.
 *
 * Sinds v0.25.2 bewijst hij het productiedashboard: geen meetgates, geen LLM-panelen,
 * geen run-banner meer, wel een banner zodra het om echt geld gaat of kopen is
 * gepauzeerd, en meetannotaties die uit de besluitentabel zijn weggefilterd. De
 * meetdata zelf blijft in de database en via de CLI-analyzers beschikbaar.
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

/* jsdom bewust vanuit de WERKMAP oplossen en niet met een gewone import: een
   ESM-import zoekt node_modules vanaf dit bestand omhoog, en dan moet je in de
   repo installeren. Zo blijft de enige npm-afhankelijkheid buiten het project. */
const { JSDOM } = createRequire(path.join(process.cwd(), 'noop.js'))('jsdom');

const ROOT = process.argv[2];
const S = p => fs.readFileSync(path.join(ROOT, 'src/tradebot/static', p), 'utf8');
const html = S('index.html');

const FEED = {
  'api/mode': { mode: 'paper', paused: false, version: '0.25.2', run_purpose: 'infrastructuurtest',
    run_until: '2026-09-15', live_max_capital_eur: 100, candle_interval: '4h', analysis_interval_minutes: 60,
    sizing: 'bucket', bucket_eur: 250, llm: { enabled: false, binding: false },
    gates: { veto: false, regime: false, breakeven: false, chase: false, timestop: false } },
  'api/stats': { net_pnl_eur: 110.77, closed_trades: 44, total_fees_eur: 58.25, win_rate_pct: 52.3,
    max_drawdown_pct: 10.7, llm_calls: 0, llm_veto_rate_pct: null,
    llm_calls_all: 121, llm_veto_rate_all_pct: 100.0, mode: 'paper' },
  'api/alfa': { error: null, alfa_pp: -3.4, run_return_pct: 9.12, exposure_pct: 88.0,
    benchmark_return_pct: 14.2, passive_expected_pct: 12.5, benchmark_max_decline_pct: 11.3,
    benchmark_markets: ['A-EUR', 'B-EUR', 'C-EUR'], reference_return_pct: 6.1 },
  'api/portfolio': { total_eur: 1000, cash_eur: 900, positions: [] },
  'api/balance': { assets: [], total_eur: 0 },
  'api/markets': [], 'api/advice': [], 'api/lists': { markets: [], watchlist: [], blocklist: [], paused: false },
  'api/equity': [], 'api/trades': [],
  'api/signals': [{ ts: '2026-10-18T06:00:00+00:00', market: 'SOL-EUR', action: 'buy', decision: 'buy',
    score: 3, reason: 'uptrend: EMA fast > slow | SHADOW-REGIME genegeerd: regime gate: BTC-EUR trend down' +
    ' | SHADOW-CHASE genegeerd: chase-guard: te ver' }],
  'api/llm': [],
  'api/llm/health': { enabled: false, binding: false, run_purpose: 'infrastructuurtest',
    active: 'groq', chain: [
      { order: 1, provider: 'groq', model: 'openai/gpt-oss-20b', key_present: true,
        used_today: 3, daily_budget: 200, status: 'fout', last_ok: null,
        last_error: { ts: '2026-09-02T08:00:00+00:00', model: 'llama-3.1-8b-instant',
                      http_status: 404, message: 'model_decommissioned' } },
      { order: 2, provider: 'gemini', model: 'gemini-2.5-flash', key_present: false,
        used_today: 0, daily_budget: 100, status: 'geen sleutel', last_ok: null, last_error: null },
    ] },
  'api/scanner': { results: [] },
  'api/veto-analysis': { summary: null, per_market: [] },
  'api/regime-analysis': { n_events: 13, n_deduped: 0, n_resolved: 12, n_positions: 13,
    n_open_positions: 1, target_resolved: 20, position_size_eur: 250,
    summary: { n: 12, n_avoided: 5, n_missed: 7, veto_precision_pct: 41.7, precision_lo_pct: 19.3,
      precision_hi_pct: 68.0, avoided_eur: 92.47, missed_eur: 147.32, net_gate_eur: -54.86 },
    per_market: [] },
  'api/breakeven-analysis': { n_events: 1332, n_deduped: 1145, n_resolved: 20, n_positions: 22,
    n_open_positions: 2, target_resolved: 20, trigger_atr: 1, offset_pct: 0, position_size_eur: 250,
    summary: { n: 20, n_avoided: 8, n_missed: 12, veto_precision_pct: 40.0, precision_margin_pp: 20.3,
      precision_lo_pct: 21.9, precision_hi_pct: 61.3, avoided_eur: 120, missed_eur: 230, net_gate_eur: -110 },
    per_market: [{ group: 'overige (elk n<5)', pooled_groups: 12, n: 20, n_avoided: 8, n_missed: 12,
      veto_precision_pct: 40.0, precision_lo_pct: 21.9, precision_hi_pct: 61.3,
      avoided_eur: 120, missed_eur: 230, net_gate_eur: -110 }] },
  'api/chase-analysis': { summary: null, per_market: [] },
};

const posts = [];
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true,
                              url: 'http://localhost:8000/' });
const { window } = dom;
const urls = [];
window.fetch = (url, opts) => {
  urls.push(String(url));
  /* Langste sleutel eerst: 'api/llm' is een prefix van 'api/llm/health' en zou
     anders het verkeerde antwoord teruggeven. */
  const key = Object.keys(FEED).sort((a, b) => b.length - a.length)
    .find(k => String(url).includes(k));
  if (opts && opts.method === 'POST') {
    posts.push({ url: String(url), body: JSON.parse(opts.body) });
    return Promise.resolve({ json: () => Promise.resolve({
      ok: false, provider: 'groq', model: 'openai/gpt-oss-20b', latency_ms: 120,
      http_status: 404, error: 'model_decommissioned', verdict: null }) });
  }
  return Promise.resolve({ json: () => Promise.resolve(FEED[key] ?? {}) });
};

const errors = [];
window.addEventListener('error', e => errors.push(String(e.error || e.message)));
window.HTMLCanvasElement.prototype.getContext = () => null;

/* uPlot gestubd: de echte bundel verwacht een levende layout-engine en dit is
   geen grafiektest. Wat hier bewezen moet worden is de providerkaart. */
window.eval('window.uPlot = function(){ return { setSize(){}, destroy(){}, width:600, height:200 }; };'
          + 'window.uPlot.paths = { bars: () => () => null };');
/* In één eval: `const eur/fmt/pct` uit charts.js zijn lexicale bindingen en
   overleven een losse indirecte eval niet, terwijl ze in de browser via twee
   script-tags wel in dezelfde globale scope staan. */
try { window.eval(S('charts.js') + '\n;\n' + S('app.js')); }
catch (e) { console.log('FOUT laden van de dashboard-JS: ' + (e && e.message)); process.exit(1); }

const checks = [];
const check = (naam, ok, extra = '') => checks.push({ naam, ok, extra });

await new Promise(r => setTimeout(r, 300));
const $ = id => window.document.getElementById(id);

const doc = window.document;
check('geen meetgates-tab', !doc.querySelector('[data-tab="gates"]') && !$('tab-gates'));
check('geen gate-status, LLM- of veto-panelen',
      !$('gatesum') && !$('llmhealth') && !$('llm') && !$('vetoanalysis') && !$('breakevenanalysis'));
check('geen gate- of LLM-calls meer vanuit het dashboard',
      !urls.some(u => /veto-analysis|regime-analysis|breakeven-analysis|chase-analysis|api\/llm/.test(u)));
check('besluiten zonder meetregels opgevraagd', urls.some(u => u.includes('api/signals') && u.includes('shadow=false')));
check('geen banner in paper zonder pauze, ook niet bij verlopen venster', $('runbanner').innerHTML === '');
check('nergens "infrastructuurtest" of "shadow" in beeld',
      !/infrastructuurtest|shadow/i.test(doc.body.textContent));
const sg = $('signals').textContent;
check('meetannotatie uit de besluitreden gefilterd',
      sg.includes('uptrend: EMA fast > slow') && !sg.includes('SHADOW') && !sg.includes('genegeerd'));
const kp = $('kpis').textContent;
check('geen veto-rate-tegel meer', !/veto/i.test(kp));
check('alfa-tegel', kp.includes('Alfa van de run') && kp.includes('-3,4 pp'));
check('markttegel met grootste daling', kp.includes('Markt ter vergelijking') && kp.includes('max. daling 11,3%'));
check('uitleg zonder shadow-stap', !$('tab-uitleg').textContent.includes('Shadow'));

const MD = JSON.stringify(FEED['api/mode']);
window.eval(`renderMode(Object.assign(${MD}, { paused: true }))`);
check('banner bij gepauzeerd kopen', $('runbanner').textContent.includes('Kopen staat gepauzeerd'));
window.eval(`renderMode(Object.assign(${MD}, { paused: false, mode: 'live' }))`);
check('banner bij live met plafond', $('runbanner').textContent.includes('LIVE') &&
      $('runbanner').textContent.includes('100') && $('runbanner').classList.contains('neg'));
check('geen JS-fouten', errors.length === 0, errors.join(' | '));

let bad = 0;
for (const c of checks) { if (!c.ok) bad++; console.log(`${c.ok ? 'ok  ' : 'FOUT'} ${c.naam}${c.extra ? ' :: ' + c.extra : ''}`); }
console.log(`\n${checks.length - bad}/${checks.length} checks geslaagd`);
process.exit(bad ? 1 : 0);
