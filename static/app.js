(() => {
  const $ = (s) => document.querySelector(s);
  const fmt = (n, d = 1) => (n == null || Number.isNaN(n) ? '-' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: d, minimumFractionDigits: d }));
  const inr = (n) => (n == null ? '-' : '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 }));
  const pct = (n) => (n == null ? '-' : `<span class="${n >= 0 ? 'pos' : 'neg'}">${n >= 0 ? '+' : ''}${fmt(n)}%</span>`);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const ist = (d) => new Date(d).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) + ' IST';

  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    const body = await r.json().catch(() => ({}));
    if (r.status === 401) { location.href = '/login?next=/app'; throw new Error('Please log in'); }
    if (!r.ok) throw new Error(body.detail || r.statusText);
    return body;
  }

  let toastTimer;
  function toast(msg, err = false) {
    const t = $('#toast');
    t.textContent = msg; t.className = 'toast' + (err ? ' err' : '');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), 6000);
  }

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + name));
    if (name === 'history') loadHistory();
    if (name === 'settings') loadSettings();
    if (name === 'performance') loadPerformance();
  }
  document.querySelectorAll('nav button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  document.addEventListener('click', (e) => { const g = e.target.closest('[data-goto]'); if (g) { e.preventDefault(); showTab(g.dataset.goto); } });

  // ---------------------------------------------------------------- terminal
  const term = { lines: [], last: '' };
  function termLog(text, cls = '') {
    if (text === term.last) return;
    term.last = text;
    const t = new Date().toLocaleTimeString('en-IN', { hour12: false });
    term.lines.push(`<div class="ln ${cls}"><span class="t">${t}</span>${esc(text)}</div>`);
    if (term.lines.length > 60) term.lines.shift();
    const el = $('#term'); el.classList.remove('hidden');
    el.innerHTML = term.lines.join('');
    el.lastElementChild && el.lastElementChild.classList.add('cur');
    el.scrollTop = el.scrollHeight;
  }

  // ---------------------------------------------------------------- cells
  const fmtDay = (d) => (d ? new Date(d + 'T00:00:00').toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' }) : '-');
  const ACT = { buy: 'Buy', wait: 'Wait', avoid: 'Avoid' };
  const VERDICT = { positive: 'positive', neutral: 'neutral', negative: 'negative', no_news: 'no news', not_checked: 'not checked', error: 'error' };
  function sentBadge(r) {
    const v = r.sentiment_verdict || 'not_checked';
    const sc = r.sentiment_score;
    const bar = sc == null ? '' : `<span class="bar"><i class="${sc >= 0 ? 'p' : 'n'}" style="width:${Math.min(Math.abs(sc), 1) * 50}%"></i></span>`;
    return `<span class="badge ${v}">${VERDICT[v] || v}</span> ${sc == null ? '' : `<span class="${sc > 0 ? 'pos' : sc < 0 ? 'neg' : 'neu'}">${sc >= 0 ? '+' : ''}${fmt(sc, 2)}</span>`}${bar}`;
  }
  function headlinesCell(r, n = 3) {
    const h = (r.headlines || []).slice(0, n);
    if (!h.length) return `<span class="dim">${r.sentiment_verdict === 'not_checked' ? 'not checked yet' : r.sentiment_n ? 'scored in bulk, no headlines kept' : 'no recent articles'}</span>`;
    return `<ul class="headlines">${h.map((x) => `<li>${x.sentiment != null ? `<span class="${x.sentiment > 0.1 ? 'pos' : x.sentiment < -0.1 ? 'neg' : 'neu'}">${x.sentiment >= 0 ? '+' : ''}${fmt(x.sentiment, 2)}</span> ` : ''}<b>${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : esc(x.title)}</b> <span class="dim">${esc(x.source || '')} ${x.published_at || ''}</span></li>`).join('')}</ul>`;
  }
  const actBadge = (r) => `<span class="act ${r.action}">${ACT[r.action] || r.action}</span>`;
  const entryText = (r) => (r.entry == null ? '-' : r.entry_type === 'above' ? `above ₹${fmt(r.entry, 2)}` : r.entry_type === 'dip' ? `₹${fmt(r.entry, 2)} (limit)` : `₹${fmt(r.entry, 2)}`);
  function buyAtCell(r) {
    if (r.action === 'avoid' || r.entry == null) return '<td class="num dim">—<small class="sub">not a buy</small></td>';
    const sub = { open: 'at next open', above: 'buy-stop order', dip: 'limit order on a dip' }[r.entry_type] || '';
    return `<td class="num"><b>${r.entry_type === 'above' ? 'above ' : ''}₹${fmt(r.entry, 2)}</b><small class="sub">${sub}</small></td>`;
  }
  function whenCell(r) {
    if (r.action === 'avoid') return '<td class="dim">—</td>';
    if (r.action === 'buy') return `<td>${fmtDay(r.entry_date)}, at the open<small class="sub">hold until ${fmtDay(r.hold_until)}</small></td>`;
    if (r.entry_type === 'dip') return `<td>from ${fmtDay(r.entry_date)}<small class="sub">if it dips to ₹${fmt(r.entry, 2)}</small></td>`;
    return `<td>from ${fmtDay(r.entry_date)}<small class="sub">once it trades above ₹${fmt(r.entry, 2)}</small></td>`;
  }
  const levelCell = (v, p, cls) => (v == null ? '<td class="num dim">-</td>'
    : `<td class="num lv ${cls}">₹${fmt(v, 2)}<small class="sub">${p >= 0 ? '+' : ''}${fmt(p)}%</small></td>`);

  function detailRow(r, cols) {
    const kv = (pairs) => `<div class="kv">${pairs.filter(([, v]) => v != null && v !== '').map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join('')}</div>`;
    const checked = r.quality_score != null || (r.quality_flags || []).length;
    const fund = checked
      ? kv([['ROE', r.roe != null ? fmt(r.roe) + '%' : null], ['ROCE', r.roce != null ? fmt(r.roce) + '%' : null], ['P/E', r.pe != null ? fmt(r.pe) : null], ['D/E', r.debt_equity != null ? fmt(r.debt_equity, 2) : null], ['net margin', r.net_margin != null ? fmt(r.net_margin) + '%' : null], ['EPS growth', r.eps_growth != null ? fmt(r.eps_growth, 0) + '%' : null], ['revenue growth', r.revenue_growth != null ? fmt(r.revenue_growth, 0) + '%' : null], ['quality score', r.quality_score != null ? fmt(r.quality_score, 0) + '/100' : null]])
        + `<div style="margin-top:4px">${(r.quality_flags || []).length ? r.quality_flags.map((x) => `<span class="flag">${esc(x)}</span>`).join(' ') : '<span class="pos">no flags</span>'}</div>`
      : '<span class="dim small">checked for buy and wait stocks only</span>';
    const prom = checked ? kv([['promoter holding', r.promoter_pct != null ? fmt(r.promoter_pct) + '%' : 'n/a'], ['change (2 qtrs)', r.promoter_change_pp != null ? pct(r.promoter_change_pp) + ' pp' : null], ['pledged', r.pledge_pct != null ? fmt(r.pledge_pct, 0) + '%' : null], ['FII', r.fii_pct != null ? fmt(r.fii_pct) + '%' : null], ['DII', r.dii_pct != null ? fmt(r.dii_pct) + '%' : null]]) : '<span class="dim small">checked for buy and wait stocks only</span>';
    const tech = kv([['close', fmt(r.close, 2)], ['50 dma', fmt(r.sma50, 2)], ['200 dma', fmt(r.sma200, 2)], ['off 20-day high', r.pullback_pct != null ? fmt(r.pullback_pct) + '%' : null], ['RSI(2)', fmt(r.rsi2, 0)], ['6m return', pct(r.ret_6)], ['1m return', pct(r.ret_1)], ['ATR %', fmt(r.atr_pct, 2) + '%'], ['turnover', '₹' + fmt(r.turnover_cr) + ' cr/day'], ['score', fmt(r.score, 0) + '/100']]);
    const plan = kv([[r.action === 'avoid' ? 'last close' : 'buy at', r.action === 'avoid' ? fmt(r.close, 2) : entryText(r)], ['when', r.action === 'buy' ? `${fmtDay(r.entry_date)}, at the open` : r.action === 'wait' ? `from ${fmtDay(r.entry_date)}` : 'not a buy now'], [r.action === 'avoid' ? 'stop-loss (if holding)' : 'stop-loss', fmt(r.stop_loss, 2)], ['target', fmt(r.target, 2)], ['reward : risk', r.reward_risk != null ? fmt(r.reward_risk) : null], ['hold until', r.hold_until ? fmtDay(r.hold_until) : null], ['quantity', r.quantity ? `${r.quantity} (${inr(r.position_value)})` : null], ['risk if stopped', r.quantity ? inr(r.risk_amount) : null]]);
    return `<tr class="detail"><td colspan="${cols}"><div class="dgrid">
      <div><h4>trade plan</h4>${plan}<p class="hint">${esc(r.exit_rules || '')}</p></div>
      <div><h4>technicals</h4>${tech}</div>
      <div><h4>fundamentals</h4>${fund}</div>
      <div><h4>promoters &amp; holders</h4>${prom}</div>
      <div><h4>news (${r.sentiment_n || 0} scored of ${r.sentiment_articles || 0})</h4>${sentBadge(r)}${headlinesCell(r, 4)}</div>
    </div></td></tr>`;
  }
  function overviewRow(r, i) {
    return `<tr class="main r-${r.action}" data-key="${esc(r.instrument_key)}"><td>${i + 1}</td><td class="sym">${esc(r.symbol)}<small>${esc(r.name || '')}</small></td>`
      + `<td class="why">${actBadge(r)}<small>${esc(r.reason || '')}</small></td>${buyAtCell(r)}${whenCell(r)}`
      + `${levelCell(r.stop_loss, r.stop_pct, 'neg')}${levelCell(r.target, r.target_pct, 'pos')}<td>${sentBadge(r)}</td>`
      + `<td class="num">${r.quantity || '<span class="dim">-</span>'}</td></tr>` + detailRow(r, 9);
  }
  function sentRow(r, i) {
    return `<tr class="r-${r.action}"><td>${i + 1}</td><td class="sym">${esc(r.symbol)}<small>${esc(r.name || '')}</small></td><td>${sentBadge(r)}</td>`
      + `<td class="num">${r.sentiment_n || 0}/${r.sentiment_articles || 0}</td><td class="num"><span class="pos">${r.sentiment_pos || 0}</span> / <span class="neg">${r.sentiment_neg || 0}</span></td>`
      + `<td>${actBadge(r)}</td>${levelCell(r.stop_loss, r.stop_pct, 'neg')}${levelCell(r.target, r.target_pct, 'pos')}<td class="small">${headlinesCell(r, 2)}</td></tr>`;
  }
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('tr.main'); if (!tr || e.target.closest('a')) return;
    tr.classList.toggle('open'); tr.nextElementSibling?.classList.toggle('open');
  });

  async function renderSummary(scan) {
    const st = scan.stats || {}, rg = scan.regime || {};
    const perf = await api('/api/performance').catch(() => null);
    const ps = perf?.stats || {};
    const settings = await api('/api/settings').catch(() => ({}));
    const cells = [
      ['market', `<span class="${rg.state || ''}">${(rg.state || '-').replace('_', ' ')}</span>`, rg.nifty_close ? `nifty ${fmt(rg.nifty_close, 0)}` : ''],
      ['buy now', `<span class="pos">${st.buy ?? 0}</span>`, `${st.wait ?? 0} wait · ${st.avoid ?? 0} avoid`],
      ['stocks rated', `${st.universe || 0}`, `${st.gated_out ?? 0} failed quality checks`],
      ['news checked', `${st.sentiment_scored ?? 0}`, st.sentiment_enabled ? `${st.sentiment_requests ?? 0} marketaux requests` : 'no marketaux key'],
      ['data as of', st.data_as_of ? fmtDay(st.data_as_of) : '-', `scan ${new Date(scan.run_at).toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })} ist`],
      ['track record', ps.trades ? `<span class="${ps.avg_ret_pct >= 0 ? 'pos' : 'neg'}">${ps.avg_ret_pct >= 0 ? '+' : ''}${fmt(ps.avg_ret_pct, 2)}%</span>` : '<span class="dim">-</span>', ps.trades ? `${ps.trades} closed · win ${ps.win_rate_pct}%` : 'no closed buys yet'],
      ['next scan', settings.schedule?.enabled && settings.schedule.next_run ? new Date(settings.schedule.next_run).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'off', settings.schedule?.enabled ? 'automatic, ist' : 'enable in settings'],
    ];
    const el = $('#summary');
    el.innerHTML = cells.map(([k, v, b]) => `<div class="s"><div class="k">${k}</div><div class="v">${v}</div><div class="b">${b}</div></div>`).join('');
    el.classList.remove('hidden');
  }

  // ------------------------------------------------------------ filters
  const view = { ov: 'all', ovQ: '', sent: 'all', sentQ: '' };
  const matchQ = (r, q) => !q || r.symbol.toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q);
  function filterBar(el, opts, current, key) {
    $(el).innerHTML = opts.map(([v, label, n]) => `<button data-f="${key}" data-v="${v}" class="${v === current ? 'on' : ''}">${label}<small>${n}</small></button>`).join('');
  }
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-f]'); if (!b) return;
    view[b.dataset.f] = b.dataset.v; renderLists();
  });
  $('#ov-search').addEventListener('input', (e) => { view.ovQ = e.target.value.trim().toLowerCase(); renderLists(); });
  $('#sent-search').addEventListener('input', (e) => { view.sentQ = e.target.value.trim().toLowerCase(); renderLists(); });

  function renderLists() {
    const all = (lastScan && lastScan.results) || [];
    const count = (f) => all.filter(f).length;
    filterBar('#ov-filters', [['all', 'All', all.length], ['buy', 'Buy', count((r) => r.action === 'buy')], ['wait', 'Wait', count((r) => r.action === 'wait')], ['avoid', 'Avoid', count((r) => r.action === 'avoid')]], view.ov, 'ov');
    const ov = all.filter((r) => (view.ov === 'all' || r.action === view.ov) && matchQ(r, view.ovQ));
    $('#overview tbody').innerHTML = ov.map(overviewRow).join('')
      || `<tr><td colspan="9" class="muted">${all.length ? 'nothing matches this filter.' : 'no scan yet — connect Upstox in settings, then click run scan.'}</td></tr>`;

    const v = (r) => r.sentiment_verdict || 'not_checked';
    filterBar('#sent-filters', [['all', 'All', all.length], ['positive', 'Positive', count((r) => v(r) === 'positive')], ['neutral', 'Neutral', count((r) => v(r) === 'neutral')], ['negative', 'Negative', count((r) => v(r) === 'negative')], ['no_news', 'No news', count((r) => v(r) === 'no_news')], ['not_checked', 'Not checked', count((r) => v(r) === 'not_checked')]], view.sent, 'sent');
    const rank = (r) => (r.sentiment_score != null ? r.sentiment_score : v(r) === 'no_news' ? -2 : -3);
    const sent = all.filter((r) => (view.sent === 'all' || v(r) === view.sent) && matchQ(r, view.sentQ))
      .sort((a, b) => rank(b) - rank(a) || (b.sentiment_n || 0) - (a.sentiment_n || 0));
    $('#sent-table tbody').innerHTML = sent.map(sentRow).join('')
      || `<tr><td colspan="9" class="muted">${all.length ? 'nothing matches this filter.' : 'no scan yet.'}</td></tr>`;
  }

  // ------------------------------------------------------------- dashboard
  let lastScan = null;
  function renderScan(scan) {
    lastScan = scan;
    $('#needs-login').classList.toggle('hidden', scan.status !== 'needs_login');
    if (!scan || scan.status === 'none') { $('#scan-meta').textContent = 'no scan yet. connect upstox in settings, then run a scan.'; lastScan = null; renderLists(); return; }
    const st = scan.stats || {};
    $('#scan-meta').textContent = `last scan ${ist(scan.run_at)} · ${scan.status} · ${scan.message || ''}`;
    const rg = scan.regime || {};
    const rEl = $('#regime');
    if (rg.state && rg.state !== 'RISK_ON') {
      rEl.className = 'regime ' + rg.state;
      rEl.innerHTML = `<span class="state">${rg.state.replace('_', ' ')}</span> ${esc(rg.note)} <span class="muted small">nifty ${fmt(rg.nifty_close, 0)} · 50dma ${fmt(rg.sma50, 0)} · 200dma ${fmt(rg.sma200, 0)}</span>`;
      rEl.classList.remove('hidden');
    } else rEl.classList.add('hidden');
    if (scan.status === 'done') renderSummary(scan); else $('#summary').classList.add('hidden');
    const gate = Object.entries(st.gate_reasons || {}).map(([k, n]) => `${k}: ${n}`).join(' · ');
    $('#stats').innerHTML = st.universe ? `${st.universe} stocks rated · ${st.verified ?? 0} buy/wait candidates checked for fundamentals · ${st.gated_out ?? 0} failed${gate ? ` (${esc(gate)})` : ''} · ${st.sentiment_excluded ?? 0} moved to avoid on negative news`
      + (st.fetch_errors ? ` · ${st.fetch_errors} data errors` : '')
      + (st.missing_symbols?.length ? ` · not in instrument master: ${st.missing_symbols.join(', ')}` : '') : '';
    $('#sent-warn').classList.toggle('hidden', !!st.sentiment_enabled || !st.universe);
    $('#sent-meta').textContent = st.universe
      ? `${st.sentiment_scored ?? 0} of ${st.universe} stocks have news sentiment · method ${st.sentiment_method || '-'} · ${st.sentiment_requests ?? 0} marketaux requests this scan` + (st.sentiment_error ? ` · marketaux: ${st.sentiment_error}` : '')
      : 'score is a recency-weighted average of Marketaux article sentiment, from −1 (very negative) to +1 (very positive).';
    renderLists();
  }

  async function loadLatest() { try { renderScan(await api('/api/scan/latest')); } catch (e) { toast(e.message, true); } }

  let pollTimer, wasRunning = false;
  async function pollStatus() {
    const s = await api('/api/scan/status').catch(() => null);
    if (!s) return;
    const p = $('#progress');
    $('#btn-scan').disabled = s.running;
    if (s.running) {
      wasRunning = true;
      p.classList.remove('hidden');
      const frac = s.total ? s.done / s.total : 0;
      $('#progress-fill').style.width = Math.round(frac * 100) + '%';
      termLog(`[${s.phase}] ${s.message}${s.total ? ` ${s.done}/${s.total}` : ''}${s.error_count ? ` · ${s.error_count} errors` : ''}`);
      (s.errors || []).slice(-2).forEach((e) => termLog('  ! ' + e, 'err'));
      pollTimer = setTimeout(pollStatus, 1500);
    } else if (wasRunning) {
      wasRunning = false; p.classList.add('hidden');
      termLog(`[${s.phase}] ${s.message}`, s.phase === 'done' ? 'ok' : 'err');
      loadLatest(); toast(s.message || 'scan finished', s.phase !== 'done');
    }
    sysStatus();
  }
  $('#btn-scan').addEventListener('click', async () => {
    try { term.lines = []; termLog('scan requested'); await api('/api/scan', { method: 'POST' }); $('#progress').classList.remove('hidden'); pollStatus(); }
    catch (e) { toast(e.message, true); }
  });

  async function sysStatus() {
    const s = await api('/api/settings').catch(() => null);
    if (!s) return;
    $('#sys').innerHTML = `<span>Upstox <b class="${s.has_access_token ? 'ok' : 'bad'}">${s.has_access_token ? 'connected' : 'not connected'}</b></span><span>Marketaux <b class="${s.has_marketaux_key ? 'ok' : 'warn'}">${s.has_marketaux_key ? 'connected' : 'off'}</b></span><span>Daily scan <b class="${s.schedule?.enabled ? 'ok' : 'warn'}">${s.schedule?.enabled ? s.schedule_time + ' IST' : 'off'}</b></span>`;
    if (!$('#user').dataset.loaded) { api('/api/me').then((m) => { $('#user').dataset.loaded = '1'; $('#user').innerHTML = m.login_required ? `<span>${esc(m.email)}</span><a href="/logout">Log out</a>` : ''; }).catch(() => {}); }
  }

  // -------------------------------------------------------------- sentiment lookup
  $('#btn-sent-lookup').addEventListener('click', async () => {
    const sym = $('#sent-symbol').value.trim().toUpperCase(); if (!sym) return;
    const out = $('#sent-lookup-out'); out.innerHTML = '<div class="muted small">querying marketaux…</div>';
    try {
      const r = await api(`/api/sentiment/${sym}?refresh=true`);
      const s = r.sentiment;
      out.innerHTML = `<div class="card" style="margin-bottom:12px"><h2>${esc(sym)}</h2>${sentBadge({ sentiment_verdict: s.verdict, sentiment_score: s.score, sentiment_n: s.n_scored })} <span class="muted small">${s.n_articles} articles in window · cached for 12 hours, shows in the table after the next scan</span>${headlinesCell({ headlines: s.headlines }, 8)}</div>`;
    } catch (e) { out.innerHTML = `<div class="callout warn">${esc(e.message)}</div>`; }
  });

  // ------------------------------------------------------------ performance
  function statBlock(title, s) {
    if (!s || !s.trades) return `<div class="metric"><div class="k">${title}</div><div class="v dim">-</div><div class="b">no closed trades</div></div>`;
    return `<div class="metric"><div class="k">${title}</div><div class="v ${s.avg_ret_pct >= 0 ? 'pos' : 'neg'}">${s.avg_ret_pct >= 0 ? '+' : ''}${fmt(s.avg_ret_pct, 2)}%</div><div class="b">${s.trades} trades · win ${s.win_rate_pct}% · pf ${s.profit_factor ?? '-'} · avg win ${fmt(s.avg_win_pct, 2)}% / loss ${fmt(s.avg_loss_pct, 2)}%${s.nifty_avg_pct != null ? ` · nifty same window ${fmt(s.nifty_avg_pct, 2)}%` : ''}</div></div>`;
  }
  function equityChart(curve) {
    if (curve.length < 2) return '';
    const W = 900, H = 200, L = 50, R = 10, T = 12, B = 28;
    const ys = curve.map((c) => c.equity); const y0 = Math.min(...ys, 1) * 0.98, y1 = Math.max(...ys, 1) * 1.02;
    const x = (i) => L + (i / (curve.length - 1)) * (W - L - R);
    const y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
    return `<svg class="chart" style="height:200px" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <line x1="${L}" x2="${W - R}" y1="${y(1)}" y2="${y(1)}" stroke="#d1d5db" stroke-dasharray="4 4"/>
      <path d="${curve.map((c, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(c.equity).toFixed(1)}`).join(' ')}" fill="none" stroke="#f26625" stroke-width="2"/>
      <text x="${L - 6}" y="${y(1) + 4}" fill="#6b7280" font-size="11" text-anchor="end">1.00x</text>
      <text x="${L - 6}" y="${y(y1) + 10}" fill="#6b7280" font-size="11" text-anchor="end">${y1.toFixed(2)}x</text>
      <text x="${L}" y="${H - 8}" fill="#6b7280" font-size="11">${curve[0].date}</text>
      <text x="${W - R}" y="${H - 8}" fill="#6b7280" font-size="11" text-anchor="end">${curve[curve.length - 1].date}</text></svg>`;
  }
  async function loadPerformance() {
    const out = $('#perf-out'); out.textContent = 'grading…';
    try {
      const r = await api('/api/performance');
      if (!r.outcomes.length) { out.textContent = 'no picks yet. outcomes appear here the day after a scan, once the next candles are downloaded.'; return; }
      const s = r.stats;
      const exits = s.exits ? Object.entries(s.exits).map(([k, v]) => `${k} ${v}`).join(' · ') : '';
      out.innerHTML = `<div class="metrics">${statBlock('all closed buys', s)}
        <div class="metric"><div class="k">status</div><div class="v">${s.trades || 0}<span class="dim small"> closed</span></div><div class="b">${s.open || 0} open · ${s.pending || 0} awaiting next candle · exits: ${exits}</div></div></div>
        ${equityChart(r.curve)}
        <h3>by news verdict at signal time</h3>
        <div class="metrics">${Object.entries(r.by_sentiment || {}).map(([k, v]) => statBlock(k.replace('_', ' '), v)).join('')}</div>
        <h3>every pick</h3>
        <div class="table-wrap"><table><thead><tr><th>signal</th><th>symbol</th><th class="num">entry</th><th class="num">stop</th><th class="num">target</th><th class="num">exit</th><th>reason</th><th class="num">days</th><th class="num">return</th><th class="num">nifty</th><th>news</th></tr></thead><tbody>
        ${r.outcomes.map((o) => `<tr><td>${o.scan_date}</td><td class="sym">${esc(o.symbol)}</td><td class="num">${fmt(o.entry, 2)}</td><td class="num neg">${fmt(o.stop, 2)}</td><td class="num pos">${fmt(o.target, 2)}</td><td class="num">${fmt(o.exit, 2)}</td><td><span class="badge ${o.exit_reason === 'target' ? 'positive' : o.exit_reason === 'stop' ? 'negative' : o.status === 'open' ? 'neutral' : 'no_news'}">${o.status === 'pending' ? 'pending' : o.exit_reason}</span></td><td class="num">${o.days_held}</td><td class="num">${o.ret_pct == null ? '-' : pct(o.ret_pct)}</td><td class="num">${o.nifty_ret_pct == null ? '-' : pct(o.nifty_ret_pct)}</td><td class="small">${(o.sentiment_verdict || '').replace('_', ' ')}</td></tr>`).join('')}</tbody></table></div>
        <p class="muted small">a stock that stays in the list on consecutive days is graded once per signal day. "open" = still inside its holding window or above its stop; "pending" = no candle after the signal yet. judge after 30+ closed trades, not 4.</p>`;
    } catch (e) { out.textContent = e.message; }
  }
  $('#btn-perf').addEventListener('click', loadPerformance);

  // -------------------------------------------------------------- backtest
  function metric(k, v, b) { return `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div><div class="b">${b || ''}</div></div>`; }
  function chart(curve) {
    const W = 900, H = 280, L = 50, R = 10, T = 12, B = 28;
    const ys = curve.flatMap((c) => [c.strategy, c.nifty]);
    const y0 = Math.min(...ys) * 0.95, y1 = Math.max(...ys) * 1.05;
    const x = (i) => L + (i / (curve.length - 1)) * (W - L - R);
    const y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
    const path = (key) => curve.map((c, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(c[key]).toFixed(1)}`).join(' ');
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => y0 + f * (y1 - y0));
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      ${ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#e5e7eb"/><text x="${L - 6}" y="${y(v) + 4}" fill="#6b7280" font-size="11" text-anchor="end">${v.toFixed(2)}x</text>`).join('')}
      <path d="${path('nifty')}" fill="none" stroke="#9ca3af" stroke-width="1.5"/>
      <path d="${path('strategy')}" fill="none" stroke="#f26625" stroke-width="2"/>
      <text x="${L}" y="${H - 8}" fill="#6b7280" font-size="11">${curve[0].date}</text>
      <text x="${W - R}" y="${H - 8}" fill="#6b7280" font-size="11" text-anchor="end">${curve[curve.length - 1].date}</text>
      <text x="${W - R - 4}" y="${T + 12}" fill="#f26625" font-size="11" text-anchor="end">strategy</text>
      <text x="${W - R - 4}" y="${T + 26}" fill="#6b7280" font-size="11" text-anchor="end">nifty 50</text>
    </svg>`;
  }
  function renderSwingBacktest(r) {
    const edge = r.baseline_avg_return_pct != null ? (r.avg_return_pct - r.baseline_avg_return_pct).toFixed(2) : null;
    return `<div class="muted small">${r.period.from} → ${r.period.to} · ${r.trades} setups · ${r.params.hold_days}-day max hold · ${r.params.cost_pct_round_trip}% round-trip cost · fundamentals/sentiment gates not applied (no history)</div>
      <div class="metrics">
        ${metric('win rate', r.win_rate_pct + '%', r.baseline_win_rate_pct != null ? `random entry ${r.baseline_win_rate_pct}%` : '')}
        ${metric('avg return / trade', fmt(r.avg_return_pct, 2) + '%', r.baseline_avg_return_pct != null ? `random entry ${fmt(r.baseline_avg_return_pct, 2)}% (edge ${edge >= 0 ? '+' : ''}${edge})` : '')}
        ${metric('avg win / loss', `${fmt(r.avg_win_pct, 2)}% / ${fmt(r.avg_loss_pct, 2)}%`, `median ${fmt(r.median_return_pct, 2)}%`)}
        ${metric('profit factor', r.profit_factor ?? '-', 'gross wins ÷ gross losses')}
        ${metric('avg days held', fmt(r.avg_days_held), '')}
        ${metric('exits', Object.entries(r.by_exit).map(([k, v]) => `${k} ${v.trades}`).join(' · '), Object.entries(r.by_exit).map(([k, v]) => `${k} ${fmt(v.avg_ret_pct, 2)}%`).join(' · '))}
      </div>
      <h3>by year</h3>
      <div class="table-wrap"><table class="plain"><thead><tr><th>year</th><th class="num">setups</th><th class="num">win rate</th><th class="num">avg return</th></tr></thead><tbody>
      ${r.by_year.map((y) => `<tr><td>${y.year}</td><td class="num">${y.trades}</td><td class="num">${y.win_rate_pct}%</td><td class="num">${pct(y.avg_ret_pct)}</td></tr>`).join('')}</tbody></table></div>
      ${r.top_score_bucket?.length ? `<h3>by score bucket</h3><div class="table-wrap"><table class="plain"><thead><tr><th>score</th><th class="num">setups</th><th class="num">win rate</th><th class="num">avg return</th></tr></thead><tbody>
      ${r.top_score_bucket.map((b) => `<tr><td>${b.bucket}</td><td class="num">${b.trades}</td><td class="num">${b.win_rate_pct}%</td><td class="num">${pct(b.avg_ret_pct)}</td></tr>`).join('')}</tbody></table></div>` : ''}
      <details><summary>most recent 30 setups</summary><div class="table-wrap"><table class="plain"><thead><tr><th>date</th><th>symbol</th><th>exit</th><th class="num">days</th><th class="num">return</th></tr></thead><tbody>
      ${r.recent_trades.map((t) => `<tr><td>${t.date}</td><td>${esc(t.symbol.split('|')[1] || t.symbol)}</td><td>${t.exit}</td><td class="num">${t.days}</td><td class="num">${pct(t.ret)}</td></tr>`).join('')}</tbody></table></div></details>
      <p class="muted small">"random entry" = every stock that passed the trend and sanity filters, bought on every day, held the same number of days. the difference is the value the pullback + candle trigger adds.</p>`;
  }
  $('#btn-backtest').addEventListener('click', async () => {
    const out = $('#bt-out'); out.textContent = 'running…'; $('#btn-backtest').disabled = true;
    try {
      const r = await api('/api/backtest', { method: 'POST' });
      if (r.error) { out.textContent = r.error; return; }
      if (r.mode === 'swing') { out.innerHTML = renderSwingBacktest(r); return; }
      const s = r.strategy, n = r.nifty;
      out.innerHTML = `<div class="muted small">${r.period.from} → ${r.period.to}, ${s.months} months, avg turnover ${r.avg_monthly_turnover_pct}% of positions per month</div>
        <div class="metrics">
          ${metric('cagr', fmt(s.cagr_pct) + '%', 'nifty ' + fmt(n.cagr_pct) + '%')}
          ${metric('avg month', fmt(s.avg_month_pct, 2) + '%', 'nifty ' + fmt(n.avg_month_pct, 2) + '%')}
          ${metric('positive months', s.positive_months_pct + '%', 'nifty ' + n.positive_months_pct + '%')}
          ${metric('months > 1%', s.months_above_1pct + '%', 'nifty ' + n.months_above_1pct + '%')}
          ${metric('max drawdown', fmt(s.max_drawdown_pct) + '%', 'nifty ' + fmt(n.max_drawdown_pct) + '%')}
          ${metric('sharpe', fmt(s.sharpe, 2), 'nifty ' + fmt(n.sharpe, 2))}
          ${metric('best / worst month', `${fmt(s.best_month_pct)}% / ${fmt(s.worst_month_pct)}%`, `nifty ${fmt(n.best_month_pct)}% / ${fmt(n.worst_month_pct)}%`)}
          ${metric('total return', fmt(s.total_return_pct) + '%', 'nifty ' + fmt(n.total_return_pct) + '%')}
        </div>${chart(r.curve)}
        <details><summary>monthly returns</summary><div class="table-wrap"><table class="plain"><thead><tr><th>month end</th><th class="num">strategy</th><th class="num">equity</th></tr></thead><tbody>
        ${r.curve.map((c) => `<tr><td>${c.date}</td><td class="num">${pct(c.month_ret)}</td><td class="num">${fmt(c.strategy, 3)}x</td></tr>`).join('')}</tbody></table></div></details>`;
    } catch (e) { out.textContent = e.message; } finally { $('#btn-backtest').disabled = false; }
  });

  // --------------------------------------------------------------- history
  async function loadHistory() {
    const rows = await api('/api/scans').catch(() => []);
    $('#history-body').innerHTML = rows.map((s) => `<tr><td>${ist(s.run_at)}</td><td>${s.status}</td><td>${s.params?.mode || '-'}</td>
      <td>${s.regime?.state || '-'}</td><td style="white-space:normal">${esc(s.message)}</td>
      <td>${s.status === 'done' ? `<button data-scan="${s.id}">view</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">no scans yet.</td></tr>';
  }
  $('#history-body').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-scan]'); if (!b) return;
    const s = await api('/api/scans/' + b.dataset.scan);
    const rows = (s.results || []).filter((r) => r.action ? r.action !== 'avoid' : (r.rank > 0 || r.cum_rank > 0));
    $('#history-detail').innerHTML = `<h2 style="margin-top:16px">scan #${s.id} — buy and wait stocks</h2><div class="table-wrap"><table class="plain"><thead><tr><th>action</th><th>symbol</th><th class="num">buy at</th><th class="num">stop</th><th class="num">target</th><th>sentiment</th><th class="num">qty</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${r.action ? actBadge(r) : '-'}</td><td class="sym">${esc(r.symbol)}</td><td class="num">${r.action ? entryText(r) : fmt(r.entry ?? r.close, 2)}</td><td class="num">${fmt(r.stop_loss, 2)}</td><td class="num">${fmt(r.target, 2)}</td><td class="small">${sentBadge(r)}</td><td class="num">${r.quantity || '-'}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">no buy or wait stocks in this scan.</td></tr>'}</tbody></table></div>`;
  });

  // -------------------------------------------------------------- settings
  const FIELDS = ['api_key', 'redirect_uri', 'universe', 'hold_days', 'exclude_symbols', 'capital', 'risk_per_trade_pct', 'top_n', 'min_price', 'min_turnover_cr', 'schedule_time', 'sentiment_days', 'sweep_budget'];
  async function loadSettings() {
    const s = await api('/api/settings');
    FIELDS.forEach((k) => { $('#s-' + k).value = s[k] ?? ''; });
    $('#s-schedule_enabled').checked = s.schedule_enabled === '1';
    $('#s-require_fundamentals').checked = s.require_fundamentals === '1';
    $('#s-api_secret').placeholder = s.has_api_secret ? '(saved — leave blank to keep)' : 'Paste your API secret';
    $('#s-marketaux_key').placeholder = s.has_marketaux_key ? '(saved — leave blank to keep)' : 'Paste your Marketaux token';
    $('#token-state').textContent = s.has_access_token ? `Connected${s.token_issued_at ? ' ' + ist(s.token_issued_at) : ''}. Upstox tokens expire at 03:30 IST daily; reconnect when a scan reports "needs login".` : 'Not connected yet — save your key and secret, then click Connect Upstox.';
    $('#news-state').textContent = s.has_marketaux_key ? 'Token saved' : 'No token — sentiment disabled';
    $('#schedule-state').textContent = s.schedule?.enabled && s.schedule.next_run ? `next automatic scan: ${ist(s.schedule.next_run)}` : 'automatic scans disabled';
    $('#bt-desc').textContent = 'replays every historical "buy" signal on the cached candles with the same stop, target and holding period, and compares it with buying the same stocks on random days.';
    const st = await api('/api/status').catch(() => null);
    if (st) $('#data-state').textContent = `cached: ${st.instruments} nse instruments · ${st.symbols_with_data} symbols with data · ${st.candle_rows.toLocaleString('en-IN')} daily candles`;
    sysStatus();
  }
  async function saveSettings(values, msg) {
    try { await api('/api/settings', { method: 'POST', body: JSON.stringify({ values }) }); toast(msg); loadSettings(); }
    catch (e) { toast(e.message, true); }
  }
  $('#btn-save-conn').addEventListener('click', async () => {
    await saveSettings({ api_key: $('#s-api_key').value.trim(), api_secret: $('#s-api_secret').value.trim(), redirect_uri: $('#s-redirect_uri').value.trim() }, 'upstox settings saved');
    $('#s-api_secret').value = '';
  });
  $('#btn-save-news').addEventListener('click', async () => {
    const values = { marketaux_key: $('#s-marketaux_key').value.trim() };
    ['sentiment_days', 'sweep_budget'].forEach((k) => { values[k] = $('#s-' + k).value.trim(); });
    await saveSettings(values, 'marketaux settings saved');
    $('#s-marketaux_key').value = '';
  });
  $('#btn-clear-news').addEventListener('click', async () => { await api('/api/marketaux/clear', { method: 'POST' }); toast('marketaux key removed'); loadSettings(); });
  $('#btn-save-strat').addEventListener('click', async () => {
    const values = {};
    ['universe', 'hold_days', 'exclude_symbols', 'capital', 'risk_per_trade_pct', 'top_n', 'min_price', 'min_turnover_cr', 'schedule_time'].forEach((k) => { values[k] = $('#s-' + k).value.trim(); });
    values.schedule_enabled = $('#s-schedule_enabled').checked ? '1' : '0';
    values.require_fundamentals = $('#s-require_fundamentals').checked ? '1' : '0';
    await saveSettings(values, 'strategy settings saved');
  });
  $('#btn-token').addEventListener('click', async () => {
    try { const r = await api('/api/token', { method: 'POST', body: JSON.stringify({ access_token: $('#s-token').value }) }); $('#s-token').value = ''; toast('token verified for ' + (r.user || 'user')); loadSettings(); }
    catch (e) { toast(e.message, true); }
  });

  // ------------------------------------------------------------------ boot
  const q = new URLSearchParams(location.search);
  if (q.get('login') === 'ok') { toast('Upstox connected'); history.replaceState({}, '', '/app'); }
  else if (q.get('login') === 'error') { toast('Upstox login failed: ' + (q.get('msg') || ''), true); history.replaceState({}, '', '/app'); showTab('settings'); }
  loadLatest(); pollStatus(); sysStatus();
})();
