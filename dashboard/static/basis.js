/**
 * Cash-Futures Basis Monitor — live basis, history, pair mapping.
 */
(function () {
  'use strict';

  const BASIS_TABS = ['basis-live', 'basis-history', 'basis-pairs', 'basis-config'];
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  let _liveSource = null;

  function updateBasisSubtabs(activeTab) {
    $$('.basis-subtab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.btab === activeTab);
    });
  }

  function bindBasisSubtabs() {
    $$('.basis-subtab').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.btab;
        if (tab && typeof window.switchTab === 'function') window.switchTab(tab);
      });
    });
  }

  async function basisApi(path, opts = {}) {
    const res = await fetch('/api/basis' + path, {
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
      ...opts,
    });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) {
      if (!res.ok) throw new Error(res.statusText || 'Request failed');
    }
    if (!res.ok) throw new Error(data.error || res.statusText || 'Request failed');
    return data;
  }

  function escapeHtml(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtPx(v) {
    if (v == null || isNaN(v)) return '—';
    return '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }

  function fmtPct(v) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toFixed(2) + '%';
  }

  function dirClass(d) {
    if (d === 'CONTANGO') return 'basis-dir-contango';
    if (d === 'BACKWARDATION') return 'basis-dir-backwardation';
    return '';
  }

  function basisRow(b, live) {
    const dur = b.duration_sec != null ? b.duration_sec + 's' : (live ? '…' : '—');
    const peakPct = Math.max(Math.abs(Number(b.basis_pct || 0)), Math.abs(Number(b.max_basis_pct || 0)));
    const peakCls = peakPct >= 0.5 ? 'basis-pct-high' : '';
    return `<tr>
      <td><strong>${escapeHtml(b.symbol)}</strong></td>
      <td>${escapeHtml(b.fut_expiry || '')}</td>
      <td>${escapeHtml(b.started_at || '')}</td>
      <td>${live ? '—' : escapeHtml(b.ended_at || '')}</td>
      <td>${dur}</td>
      <td>${fmtPx(b.spot_ltp)}</td>
      <td>${fmtPx(b.fut_ltp)}</td>
      <td>${fmtPct(b.basis_pct)}</td>
      <td>${fmtPct(b.annualized_pct)}</td>
      <td class="${dirClass(b.direction)}">${escapeHtml(b.direction || '')}</td>
      <td class="${peakCls}">${fmtPct(b.max_basis_pct)}</td>
      <td>${b.sample_count ?? '—'}</td>
    </tr>`;
  }

  function basisTable(rows, live) {
    if (!rows || !rows.length) {
      return '<div class="basis-empty">No basis episodes' + (live ? ' open right now' : ' for these filters') + '.</div>';
    }
    const pctCol = live ? 'Basis %' : 'Basis % at close';
    const head = `<thead><tr>
      <th>Symbol</th><th>FUT expiry</th><th>Started</th><th>Ended</th><th>Duration</th>
      <th>Spot LTP</th><th>FUT LTP</th><th>${pctCol}</th><th>Ann. %</th><th>Direction</th>
      <th>Peak |basis| %</th><th>Samples</th>
    </tr></thead>`;
    const body = rows.map(b => basisRow(b, live)).join('');
    return `<div class="basis-table-wrap"><table class="basis-table">${head}<tbody>${body}</tbody></table></div>`;
  }

  function renderBasisLive(data) {
    const el = $('#basis-live-container');
    const st = $('#basis-live-status');
    if (!el) return;
    el.innerHTML = basisTable(data.basis || [], true);
    if (st) {
      const src = data.source || 'db';
      const asOf = data.as_of ? ` · ${data.as_of}` : '';
      st.textContent = `${data.count || 0} open · source: ${src}${asOf}`;
    }
    el.classList.remove('loading');
  }

  async function loadBasisLive() {
    const el = $('#basis-live-container');
    if (!el) return;
    try {
      const data = await basisApi('/live/snapshot');
      renderBasisLive(data);
    } catch (e) {
      el.innerHTML = `<div class="basis-empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  function ensureBasisLiveStream() {
    if (_liveSource && _liveSource.readyState !== 2) return;
    try {
      _liveSource = new EventSource('/api/basis/live/stream');
      _liveSource.onmessage = (ev) => {
        try { renderBasisLive(JSON.parse(ev.data)); } catch (_) { /* ignore */ }
      };
      _liveSource.onerror = () => {
        if (_liveSource && _liveSource.readyState === 2) _liveSource = null;
      };
    } catch (_) { /* SSE unsupported */ }
  }

  function stopBasisLiveStream() {
    if (_liveSource) {
      _liveSource.close();
      _liveSource = null;
    }
  }

  async function loadBasisHistory() {
    const el = $('#basis-history-container');
    if (!el) return;
    const qs = new URLSearchParams();
    const from = $('#basis-hist-from')?.value;
    const to = $('#basis-hist-to')?.value;
    const sym = ($('#basis-hist-symbol')?.value || '').trim();
    const minPct = $('#basis-hist-min-pct')?.value;
    const minDur = $('#basis-hist-min-dur')?.value;
    if (from) qs.set('from', from);
    if (to) qs.set('to', to);
    if (sym) qs.set('symbol', sym.toUpperCase());
    if (minPct !== '' && minPct != null) qs.set('min_basis_pct', minPct);
    if (minDur !== '' && minDur != null) qs.set('min_duration_sec', minDur);
    qs.set('limit', '300');
    try {
      const data = await basisApi('/episodes/history?' + qs.toString());
      el.innerHTML = basisTable(data.episodes || [], false);
      el.classList.remove('loading');
    } catch (e) {
      el.innerHTML = `<div class="basis-empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function loadBasisPairs() {
    const el = $('#basis-pairs-container');
    const st = $('#basis-pairs-status');
    if (!el) return;
    try {
      const data = await basisApi('/pairs');
      const rows = data.pairs || [];
      if (!rows.length) {
        el.innerHTML = '<div class="basis-empty">No pairs yet — click Rebuild from master (requires Zerodha login).</div>';
      } else {
        const body = rows.map(p => `<tr>
          <td>${escapeHtml(p.symbol)}</td>
          <td>${escapeHtml(p.spot_symbol)}</td>
          <td>${escapeHtml(p.fut_symbol)}</td>
          <td>${escapeHtml(p.fut_expiry || '')}</td>
          <td>${p.active ? 'yes' : 'no'}</td>
        </tr>`).join('');
        el.innerHTML = `<div class="basis-table-wrap"><table class="basis-table">
          <thead><tr><th>Symbol</th><th>Spot</th><th>FUT</th><th>Expiry</th><th>Active</th></tr></thead>
          <tbody>${body}</tbody></table></div>`;
      }
      if (st) st.textContent = `${data.count || rows.length} pairs`;
      el.classList.remove('loading');
    } catch (e) {
      el.innerHTML = `<div class="basis-empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function refreshBasisPairs() {
    const st = $('#basis-pairs-status');
    if (st) st.textContent = 'Refreshing…';
    try {
      const data = await basisApi('/pairs/refresh', { method: 'POST', body: '{}' });
      if (typeof window.toast === 'function') {
        window.toast(`Rebuilt ${data.pairs_refreshed} pairs (${data.universe})`, 'ok');
      }
      await loadBasisPairs();
    } catch (e) {
      if (typeof window.toast === 'function') window.toast(e.message, 'err');
      if (st) st.textContent = e.message;
    }
  }

  function setBasisConfigNotice(msg, ok) {
    const el = $('#basis-config-notice');
    if (!el) return;
    if (!msg) {
      el.hidden = true;
      el.textContent = '';
      el.classList.remove('scout-config-notice--ok');
      return;
    }
    el.hidden = false;
    el.textContent = msg;
    el.classList.toggle('scout-config-notice--ok', !!ok);
  }

  function fillBasisConfigForm(settings) {
    const form = $('#basis-config-form');
    if (!form || !settings) return;
    const setCheck = (name, val) => {
      const el = form.querySelector(`[name="${name}"]`);
      if (el && el.type === 'checkbox') el.checked = !!val;
    };
    const setVal = (name, val) => {
      const el = form.querySelector(`[name="${name}"]`);
      if (!el) return;
      if (el.tagName === 'SELECT') {
        el.value = val != null ? String(val) : el.options[0]?.value || '';
      } else if (el.type !== 'checkbox') {
        el.value = val != null ? String(val) : '';
      }
    };
    setCheck('enabled', settings.enabled !== false);
    setVal('universe', settings.universe || 'nifty50_fo');
    setVal('tick_staleness_sec', settings.tick_staleness_sec);
    setVal('min_basis_store_pct', settings.min_basis_store_pct);
    setVal('min_duration_store_sec', settings.min_duration_store_sec);
  }

  function readBasisConfigForm() {
    const form = $('#basis-config-form');
    if (!form) return {};
    const num = (name) => {
      const el = form.querySelector(`[name="${name}"]`);
      if (!el) return undefined;
      if (el.type === 'checkbox') return el.checked;
      if (el.type === 'number') return el.value === '' ? undefined : Number(el.value);
      return el.value;
    };
    return {
      enabled: !!form.querySelector('[name="enabled"]')?.checked,
      universe: form.querySelector('[name="universe"]')?.value,
      tick_staleness_sec: num('tick_staleness_sec'),
      min_basis_store_pct: num('min_basis_store_pct'),
      min_duration_store_sec: num('min_duration_store_sec'),
    };
  }

  async function loadBasisConfig() {
    const form = $('#basis-config-form');
    if (!form) return;
    setBasisConfigNotice('');
    try {
      const data = await basisApi('/config');
      fillBasisConfigForm(data.settings || {});
    } catch (e) {
      setBasisConfigNotice('Failed to load settings: ' + e.message, false);
    }
  }

  async function saveBasisConfig(ev) {
    if (ev) ev.preventDefault();
    const body = readBasisConfigForm();
    try {
      const data = await basisApi('/config', {
        method: 'PUT',
        body: JSON.stringify(body),
      });
      fillBasisConfigForm(data.settings || body);
      setBasisConfigNotice('Settings saved.', true);
      if (typeof window.toast === 'function') window.toast('Basis settings saved', 'info');
    } catch (e) {
      setBasisConfigNotice('Save failed: ' + e.message, false);
      if (typeof window.toast === 'function') window.toast('Save failed: ' + e.message, 'err');
    }
  }

  function startLivePoll() {
    stopLivePoll();
    loadBasisLive().then(() => ensureBasisLiveStream());
  }

  function stopLivePoll() {
    stopBasisLiveStream();
  }

  function onBasisTabEnter(tab) {
    updateBasisSubtabs(tab);
    if (tab === 'basis-live') {
      loadBasisLive().then(() => startLivePoll());
    } else {
      stopLivePoll();
      if (tab === 'basis-history') loadBasisHistory();
      if (tab === 'basis-pairs') loadBasisPairs();
      if (tab === 'basis-config') loadBasisConfig();
    }
  }

  function onBasisTabLeave() {
    stopLivePoll();
  }

  BASIS_TABS.forEach(tab => {
    if (typeof window.registerDashboardTab === 'function') {
      window.registerDashboardTab(tab, () => onBasisTabEnter(tab), onBasisTabLeave);
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    bindBasisSubtabs();
    $('#basis-live-refresh')?.addEventListener('click', loadBasisLive);
    $('#basis-hist-apply')?.addEventListener('click', loadBasisHistory);
    $('#basis-pairs-refresh')?.addEventListener('click', refreshBasisPairs);
    $('#basis-config-form')?.addEventListener('submit', saveBasisConfig);
    $('#basis-config-reload')?.addEventListener('click', () => loadBasisConfig());
    const today = new Date();
    const iso = d => d.toISOString().slice(0, 10);
    const fromEl = $('#basis-hist-from');
    const toEl = $('#basis-hist-to');
    if (fromEl && !fromEl.value) fromEl.value = iso(today);
    if (toEl && !toEl.value) toEl.value = iso(today);
  });
})();
