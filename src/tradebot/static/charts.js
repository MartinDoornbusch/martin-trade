/* Grafiek-laag boven uPlot (gevendord in static/vendor).
   Canvas kan geen CSS-variabelen lezen, dus het palet staat hier nogmaals;
   houd het gelijk aan app.css. */

const C = {
  acc: '#38bdf8', acc2: '#818cf8',
  pos: '#34d399', neg: '#fb7185', warn: '#fbbf24',
  txt: '#e6edf7', sub: '#8b9cb8', dim: '#5b6c88',
  grid: '#182339', axis: '#233149',
};

const FONT = '11px ui-sans-serif, system-ui, "Segoe UI", sans-serif';

/* --- getalopmaak ------------------------------------------------------- */

const fmt = (n, d = 2) => n == null || Number.isNaN(n)
  ? '—'
  : Number(n).toLocaleString('nl-NL', { minimumFractionDigits: d, maximumFractionDigits: d });

/* Koersformatter: schaalt decimalen mee met de magnitude, zodat sub-cent coins
   (bv. PUMP) niet als 0,00 tonen. */
const fmtp = n => {
  if (n == null || Number.isNaN(n)) return '—';
  const a = Math.abs(n);
  if (a === 0) return fmt(0);
  const d = a >= 1 ? 2 : Math.min(8, Math.max(2, 2 - Math.floor(Math.log10(a))));
  return Number(n).toLocaleString('nl-NL', { minimumFractionDigits: d, maximumFractionDigits: d });
};

const eur = (n, d = 2) => n == null ? '—' : '€ ' + fmt(n, d);
const pct = (n, d = 1) => n == null ? '—' : fmt(n, d) + '%';

const dateNL = ts => new Date(ts).toLocaleDateString('nl-NL', { day: 'numeric', month: 'short' });
const stampNL = ts => new Date(ts).toLocaleString('nl-NL',
  { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

/* --- gedeelde opties --------------------------------------------------- */

const axisX = {
  stroke: C.dim, font: FONT, size: 30, space: 74,
  grid: { stroke: C.grid, width: 1 },
  ticks: { stroke: C.axis, width: 1, size: 4 },
  values: (u, splits) => splits.map(t => dateNL(t * 1000)),
};

const axisY = yfmt => ({
  stroke: C.dim, font: FONT, size: 62,
  grid: { stroke: C.grid, width: 1 },
  ticks: { show: false },
  values: (u, splits) => splits.map(yfmt),
});

/* Tooltip-plugin. `rows(u, i)` levert [[label, waarde, kleur], ...]. */
function tooltip(rows) {
  let box;
  return {
    hooks: {
      init: u => {
        box = document.createElement('div');
        box.className = 'u-tooltip';
        box.style.display = 'none';
        u.over.appendChild(box);
      },
      setCursor: u => {
        const i = u.cursor.idx;
        if (i == null || u.cursor.left < 0 || u.cursor.top < 0) {
          box.style.display = 'none';
          return;
        }
        box.innerHTML = rows(u, i)
          .map(([k, v, c]) => `<div class="r"><span>${k}</span><b style="color:${c || C.txt}">${v}</b></div>`)
          .join('');
        box.style.display = 'block';
        box.style.left = u.cursor.left + 'px';
        box.style.top = Math.max(28, u.cursor.top) + 'px';
      },
    },
  };
}

/* Maakt de grafiek en houdt hem op de breedte van zijn container. Een tweede
   aanroep op dezelfde host vervangt de vorige grafiek (geen stapeling bij de
   ververs-cyclus van 60s). */
function mount(host, opts, data) {
  if (host._u) { host._ro && host._ro.disconnect(); host._u.destroy(); host._u = null; }
  const w = Math.max(220, host.clientWidth || 600);
  const u = new uPlot(Object.assign({ width: w, legend: { show: false } }, opts), data, host);
  host._u = u;
  host._ro = new ResizeObserver(() => {
    const nw = Math.max(220, host.clientWidth || 600);
    if (Math.abs(nw - u.width) > 1) u.setSize({ width: nw, height: opts.height });
  });
  host._ro.observe(host);
  return u;
}

const cursorOpts = { points: { show: false }, drag: { x: false, y: false }, y: false };

/* --- lijn-/vlakgrafiek -------------------------------------------------- */

/* series: [{label, data, color, fill, dash, fmt}] ; x in ms */
function lineChart(host, { x, series, height = 220, yfmt = (v => fmt(v, 0)), xIsTime = true }) {
  const xs = x.map(t => t / 1000);
  const opts = {
    height,
    cursor: cursorOpts,
    scales: { x: { time: xIsTime } },
    axes: [axisX, axisY(yfmt)],
    series: [
      {},
      ...series.map(s => ({
        label: s.label,
        stroke: s.color,
        width: s.width || 2,
        dash: s.dash,
        fill: s.fill,
        points: { show: false },
      })),
    ],
    plugins: [tooltip((u, i) => [
      ['', stampNL(x[i]), C.sub],
      ...series.map((s, k) => [s.label, (s.fmt || yfmt)(u.data[k + 1][i]), s.color]),
    ])],
  };
  return mount(host, opts, [xs, ...series.map(s => s.data)]);
}

/* --- candlestick + EMA -------------------------------------------------- */

/* d: payload van /api/chart. levels: [{value, color, label}] */
function candleChart(host, d, { height = 320 } = {}) {
  const xs = d.ts.map(t => t / 1000);
  const levels = [];
  if (d.position) {
    levels.push({ v: d.position.take_profit, c: C.pos, t: 'TP' });
    levels.push({ v: d.position.entry, c: C.sub, t: 'entry' });
    levels.push({ v: d.position.stop_loss, c: C.neg, t: 'SL' });
  }
  let lo = Math.min(...d.low), hi = Math.max(...d.high);
  for (const l of levels) { lo = Math.min(lo, l.v); hi = Math.max(hi, l.v); }
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.06;

  /* De OHLC-reeksen worden niet als lijn getekend (paths -> null) maar tellen
     wel mee voor de schaal en voor de tooltip. De candles zelf komen uit de
     draw-hook hieronder. */
  const silent = { paths: () => null, points: { show: false } };

  const drawCandles = u => {
    const ctx = u.ctx;
    const [i0, i1] = u.series[0].idxs;
    const [, O, H, L, K] = u.data;
    const n = Math.max(1, i1 - i0 + 1);
    const dpr = devicePixelRatio || 1;
    const w = Math.max(1, Math.min(14 * dpr, (u.bbox.width / n) * 0.62));
    const lw = Math.max(1, Math.round(dpr));
    ctx.save();
    ctx.beginPath();
    ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
    ctx.clip();
    for (let i = i0; i <= i1; i++) {
      if (O[i] == null) continue;
      const up = K[i] >= O[i];
      const col = up ? C.pos : C.neg;
      const x = Math.round(u.valToPos(u.data[0][i], 'x', true));
      ctx.strokeStyle = col;
      ctx.fillStyle = col;
      ctx.lineWidth = lw;
      ctx.beginPath();
      ctx.moveTo(x, u.valToPos(H[i], 'y', true));
      ctx.lineTo(x, u.valToPos(L[i], 'y', true));
      ctx.stroke();
      const yo = u.valToPos(O[i], 'y', true), yk = u.valToPos(K[i], 'y', true);
      const top = Math.min(yo, yk), h = Math.max(1.5 * dpr, Math.abs(yk - yo));
      if (up) { ctx.globalAlpha = 0.22; ctx.fillRect(x - w / 2, top, w, h); ctx.globalAlpha = 1;
                ctx.strokeRect(x - w / 2, top, w, h); }
      else { ctx.fillRect(x - w / 2, top, w, h); }
    }
    ctx.restore();
  };

  const drawLevels = u => {
    if (!levels.length) return;
    const ctx = u.ctx;
    const dpr = devicePixelRatio || 1;
    ctx.save();
    ctx.setLineDash([5 * dpr, 4 * dpr]);
    ctx.lineWidth = Math.max(1, Math.round(dpr));
    ctx.font = `${11 * dpr}px ui-sans-serif, system-ui, sans-serif`;
    ctx.textAlign = 'right';
    for (const l of levels) {
      const y = Math.round(u.valToPos(l.v, 'y', true));
      ctx.strokeStyle = l.c;
      ctx.beginPath();
      ctx.moveTo(u.bbox.left, y);
      ctx.lineTo(u.bbox.left + u.bbox.width, y);
      ctx.stroke();
      ctx.fillStyle = l.c;
      ctx.fillText(`${l.t} ${fmtp(l.v)}`, u.bbox.left + u.bbox.width - 4 * dpr, y - 4 * dpr);
    }
    ctx.restore();
  };

  const opts = {
    height,
    cursor: cursorOpts,
    scales: { x: { time: true }, y: { range: () => [lo - pad, hi + pad] } },
    axes: [axisX, axisY(v => fmtp(v))],
    series: [
      {},
      Object.assign({ label: 'open' }, silent),
      Object.assign({ label: 'hoog' }, silent),
      Object.assign({ label: 'laag' }, silent),
      Object.assign({ label: 'slot' }, silent),
      { label: `EMA${d.ema_fast_period}`, stroke: C.acc, width: 1.6, points: { show: false } },
      { label: `EMA${d.ema_slow_period}`, stroke: C.warn, width: 1.6, points: { show: false } },
    ],
    hooks: { draw: [drawCandles, drawLevels] },
    plugins: [tooltip((u, i) => {
      const o = d.open[i], h = d.high[i], l = d.low[i], k = d.close[i];
      const col = k >= o ? C.pos : C.neg;
      return [
        ['', stampNL(d.ts[i]), C.sub],
        ['open', fmtp(o)], ['hoog', fmtp(h)], ['laag', fmtp(l)],
        ['slot', fmtp(k), col],
        [`EMA${d.ema_fast_period}`, fmtp(d.ema_fast[i]), C.acc],
        [`EMA${d.ema_slow_period}`, fmtp(d.ema_slow[i]), C.warn],
      ];
    })],
  };
  return mount(host, opts,
    [xs, d.open, d.high, d.low, d.close, d.ema_fast, d.ema_slow]);
}

/* --- staafgrafiek (P&L per trade) -------------------------------------- */

/* values: array met + en -; labels: tekst per staaf voor de tooltip */
function barChart(host, { values, labels, height = 220, yfmt = (v => fmt(v, 0)) }) {
  const xs = values.map((_, i) => i);
  const win = values.map(v => (v > 0 ? v : null));
  const los = values.map(v => (v <= 0 ? v : null));
  const bars = uPlot.paths.bars({ size: [0.68, 18] });
  const opts = {
    height,
    cursor: cursorOpts,
    scales: { x: { time: false } },
    axes: [
      Object.assign({}, axisX, {
        values: (u, splits) => splits.map(i => (labels[i] ? labels[i].short : '')),
        space: 58,
      }),
      axisY(yfmt),
    ],
    series: [
      {},
      { label: 'winst', stroke: C.pos, fill: C.pos + 'cc', paths: bars, points: { show: false } },
      { label: 'verlies', stroke: C.neg, fill: C.neg + 'cc', paths: bars, points: { show: false } },
    ],
    plugins: [tooltip((u, i) => (labels[i] ? [
      ['', labels[i].full, C.sub],
      ['P&L', yfmt(values[i]), values[i] >= 0 ? C.pos : C.neg],
    ] : []))],
  };
  return mount(host, opts, [xs, win, los]);
}

/* --- legenda onder de titel -------------------------------------------- */

function legend(items) {
  return items.map(([label, color, value]) =>
    `<span><i style="background:${color}"></i>${label}${value != null ? ' <b>' + value + '</b>' : ''}</span>`
  ).join('');
}
