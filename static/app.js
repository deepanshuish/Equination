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
  const PATTERN_LABEL = { morning_star: 'morning star', bullish_engulfing: 'bull engulfing', piercing: 'piercing', hammer: 'hammer', bullish_harami: 'bull harami', strong_close: 'strong close' };
  function fundCell(r) {
    const bits = [];
    if (r.roe != null) bits.push(`ROE ${fmt(r.roe)}%`);
    if (r.pe != null) bits.push(`P/E ${fmt(r.pe)}`);
    if (r.debt_equity != null) bits.push(`D/E ${fmt(r.debt_equity, 2)}`);
    if (r.eps_growth != null) bits.push(`EPSg ${fmt(r.eps_growth, 0)}%`);
    return bits.length ? bits.join(' · ') : '<span class="dim">n/a</span>';
  }
  function promoterCell(r) {
    if (r.promoter_pct == null) return '<span class="dim">n/a</span>';
    let s = `${fmt(r.promoter_pct)}%`;
    if (r.promoter_change_pp != null) s += ` (${pct(r.promoter_change_pp)}pp)`;
    if (r.pledge_pct != null && r.pledge_pct > 0) s += ` · pledged ${fmt(r.pledge_pct, 0)}%`;
    return s;
  }
  function flagsCell(r) {
    const f = r.quality_flags || [];
    return f.length ? f.map((x) => `<span class="flag">${esc(x)}</span>`).join(' ') : '<span class="pos">ok</span>';
  }
  function sentBadge(r) {
    const v = r.sentiment_verdict || 'no_news';
    const sc = r.sentiment_score;
    const bar = sc == null ? '' : `<span class="bar"><i class="${sc >= 0 ? 'p' : 'n'}" style="width:${Math.min(Math.abs(sc), 1) * 50}%"></i></span>`;
    return `<span class="badge ${v}">${v.replace('_', ' ')}</span> ${sc == null ? '' : `<span class="${sc > 0 ? 'pos' : sc < 0 ? 'neg' : 'neu'}">${sc >= 0 ? '+' : ''}${fmt(sc, 2)}</span>`}${bar} <span class="dim small">${r.sentiment_n || 0} art</span>`;
  }
  function headlinesCell(r, n = 3) {
    const h = (r.headlines || []).slice(0, n);
    if (!h.length) return '<span class="dim">no recent articles</span>';
    return `<ul class="headlines">${h.map((x) => `<li>${x.sentiment != null ? `<span class="${x.sentiment > 0.1 ? 'pos' : x.sentiment < -0.1 ? 'neg' : 'neu'}">${x.sentiment >= 0 ? '+' : ''}${fmt(x.sentiment, 2)}</span> ` : ''}<b>${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : esc(x.title)}</b> <span class="dim">${esc(x.source || '')} ${x.published_at || ''}</span></li>`).join('')}</ul>`;
  }
  function setupCell(r, mode) {
    if (mode === 'swing') {
      const setup = (r.patterns || []).map((p) => PATTERN_LABEL[p] || p).join(', ');
      return `<div>${esc(setup)}</div><small class="muted">pullback ${fmt(r.pullback_pct)}% · rsi2 ${fmt(r.rsi2_prev, 0)}→${fmt(r.rsi2, 0)} · vol ${fmt(r.vol_ratio)}× · 6m ${r.ret_6 >= 0 ? '+' : ''}${fmt(r.ret_6)}%</small>`;
    }
    return `<div>momentum</div><small class="muted">12-1m ${fmt(r.ret_12_1)}% · 6m ${fmt(r.ret_6)}% · 3m ${fmt(r.ret_3)}% · vol ${fmt(r.vol)}% · ${fmt(r.from_high)}% vs 52w hi</small>`;
  }
  function planCells(r, mode) {
    if (mode === 'swing') return `<td class="num">${fmt(r.entry, 2)}</td><td class="num neg">${fmt(r.stop_loss, 2)}</td><td class="num pos">${fmt(r.target, 2)}</td><td class="num">${fmt(r.reward_risk)}</td><td>${r.hold_until || '-'}</td>`;
    return `<td class="num">${fmt(r.close, 2)}</td><td class="num neg">${fmt(r.stop_loss, 2)}</td><td class="num dim">50dma</td><td class="num dim">-</td><td class="dim">monthly</td>`;
  }
  const PLAN_HEAD = '<th class="num">entry</th><th class="num">stop</th><th class="num">target</th><th class="num">r:r</th><th>hold until</th>';
  const QUANT_HEAD = `<tr><th>#</th><th>symbol</th><th>setup</th><th class="num">score</th>${PLAN_HEAD}<th class="num">qty</th><th class="num">value</th><th class="num">risk</th><th>fundamentals</th><th>promoters</th><th>flags</th></tr>`;
  const CUM_HEAD = `<tr><th>#</th><th>symbol</th><th class="num">cum</th><th class="num">quant</th><th>sentiment</th><th class="num">quality</th><th>setup</th>${PLAN_HEAD}<th class="num">qty</th><th class="num">value</th><th>headlines</th><th>flags</th></tr>`;

  function quantRow(r, mode) {
    return `<tr><td>${r.rank}</td><td class="sym">${esc(r.symbol)}<small>${esc(r.name || '')}</small></td><td>${setupCell(r, mode)}</td><td class="num">${fmt(r.score, mode === 'swing' ? 0 : 2)}</td>${planCells(r, mode)}<td class="num">${r.quantity}</td><td class="num">${inr(r.position_value)}</td><td class="num">${inr(r.risk_amount)}</td><td class="small">${fundCell(r)}</td><td class="small">${promoterCell(r)}</td><td class="small">${flagsCell(r)}</td></tr>`;
  }
  function cumRow(r, mode) {
    return `<tr><td>${r.cum_rank}</td><td class="sym">${esc(r.symbol)}<small>${esc(r.name || '')}</small></td><td class="num"><b>${fmt(r.cum_score, 0)}</b></td><td class="num">${fmt(r.quant_points, 0)}<br><span class="dim small">#${r.quant_rank}</span></td><td class="small">${sentBadge(r)}</td><td class="num">${fmt(r.quality_score, 0)}</td><td>${setupCell(r, mode)}</td>${planCells(r, mode)}<td class="num">${r.quantity}</td><td class="num">${inr(r.position_value)}</td><td class="small">${headlinesCell(r, 1)}</td><td class="small">${flagsCell(r)}</td></tr>`;
  }
  function sentRow(r, i) {
    return `<tr><td>${i + 1}</td><td class="sym">${esc(r.symbol)}<small>${esc(r.name || '')}</small></td><td>${sentBadge(r)}</td><td class="num">${r.sentiment_score == null ? '-' : fmt(r.sentiment_score, 2)}</td><td class="num">${r.sentiment_n || 0}/${r.sentiment_articles || 0}</td><td class="num"><span class="pos">${r.sentiment_pos || 0}</span> / <span class="neg">${r.sentiment_neg || 0}</span></td><td class="small">${headlinesCell(r, 4)}</td></tr>`;
  }

  // ------------------------------------------------------------- dashboard
  let lastScan = null;
  function renderScan(scan) {
    lastScan = scan;
    const tb = $('#picks tbody');
    $('#needs-login').classList.toggle('hidden', scan.status !== 'needs_login');
    if (!scan || scan.status === 'none') { $('#scan-meta').textContent = 'no scan yet. configure upstox in settings, then run a scan.'; tb.innerHTML = ''; return; }
    const mode = scan.stats?.mode || scan.params?.mode || 'positional';
    const st = scan.stats || {};
    $('#scan-meta').textContent = `last scan ${ist(scan.run_at)} · ${scan.status} · ${mode} mode · ${scan.message || ''}` + (st.data_as_of ? ` · data as of ${st.data_as_of}` : '');
    const rg = scan.regime || {};
    const rEl = $('#regime');
    if (rg.state) {
      rEl.className = 'regime ' + rg.state;
      rEl.innerHTML = `<span class="state">${rg.state.replace('_', ' ')}</span> ${esc(rg.note)} <span class="muted small">nifty ${fmt(rg.nifty_close, 0)} · 50dma ${fmt(rg.sma50, 0)} · 200dma ${fmt(rg.sma200, 0)} · allocation ${Math.round(rg.allocation * 100)}%</span>`;
      rEl.classList.remove('hidden');
    } else rEl.classList.add('hidden');

    const all = scan.results || [];
    const quant = all.filter((r) => r.rank > 0).sort((a, b) => a.rank - b.rank);
    $('#picks thead').innerHTML = QUANT_HEAD;
    tb.innerHTML = quant.map((r) => quantRow(r, mode)).join('') ||
      `<tr><td colspan="16" class="muted">${mode === 'swing' ? 'no valid setups today — normal; pullback-reversal setups appear a handful of days a month. nothing to buy.' : 'no eligible stocks in this scan.'}</td></tr>`;
    const reasons = Object.entries(st.excluded_reasons || {}).map(([k, v]) => `${k}: ${v}`).join(' · ');
    const gate = Object.entries(st.gate_reasons || {}).map(([k, v]) => `${k}: ${v}`).join(' · ');
    $('#stats').innerHTML = st.universe ? `universe ${st.universe} · ${mode === 'swing' ? `setups ${st.setups}` : `eligible ${st.eligible}`} · verified ${st.verified ?? 0} · gated out ${st.gated_out ?? 0}${gate ? ` (${esc(gate)})` : ''} · technical exclusions: ${esc(reasons)}` +
      (st.fetch_errors ? ` · ${st.fetch_errors} data errors` : '') +
      (st.missing_symbols?.length ? ` · not in instrument master: ${st.missing_symbols.join(', ')}` : '') : '';
    $('#mode-note').textContent = mode === 'swing'
      ? 'swing mode: buy at the next open, place the stop immediately, sell at the target or the stop, otherwise at the close of the "hold until" day. quantity uses your capital, risk-per-trade and the regime allocation. educational tool, not investment advice.'
      : 'positional mode: review daily, act only when a stop is hit or at the monthly rebalance. quantity uses your capital, risk-per-trade and the regime allocation. educational tool, not investment advice.';

    // cumulative
    const cum = all.filter((r) => r.cum_rank > 0).sort((a, b) => a.cum_rank - b.cum_rank);
    $('#cum-picks thead').innerHTML = CUM_HEAD;
    $('#cum-picks tbody').innerHTML = cum.map((r) => cumRow(r, mode)).join('') || '<tr><td colspan="17" class="muted">nothing passed the cumulative filter in this scan.</td></tr>';
    $('#cum-warn').classList.toggle('hidden', !!st.sentiment_enabled);
    const w = st.weights || {};
    $('#cum-meta').textContent = `weights quant ${w.quant ?? 55} / sentiment ${w.sentiment ?? 30} / quality ${w.quality ?? 15} · ${st.sentiment_scored ?? 0} of ${all.length} candidates have news sentiment · ${st.sentiment_excluded ?? 0} vetoed by negative news` + (st.sentiment_error ? ` · marketaux: ${st.sentiment_error}` : '');

    // sentiment
    const sent = [...all].sort((a, b) => (b.sentiment_score ?? -9) - (a.sentiment_score ?? -9) || (b.sentiment_n || 0) - (a.sentiment_n || 0));
    $('#sent-table tbody').innerHTML = sent.map(sentRow).join('') || '<tr><td colspan="7" class="muted">no candidates in the last scan.</td></tr>';
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
    $('#sys').innerHTML = `<span>upstox <b class="${s.has_access_token ? 'ok' : 'bad'}">${s.has_access_token ? 'online' : 'no token'}</b></span><span>marketaux <b class="${s.has_marketaux_key ? 'ok' : 'warn'}">${s.has_marketaux_key ? 'online' : 'off'}</b></span><span>cron <b class="${s.schedule?.enabled ? 'ok' : 'warn'}">${s.schedule?.enabled ? s.schedule_time + ' ist' : 'off'}</b></span>`;
  }

  // -------------------------------------------------------------- sentiment lookup
  $('#btn-sent-lookup').addEventListener('click', async () => {
    const sym = $('#sent-symbol').value.trim().toUpperCase(); if (!sym) return;
    const out = $('#sent-lookup-out'); out.innerHTML = '<div class="muted small">querying marketaux…</div>';
    try {
      const r = await api(`/api/sentiment/${sym}?refresh=true`);
      const s = r.sentiment;
      out.innerHTML = `<div class="card" style="margin-bottom:12px"><h2>${esc(sym)}</h2>${sentBadge({ sentiment_verdict: s.verdict, sentiment_score: s.score, sentiment_n: s.n_scored })} <span class="muted small">${s.n_articles} articles in window · cumulative points ${r.points} · query: ${esc(s.query)}</span>${headlinesCell({ headlines: s.headlines }, 8)}</div>`;
    } catch (e) { out.innerHTML = `<div class="callout warn">${esc(e.message)}</div>`; }
  });

  // -------------------------------------------------------------- sweep
  function sweepRow(r, i) {
    return `<tr><td>${i + 1}</td><td class="sym">${esc(r.symbol)}<small>${esc(r.name || '')}</small></td><td><span class="badge ${r.verdict}">${(r.verdict || '').replace('_', ' ')}</span></td><td class="num ${r.score > 0 ? 'pos' : r.score < 0 ? 'neg' : 'neu'}">${r.score >= 0 ? '+' : ''}${fmt(r.score, 2)}</td><td class="num">${r.n}</td></tr>`;
  }
  async function loadSweep() {
    const s = await api('/api/sweep/latest?top=5').catch(() => null);
    if (!s || s.status === 'none') return;
    $('#sweep-meta').textContent = `last sweep ${ist(s.run_at)} · ${s.scored} of ${s.covered} symbols scored (${s.universe} in universe) · method ${s.method} · ${s.requests_used} marketaux requests · ${s.days}-day window`;
    $('#sweep-top').innerHTML = s.top.map(sweepRow).join('') || '<tr><td colspan="5" class="muted">nothing qualified (need ≥ 2 scored articles)</td></tr>';
    $('#sweep-bottom').innerHTML = s.bottom.map(sweepRow).join('') || '<tr><td colspan="5" class="muted">-</td></tr>';
  }
  let sweepPoll;
  $('#btn-sweep').addEventListener('click', async () => {
    try {
      await api('/api/sweep', { method: 'POST' });
      $('#btn-sweep').disabled = true; $('#sweep-meta').textContent = 'sweeping…';
      const tick = async () => {
        const st = await api('/api/scan/status').catch(() => null);
        if (st && st.running) { $('#sweep-meta').textContent = `sweeping… ${st.done}/${st.total}`; sweepPoll = setTimeout(tick, 1500); return; }
        $('#btn-sweep').disabled = false; toast(st?.message || 'sweep finished', st?.phase === 'failed'); loadSweep();
      };
      tick();
    } catch (e) { toast(e.message, true); }
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
      <line x1="${L}" x2="${W - R}" y1="${y(1)}" y2="${y(1)}" stroke="#1f4a2e" stroke-dasharray="4 4"/>
      <path d="${curve.map((c, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(c.equity).toFixed(1)}`).join(' ')}" fill="none" stroke="#39ff14" stroke-width="2"/>
      <text x="${L - 6}" y="${y(1) + 4}" fill="#5f8a6d" font-size="11" text-anchor="end">1.00x</text>
      <text x="${L - 6}" y="${y(y1) + 10}" fill="#5f8a6d" font-size="11" text-anchor="end">${y1.toFixed(2)}x</text>
      <text x="${L}" y="${H - 8}" fill="#5f8a6d" font-size="11">${curve[0].date}</text>
      <text x="${W - R}" y="${H - 8}" fill="#5f8a6d" font-size="11" text-anchor="end">${curve[curve.length - 1].date}</text></svg>`;
  }
  async function loadPerformance() {
    const out = $('#perf-out'); out.textContent = 'grading…';
    try {
      const r = await api('/api/performance');
      if (!r.outcomes.length) { out.textContent = 'no picks yet. outcomes appear here the day after a scan, once the next candles are downloaded.'; return; }
      const s = r.stats;
      const exits = s.exits ? Object.entries(s.exits).map(([k, v]) => `${k} ${v}`).join(' · ') : '';
      out.innerHTML = `<div class="metrics">${statBlock('all closed picks', s)}${statBlock('quant list', r.by_list.quant)}${statBlock('cumulative list', r.by_list.cumulative)}
        <div class="metric"><div class="k">status</div><div class="v">${s.trades || 0}<span class="dim small"> closed</span></div><div class="b">${s.open || 0} open · ${s.pending || 0} awaiting next candle · exits: ${exits}</div></div></div>
        ${equityChart(r.curve)}
        <h3>by news verdict at signal time</h3>
        <div class="metrics">${Object.entries(r.by_sentiment || {}).map(([k, v]) => statBlock(k.replace('_', ' '), v)).join('')}</div>
        <h3>every pick</h3>
        <div class="table-wrap"><table><thead><tr><th>signal</th><th>symbol</th><th>list</th><th>mode</th><th class="num">entry</th><th class="num">stop</th><th class="num">target</th><th class="num">exit</th><th>reason</th><th class="num">days</th><th class="num">return</th><th class="num">nifty</th><th>news</th></tr></thead><tbody>
        ${r.outcomes.map((o) => `<tr><td>${o.scan_date}</td><td class="sym">${esc(o.symbol)}</td><td class="small">${o.list}${o.quant_rank ? ` q#${o.quant_rank}` : ''}${o.cum_rank ? ` c#${o.cum_rank}` : ''}</td><td class="small">${o.mode}</td><td class="num">${fmt(o.entry, 2)}</td><td class="num neg">${fmt(o.stop, 2)}</td><td class="num pos">${fmt(o.target, 2)}</td><td class="num">${fmt(o.exit, 2)}</td><td><span class="badge ${o.exit_reason === 'target' ? 'positive' : o.exit_reason === 'stop' ? 'negative' : o.status === 'open' ? 'neutral' : 'no_news'}">${o.status === 'pending' ? 'pending' : o.exit_reason}</span></td><td class="num">${o.days_held}</td><td class="num">${o.ret_pct == null ? '-' : pct(o.ret_pct)}</td><td class="num">${o.nifty_ret_pct == null ? '-' : pct(o.nifty_ret_pct)}</td><td class="small">${o.sentiment_verdict.replace('_', ' ')}</td></tr>`).join('')}</tbody></table></div>
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
      ${ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#16301f"/><text x="${L - 6}" y="${y(v) + 4}" fill="#5f8a6d" font-size="11" text-anchor="end">${v.toFixed(2)}x</text>`).join('')}
      <path d="${path('nifty')}" fill="none" stroke="#5f8a6d" stroke-width="1.5"/>
      <path d="${path('strategy')}" fill="none" stroke="#39ff14" stroke-width="2"/>
      <text x="${L}" y="${H - 8}" fill="#5f8a6d" font-size="11">${curve[0].date}</text>
      <text x="${W - R}" y="${H - 8}" fill="#5f8a6d" font-size="11" text-anchor="end">${curve[curve.length - 1].date}</text>
      <text x="${W - R - 4}" y="${T + 12}" fill="#39ff14" font-size="11" text-anchor="end">strategy</text>
      <text x="${W - R - 4}" y="${T + 26}" fill="#5f8a6d" font-size="11" text-anchor="end">nifty 50</text>
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
    const rows = (s.results || []).filter((r) => r.rank > 0 || r.cum_rank > 0);
    $('#history-detail').innerHTML = `<h2 style="margin-top:16px">scan #${s.id}</h2><div class="table-wrap"><table class="plain"><thead><tr><th>quant #</th><th>cum #</th><th>symbol</th><th class="num">entry</th><th class="num">stop</th><th class="num">target</th><th>sentiment</th><th class="num">qty</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${r.rank || '-'}</td><td>${r.cum_rank || '-'}</td><td class="sym">${esc(r.symbol)}</td><td class="num">${fmt(r.entry ?? r.close, 2)}</td><td class="num">${fmt(r.stop_loss, 2)}</td><td class="num">${fmt(r.target, 2)}</td><td class="small">${sentBadge(r)}</td><td class="num">${r.quantity}</td></tr>`).join('')}</tbody></table></div>`;
  });

  // -------------------------------------------------------------- settings
  const FIELDS = ['api_key', 'redirect_uri', 'universe', 'mode', 'hold_days', 'exclude_symbols', 'capital', 'risk_per_trade_pct', 'top_n', 'min_price', 'min_turnover_cr', 'schedule_time', 'sentiment_days', 'w_quant', 'w_sentiment', 'w_quality', 'sweep_budget'];
  async function loadSettings() {
    const s = await api('/api/settings');
    FIELDS.forEach((k) => { $('#s-' + k).value = s[k] ?? ''; });
    $('#s-schedule_enabled').checked = s.schedule_enabled === '1';
    $('#s-sweep_in_scan').checked = s.sweep_in_scan === '1';
    $('#s-require_fundamentals').checked = s.require_fundamentals === '1';
    $('#s-api_secret').placeholder = s.has_api_secret ? '(saved — leave blank to keep)' : 'paste your api secret';
    $('#s-marketaux_key').placeholder = s.has_marketaux_key ? '(saved — leave blank to keep)' : 'paste your marketaux token';
    $('#token-state').textContent = s.has_access_token ? `access token saved${s.token_issued_at ? ' ' + ist(s.token_issued_at) : ''}. upstox tokens expire at 03:30 ist daily; log in again when a scan reports "needs login".` : 'no access token yet — save your key and secret, then click login with upstox.';
    $('#news-state').textContent = s.has_marketaux_key ? 'key saved' : 'no key — sentiment disabled';
    $('#schedule-state').textContent = s.schedule?.enabled && s.schedule.next_run ? `next automatic scan: ${ist(s.schedule.next_run)}` : 'automatic scans disabled';
    $('#bt-desc').textContent = s.mode === 'swing'
      ? 'replays every historical pullback + reversal-candle setup on the cached candles with the same stop, target and holding period, and compares it with buying the same stocks on random days.'
      : 'monthly rebalance of the momentum rules on the cached candles, 0.25% cost per switch, versus the nifty 50.';
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
    ['sentiment_days', 'w_quant', 'w_sentiment', 'w_quality', 'sweep_budget'].forEach((k) => { values[k] = $('#s-' + k).value.trim(); });
    values.sweep_in_scan = $('#s-sweep_in_scan').checked ? '1' : '0';
    await saveSettings(values, 'marketaux settings saved');
    $('#s-marketaux_key').value = '';
  });
  $('#btn-clear-news').addEventListener('click', async () => { await api('/api/marketaux/clear', { method: 'POST' }); toast('marketaux key removed'); loadSettings(); });
  $('#btn-save-strat').addEventListener('click', async () => {
    const values = {};
    ['universe', 'mode', 'hold_days', 'exclude_symbols', 'capital', 'risk_per_trade_pct', 'top_n', 'min_price', 'min_turnover_cr', 'schedule_time'].forEach((k) => { values[k] = $('#s-' + k).value.trim(); });
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
  if (q.get('login') === 'ok') { toast('logged in to upstox'); history.replaceState({}, '', '/'); }
  else if (q.get('login') === 'error') { toast('upstox login failed: ' + (q.get('msg') || ''), true); history.replaceState({}, '', '/'); showTab('settings'); }
  loadLatest(); pollStatus(); sysStatus(); loadSweep();
})();
