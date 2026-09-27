(() => {
  const $ = (s) => document.querySelector(s);
  const fmt = (n, d = 1) => (n == null || Number.isNaN(n) ? '-' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: d, minimumFractionDigits: d }));
  const inr = (n) => (n == null ? '-' : '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 }));
  const pct = (n) => (n == null ? '-' : `<span class="${n >= 0 ? 'pos' : 'neg'}">${n >= 0 ? '+' : ''}${fmt(n)}%</span>`);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

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
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), 5000);
  }

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + name));
    if (name === 'history') loadHistory();
    if (name === 'settings') loadSettings();
  }
  document.querySelectorAll('nav button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  document.addEventListener('click', (e) => { const g = e.target.closest('[data-goto]'); if (g) { e.preventDefault(); showTab(g.dataset.goto); } });

  // ------------------------------------------------------------- dashboard
  function renderScan(scan) {
    const tb = $('#picks tbody');
    $('#needs-login').classList.toggle('hidden', scan.status !== 'needs_login');
    if (!scan || scan.status === 'none') { $('#scan-meta').textContent = 'No scan yet. Configure Upstox in Settings, then run a scan.'; tb.innerHTML = ''; return; }
    const when = new Date(scan.run_at).toLocaleString('en-IN');
    $('#scan-meta').textContent = `Last scan ${when} (${scan.status}) — ${scan.message || ''}` + (scan.stats?.data_as_of ? ` Data as of ${scan.stats.data_as_of}.` : '');
    const rg = scan.regime || {};
    const rEl = $('#regime');
    if (rg.state) {
      rEl.className = 'regime ' + rg.state;
      rEl.innerHTML = `<span class="state">${rg.state.replace('_', ' ')}</span> ${esc(rg.note)} ` +
        (rg.nifty_close ? `<span class="muted small">Nifty ${fmt(rg.nifty_close, 0)} · 50DMA ${fmt(rg.sma50, 0)} · 200DMA ${fmt(rg.sma200, 0)} · allocation ${Math.round(rg.allocation * 100)}%</span>` : '');
      rEl.classList.remove('hidden');
    } else rEl.classList.add('hidden');
    tb.innerHTML = (scan.results || []).map((r) => `<tr>
      <td>${r.rank}</td>
      <td class="sym">${esc(r.symbol)}<small>${esc(r.name || '')}</small></td>
      <td class="num">${fmt(r.close, 2)}</td><td class="num">${fmt(r.score, 2)}</td>
      <td class="num">${pct(r.ret_12_1)}</td><td class="num">${pct(r.ret_6)}</td><td class="num">${pct(r.ret_3)}</td><td class="num">${pct(r.ret_1)}</td>
      <td class="num">${fmt(r.vol)}%</td><td class="num">${pct(r.from_high)}</td>
      <td class="num">${fmt(r.stop_loss, 2)}</td><td class="num">${r.quantity}</td><td class="num">${inr(r.position_value)}</td><td class="num">${inr(r.risk_amount)}</td>
    </tr>`).join('') || '<tr><td colspan="14" class="muted">No eligible stocks in this scan.</td></tr>';
    const st = scan.stats || {};
    const reasons = Object.entries(st.excluded_reasons || {}).map(([k, v]) => `${k}: ${v}`).join(' · ');
    $('#stats').innerHTML = st.universe ? `Universe ${st.universe}, eligible ${st.eligible}. Exclusions — ${esc(reasons)}` +
      (st.fetch_errors ? ` · ${st.fetch_errors} symbols failed to download` : '') +
      (st.missing_symbols?.length ? ` · not found in instrument master: ${st.missing_symbols.join(', ')}` : '') : '';
  }

  async function loadLatest() { try { renderScan(await api('/api/scan/latest')); } catch (e) { toast(e.message, true); } }

  let pollTimer;
  async function pollStatus() {
    const s = await api('/api/scan/status').catch(() => null);
    if (!s) return;
    const p = $('#progress');
    $('#btn-scan').disabled = s.running;
    if (s.running) {
      p.classList.remove('hidden');
      const frac = s.total ? s.done / s.total : 0;
      $('#progress-fill').style.width = Math.round(frac * 100) + '%';
      $('#progress-text').textContent = `${s.phase}: ${s.message} ${s.total ? `(${s.done}/${s.total})` : ''}` + (s.error_count ? ` · ${s.error_count} errors` : '');
      pollTimer = setTimeout(pollStatus, 1500);
    } else {
      if (!p.classList.contains('hidden')) { p.classList.add('hidden'); loadLatest(); toast(s.message || 'Scan finished', s.phase !== 'done'); }
    }
  }
  $('#btn-scan').addEventListener('click', async () => {
    try { await api('/api/scan', { method: 'POST' }); $('#progress').classList.remove('hidden'); pollStatus(); }
    catch (e) { toast(e.message, true); }
  });

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
      ${ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#262d3a"/><text x="${L - 6}" y="${y(v) + 4}" fill="#8b94a7" font-size="11" text-anchor="end">${v.toFixed(2)}x</text>`).join('')}
      <path d="${path('nifty')}" fill="none" stroke="#8b94a7" stroke-width="1.5"/>
      <path d="${path('strategy')}" fill="none" stroke="#3b82f6" stroke-width="2"/>
      <text x="${L}" y="${H - 8}" fill="#8b94a7" font-size="11">${curve[0].date}</text>
      <text x="${W - R}" y="${H - 8}" fill="#8b94a7" font-size="11" text-anchor="end">${curve[curve.length - 1].date}</text>
      <text x="${W - R - 4}" y="${T + 12}" fill="#3b82f6" font-size="11" text-anchor="end">strategy</text>
      <text x="${W - R - 4}" y="${T + 26}" fill="#8b94a7" font-size="11" text-anchor="end">nifty 50</text>
    </svg>`;
  }
  $('#btn-backtest').addEventListener('click', async () => {
    const out = $('#bt-out'); out.textContent = 'Running…'; $('#btn-backtest').disabled = true;
    try {
      const r = await api('/api/backtest', { method: 'POST' });
      if (r.error) { out.textContent = r.error; return; }
      const s = r.strategy, n = r.nifty;
      out.innerHTML = `<div class="muted small">${r.period.from} → ${r.period.to}, ${s.months} months, avg turnover ${r.avg_monthly_turnover_pct}% of positions per month</div>
        <div class="metrics">
          ${metric('CAGR', fmt(s.cagr_pct) + '%', 'Nifty ' + fmt(n.cagr_pct) + '%')}
          ${metric('Avg month', fmt(s.avg_month_pct, 2) + '%', 'Nifty ' + fmt(n.avg_month_pct, 2) + '%')}
          ${metric('Positive months', s.positive_months_pct + '%', 'Nifty ' + n.positive_months_pct + '%')}
          ${metric('Months > 1%', s.months_above_1pct + '%', 'Nifty ' + n.months_above_1pct + '%')}
          ${metric('Max drawdown', fmt(s.max_drawdown_pct) + '%', 'Nifty ' + fmt(n.max_drawdown_pct) + '%')}
          ${metric('Sharpe', fmt(s.sharpe, 2), 'Nifty ' + fmt(n.sharpe, 2))}
          ${metric('Best / worst month', `${fmt(s.best_month_pct)}% / ${fmt(s.worst_month_pct)}%`, `Nifty ${fmt(n.best_month_pct)}% / ${fmt(n.worst_month_pct)}%`)}
          ${metric('Total return', fmt(s.total_return_pct) + '%', 'Nifty ' + fmt(n.total_return_pct) + '%')}
        </div>${chart(r.curve)}
        <details><summary>Monthly returns</summary><div class="table-wrap"><table class="plain"><thead><tr><th>Month end</th><th class="num">Strategy</th><th class="num">Equity</th></tr></thead><tbody>
        ${r.curve.map((c) => `<tr><td>${c.date}</td><td class="num">${pct(c.month_ret)}</td><td class="num">${fmt(c.strategy, 3)}x</td></tr>`).join('')}</tbody></table></div></details>`;
    } catch (e) { out.textContent = e.message; } finally { $('#btn-backtest').disabled = false; }
  });

  // --------------------------------------------------------------- history
  async function loadHistory() {
    const rows = await api('/api/scans').catch(() => []);
    $('#history-body').innerHTML = rows.map((s) => `<tr><td>${new Date(s.run_at).toLocaleString('en-IN')}</td><td>${s.status}</td>
      <td>${s.regime?.state || '-'}</td><td style="white-space:normal">${esc(s.message)}</td>
      <td>${s.status === 'done' ? `<button data-scan="${s.id}">View</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No scans yet.</td></tr>';
  }
  $('#history-body').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-scan]'); if (!b) return;
    const s = await api('/api/scans/' + b.dataset.scan);
    $('#history-detail').innerHTML = `<h2 style="margin-top:16px">Scan #${s.id}</h2><div class="table-wrap"><table class="plain"><thead><tr><th>#</th><th>Symbol</th><th class="num">Close</th><th class="num">Score</th><th class="num">Stop</th><th class="num">Qty</th></tr></thead><tbody>
      ${s.results.map((r) => `<tr><td>${r.rank}</td><td class="sym">${esc(r.symbol)}</td><td class="num">${fmt(r.close, 2)}</td><td class="num">${fmt(r.score, 2)}</td><td class="num">${fmt(r.stop_loss, 2)}</td><td class="num">${r.quantity}</td></tr>`).join('')}</tbody></table></div>`;
  });

  // -------------------------------------------------------------- settings
  const FIELDS = ['api_key', 'redirect_uri', 'universe', 'capital', 'risk_per_trade_pct', 'top_n', 'min_price', 'min_turnover_cr', 'schedule_time'];
  async function loadSettings() {
    const s = await api('/api/settings');
    FIELDS.forEach((k) => { $('#s-' + k).value = s[k] ?? ''; });
    $('#s-schedule_enabled').checked = s.schedule_enabled === '1';
    $('#s-api_secret').placeholder = s.has_api_secret ? '(saved — leave blank to keep)' : 'paste your API secret';
    const ist = (d) => new Date(d).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) + ' IST';
    $('#token-state').textContent = s.has_access_token ? `Access token saved${s.token_issued_at ? ' ' + ist(s.token_issued_at) : ''}. Upstox tokens expire at 3:30 IST daily; log in again when a scan reports "needs login".` : 'No access token yet — save your key and secret, then click Login with Upstox.';
    $('#schedule-state').textContent = s.schedule?.enabled && s.schedule.next_run ? `Next automatic scan: ${ist(s.schedule.next_run)}` : 'Automatic scans disabled';
    const st = await api('/api/status').catch(() => null);
    if (st) $('#data-state').textContent = `Cached: ${st.instruments} NSE instruments, ${st.symbols_with_data} symbols with data, ${st.candle_rows.toLocaleString('en-IN')} daily candles.`;
  }
  $('#btn-save-conn').addEventListener('click', async () => {
    try {
      await api('/api/settings', { method: 'POST', body: JSON.stringify({ values: { api_key: $('#s-api_key').value.trim(), api_secret: $('#s-api_secret').value.trim(), redirect_uri: $('#s-redirect_uri').value.trim() } }) });
      $('#s-api_secret').value = ''; toast('Connection settings saved'); loadSettings();
    } catch (e) { toast(e.message, true); }
  });
  $('#btn-save-strat').addEventListener('click', async () => {
    const values = {};
    ['universe', 'capital', 'risk_per_trade_pct', 'top_n', 'min_price', 'min_turnover_cr', 'schedule_time'].forEach((k) => { values[k] = $('#s-' + k).value.trim(); });
    values.schedule_enabled = $('#s-schedule_enabled').checked ? '1' : '0';
    try { await api('/api/settings', { method: 'POST', body: JSON.stringify({ values }) }); toast('Strategy settings saved'); loadSettings(); }
    catch (e) { toast(e.message, true); }
  });
  $('#btn-token').addEventListener('click', async () => {
    try { const r = await api('/api/token', { method: 'POST', body: JSON.stringify({ access_token: $('#s-token').value }) }); $('#s-token').value = ''; toast('Token verified for ' + (r.user || 'user')); loadSettings(); }
    catch (e) { toast(e.message, true); }
  });

  // ------------------------------------------------------------------ boot
  const q = new URLSearchParams(location.search);
  if (q.get('login') === 'ok') { toast('Logged in to Upstox'); history.replaceState({}, '', '/'); }
  else if (q.get('login') === 'error') { toast('Upstox login failed: ' + (q.get('msg') || ''), true); history.replaceState({}, '', '/'); showTab('settings'); }
  loadLatest(); pollStatus();
})();
