(function () {
  'use strict';
  /* Local only: reads the JSON data block in this page and draws. No network, no storage, no external script. All amounts shown here were calculated by the
     server (estimates only: the paper engine still decides every order); the chart draws a SIMULATED HISTORY made up for visualisation, never a quote. */
  var node = document.getElementById('workspace-data');
  if (!node) { return; }
  var D = JSON.parse(node.textContent);
  document.documentElement.classList.add('js');
  var sym = D.default, win = '1h', idx = null;
  var $ = function (id) { return document.getElementById(id); };
  var select = $('symbol'), lots = $('lots'), svg = $('chart-svg'), readout = $('chart-readout');
  var W = 760, H = 320, ML = 8, MR = 62, MT = 10, MB = 26;   /* W and H follow the chart's width on screen, so text keeps its size on a phone */

  function money(s) {
    var p = String(s).split('.');
    return '₹' + p[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (p.length > 1 ? '.' + p[1] : '');
  }
  function text(id, value) { var e = $(id); if (e) { e.textContent = value; } }

  function renderDetails() {
    var d = D.details[sym];
    text('chart-title', d.symbol + ' — ' + d.title);
    text('d-kind', d.kind_text);
    text('d-strike', d.strike_text);
    text('d-premium', money(d.premium));
    text('d-bid', money(d.bid));
    text('d-ask', money(d.ask));
    text('d-lot', '1 synthetic lot = ' + d.lot_size + ' simulator units');
    var items = document.querySelectorAll('.wl-item');
    for (var i = 0; i < items.length; i++) {
      if (items[i].getAttribute('data-symbol') === sym) { items[i].setAttribute('aria-current', 'true'); } else { items[i].removeAttribute('aria-current'); }
    }
  }

  function candles() { return D.history[sym][win]; }

  function draw() {
    var box = svg.getBoundingClientRect();
    W = Math.max(240, Math.round(box.width) || 760); H = W < 520 ? Math.round(W * 0.85) : Math.round(W * 0.42);
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H); svg.setAttribute('height', H);
    var c = candles(), n = c.length, lo = Infinity, hi = -Infinity, i;
    for (i = 0; i < n; i++) { lo = Math.min(lo, +c[i][3]); hi = Math.max(hi, +c[i][2]); }
    var pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
    var pw = W - ML - MR, ph = H - MT - MB;
    var y = function (v) { return MT + (hi - v) / (hi - lo) * ph; };
    var x = function (k) { return ML + (k + 0.5) * pw / n; };
    var bw = Math.max(2, pw / n * 0.6), g = '';
    for (i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * i / 4, gy = y(v).toFixed(1);
      g += '<line class="grid" x1="' + ML + '" x2="' + (W - MR) + '" y1="' + gy + '" y2="' + gy + '"/><text class="axis" x="' + (W - MR + 6) + '" y="' + (+gy + 4) + '">' + v.toFixed(2) + '</text>';
    }
    for (i = 0; i < n; i++) {
      var k = c[i], up = +k[4] >= +k[1], cx = x(i).toFixed(1);
      g += '<line class="wick ' + (up ? 'up' : 'dn') + '" x1="' + cx + '" x2="' + cx + '" y1="' + y(+k[2]).toFixed(1) + '" y2="' + y(+k[3]).toFixed(1) + '"/>' +
           '<rect class="body ' + (up ? 'up' : 'dn') + '" x="' + (x(i) - bw / 2).toFixed(1) + '" y="' + y(Math.max(+k[1], +k[4])).toFixed(1) + '" width="' + bw.toFixed(1) +
           '" height="' + Math.max(1, Math.abs(y(+k[1]) - y(+k[4]))).toFixed(1) + '"/>';
    }
    var first = c[0][0], mid = c[Math.floor(n / 2)][0];
    g += '<text class="axis" x="' + ML + '" y="' + (H - 8) + '">-' + (first + (win === '30m' ? 1 : 5)) + 'm</text><text class="axis" text-anchor="middle" x="' + (ML + pw / 2) + '" y="' + (H - 8) +
         '">-' + mid + 'm</text><text class="axis" text-anchor="end" x="' + (W - MR) + '" y="' + (H - 8) + '">now</text>';
    g += '<g id="cross" visibility="hidden"><line class="cl" id="cl-v" y1="' + MT + '" y2="' + (MT + ph) + '"/><line class="cl" id="cl-h" x1="' + ML + '" x2="' + (W - MR) + '"/></g>';
    svg.innerHTML = g;
    if (idx === null || idx >= n) { idx = n - 1; }
    svg._geo = { n: n, x: x, y: y, ml: ML, pw: pw };
    mark(idx, false);
  }

  function mark(i, show) {
    var c = candles(), n = c.length, k;
    idx = Math.max(0, Math.min(n - 1, i)); k = c[idx];
    var geo = svg._geo;
    var cross = $('cross');
    if (cross && geo) {
      var cx = geo.x(idx), cy = geo.y(+k[4]);
      $('cl-v').setAttribute('x1', cx); $('cl-v').setAttribute('x2', cx);
      $('cl-h').setAttribute('y1', cy); $('cl-h').setAttribute('y2', cy);
      cross.setAttribute('visibility', show ? 'visible' : 'hidden');
    }
    var when = k[0] === 0 ? 'latest candle' : k[0] + ' min ago';
    text('chart-readout', 'SIMULATED HISTORY · ' + sym + ' · ' + win + ' window · ' + when + ' · open ' + k[1] + ' high ' + k[2] + ' low ' + k[3] + ' close ' + k[4] +
         ' (' + (+k[4] >= +k[1] ? 'up' : 'down') + ' candle)');
  }

  function fromPointer(ev) {
    var box = svg.getBoundingClientRect(), g = svg._geo;
    if (!g || !box.width) { return; }
    var vx = (ev.clientX - box.left) / box.width * W;
    mark(Math.floor((vx - g.ml) / g.pw * g.n), true);
  }

  function row(parent, label, value, cls) {
    var r = document.createElement('div'), a = document.createElement('span'), b = document.createElement('strong');
    r.className = 'sum-row' + (cls ? ' ' + cls : '');
    a.textContent = label; b.textContent = value; r.appendChild(a); r.appendChild(b); parent.appendChild(r);
  }
  function line(parent, cls, value) { var p = document.createElement('p'); p.className = cls; p.textContent = value; parent.appendChild(p); }

  function renderOrder() {
    var box = $('order-summary'), o = D.order;
    if (!box || !o) { return; }
    var side = document.querySelector('input[name=side]:checked'); side = side ? side.value : 'BUY';
    var raw = lots.value.trim(), n = /^\d{1,3}$/.test(raw) ? parseInt(raw, 10) : 0;
    var t = o.symbols[sym], d = D.details[sym], desc = d.kind_text + ', strike ' + d.strike_text;
    box.textContent = '';
    if (n < 1) { line(box, 'sum-bad', 'Enter a whole number of lots (1 or more).'); return; }
    row(box, side + ' ' + n + (n === 1 ? ' lot' : ' lots'), (n * d.lot_size) + ' units of ' + desc);
    if (side === 'BUY') {
      if (n > o.max_lots) { line(box, 'sum-bad', 'More than the per-trade limit of ' + o.max_lots + ' lots: the engine will refuse this order.'); return; }
      var e = t.buy[String(n)];
      row(box, 'Estimated order value', money(e.value));
      row(box, 'Estimated fee', money(e.fee));
      row(box, 'Slippage allowance (already in the value)', money(e.slippage));
      row(box, 'Estimated exit fee', money(e.exit_fee));
      row(box, 'Counts against the risk budget', money(e.risk), 'total');
      row(box, 'Remaining daily risk budget', money(o.headroom));
      if (o.limit_reached) { line(box, 'sum-bad', '✗ The daily loss limit is reached: new buys are refused. Exits are still allowed.'); }
      else if (!e.fits) { line(box, 'sum-bad', '✗ Over the daily risk budget by ' + money(e.excess) + ': the engine will refuse this order.'); }
      else if (t.new_position_blocked) { line(box, 'sum-bad', '✗ The limit of ' + o.max_open + ' open positions is reached: the engine will refuse a new contract.'); }
      else if (!e.exposure_ok) { line(box, 'sum-bad', '✗ This would take open premium above the exposure limit: the engine will refuse it.'); }
      else if (!e.cash_ok) { line(box, 'sum-bad', '✗ Not enough virtual cash for the value and fee.'); }
      else { line(box, 'sum-good', '✓ Appears to fit the daily risk budget and the other limits. The engine still decides.'); }
    } else {
      row(box, 'Available to sell', t.held_lots + (t.held_lots === 1 ? ' lot' : ' lots') + ' (' + t.held_units + ' units)');
      if (t.held_lots < 1) { line(box, 'sum-bad', '✗ You hold none of this contract. SELL only closes an existing virtual position; short selling is not simulated, so the engine will refuse this.'); return; }
      if (n > t.held_lots) { line(box, 'sum-bad', '✗ You hold ' + t.held_lots + (t.held_lots === 1 ? ' lot' : ' lots') + ': selling ' + n + ' would be refused.'); return; }
      var s = t.sell[String(n)];
      row(box, 'Estimated proceeds (before fee)', money(s.proceeds));
      row(box, 'Estimated fee', money(s.fee));
      row(box, 'Slippage allowance (already in the proceeds)', money(s.slippage));
      line(box, 'sum-good', '✓ Closes part or all of your existing position. Exits are not blocked by the daily risk budget.');
    }
  }

  function setSymbol(next) {
    if (!D.details[next]) { return; }
    sym = next; select.value = next; idx = null; renderDetails(); draw(); renderOrder();
  }

  select.addEventListener('change', function () { setSymbol(select.value); });
  lots.addEventListener('input', renderOrder);
  var sides = document.querySelectorAll('input[name=side]');
  for (var s = 0; s < sides.length; s++) { sides[s].addEventListener('change', renderOrder); }
  var items = document.querySelectorAll('.wl-item');
  for (var w = 0; w < items.length; w++) { items[w].addEventListener('click', function (ev) { setSymbol(ev.currentTarget.getAttribute('data-symbol')); }); }
  var wins = document.querySelectorAll('.win');
  for (var b = 0; b < wins.length; b++) {
    wins[b].addEventListener('click', function (ev) {
      win = ev.currentTarget.getAttribute('data-window'); idx = null;
      for (var j = 0; j < wins.length; j++) { if (wins[j] === ev.currentTarget) { wins[j].setAttribute('aria-current', 'true'); } else { wins[j].removeAttribute('aria-current'); } }
      draw();
    });
  }
  svg.addEventListener('pointermove', fromPointer);
  svg.addEventListener('pointerdown', fromPointer);
  svg.addEventListener('pointerleave', function () { mark(idx, false); });
  svg.addEventListener('keydown', function (ev) {
    var n = candles().length, k = ev.key;
    if (k === 'ArrowLeft') { mark(idx - 1, true); } else if (k === 'ArrowRight') { mark(idx + 1, true); }
    else if (k === 'Home') { mark(0, true); } else if (k === 'End') { mark(n - 1, true); } else { return; }
    ev.preventDefault();
  });
  window.addEventListener('resize', function () { draw(); });
  select.value = sym; renderDetails(); draw(); renderOrder();
}());
