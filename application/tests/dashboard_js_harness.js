// Runs src/niftypaper/dashboard/chart.js against a minimal in-memory DOM (no browser, no network) and reports what the page would show after each action.
// Usage: node dashboard_js_harness.js chart.js < {"data": <workspace JSON>, "symbols": [...], "actions": [...]}
'use strict';
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = fs.readFileSync(process.argv[2], 'utf8');

class El {
  constructor(id) { this.id = id; this.attrs = {}; this.children = []; this.listeners = {}; this.value = ''; this.className = ''; this.checked = false; this._text = ''; this.innerHTML = ''; }
  set textContent(v) { this._text = v; if (v === '') { this.children = []; } }
  get textContent() { return this._text; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { this.listeners[t] = f; }
  appendChild(c) { this.children.push(c); return c; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 760, height: 320 }; }
  fire(t, ev) { const e = Object.assign({ currentTarget: this, preventDefault() { this.prevented = true; } }, ev || {}); if (this.listeners[t]) { this.listeners[t](e); } return e; }
  lines() { return this.children.map(c => (c.children.length ? c.children.map(x => x.textContent).join(' | ') : c.textContent)); }
}

const els = {};
const get = id => (els[id] = els[id] || new El(id));
const data = get('workspace-data'); data._text = JSON.stringify(input.data);
const select = get('symbol'); select.value = input.data.default;
const lots = get('lots'); lots.value = '1';
const buy = new El('side-buy'); buy.value = 'BUY'; buy.checked = true;
const sell = new El('side-sell'); sell.value = 'SELL';
const wl = input.data.symbols.map(s => { const b = new El('wl-' + s); b.attrs['data-symbol'] = s; return b; });
const wins = input.data.windows.map(w => { const b = new El('win-' + w); b.attrs['data-window'] = w; if (w === '1h') { b.attrs['aria-current'] = 'true'; } return b; });
const classes = [];
global.window = { addEventListener() {} };
global.document = {
  documentElement: { classList: { add: c => classes.push(c) } },
  getElementById: get,
  querySelector: sel => (sel === 'input[name=side]:checked' ? (sell.checked ? sell : buy) : null),
  querySelectorAll: sel => (sel === '.wl-item' ? wl : sel === '.win' ? wins : sel === 'input[name=side]' ? [buy, sell] : []),
  createElement: () => new El('')
};
// eslint-disable-next-line no-eval
(0, eval)(code);

function snap() {
  const svg = get('chart-svg');
  return {
    js_class: classes.includes('js'), title: get('chart-title').textContent, kind: get('d-kind').textContent, strike: get('d-strike').textContent,
    premium: get('d-premium').textContent, bid: get('d-bid').textContent, ask: get('d-ask').textContent, lot: get('d-lot').textContent,
    summary: get('order-summary').lines(), readout: get('chart-readout').textContent, select: select.value,
    candles: (svg.innerHTML.match(/class="body /g) || []).length, grid: (svg.innerHTML.match(/class="grid"/g) || []).length,
    current: wins.filter(w => w.getAttribute('aria-current') === 'true').map(w => w.attrs['data-window']),
    watch: wl.filter(w => w.getAttribute('aria-current') === 'true').map(w => w.attrs['data-symbol']),
    crosshair: get('cross').getAttribute('visibility')
  };
}

const out = [{ step: 'start', ...snap() }];
for (const a of input.actions) {
  if (a.do === 'select') { select.value = a.value; select.fire('change'); }
  else if (a.do === 'watch') { wl.find(w => w.attrs['data-symbol'] === a.value).fire('click'); }
  else if (a.do === 'lots') { lots.value = a.value; lots.fire('input'); }
  else if (a.do === 'side') { buy.checked = a.value === 'BUY'; sell.checked = a.value === 'SELL'; (a.value === 'BUY' ? buy : sell).fire('change'); }
  else if (a.do === 'window') { wins.find(w => w.attrs['data-window'] === a.value).fire('click'); }
  else if (a.do === 'key') { get('chart-svg').fire('keydown', { key: a.value }); }
  else if (a.do === 'pointer') { get('chart-svg').fire('pointermove', { clientX: a.x, clientY: 100 }); }
  out.push({ step: JSON.stringify(a), ...snap() });
}
process.stdout.write(JSON.stringify(out));
