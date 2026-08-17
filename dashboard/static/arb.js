/**
 * Arb Monitor — live gaps, history, pair mapping.
 */
(function () {
  'use strict';

  const ARB_TABS = ['arb-live', 'arb-history', 'arb-pairs', 'arb-config'];
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  let _liveSource = null;

  function updateArbSubtabs(activeTab) {
    $$('.arb-subtab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.atab === activeTab);
    });
  }

  function bindArbSubtabs() {
    $$('.arb-subtab').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.atab;
        if (tab && typeof window.switchTab === 'function') window.switchTab(tab);
      });
    });
  }

  async function arbApi(path, opts = {}) {
    const res = await fetch('/api/arb' + path, {
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
    if (d === 'NSE_HIGH') return 'arb-dir-nse';
    if (d === 'BSE_HIGH') return 'arb-dir-bse';
    return '';
  }

  function gapRow(g, live) {
    const dur = g.duration_sec != null ? g.duration_sec + 's' : (live ? '…' : '—');
    const gapCls = Math.abs(Number(g.gap_pct || 0)) >= 0.5 ? 'arb-gap-high' : '';
    return `<tr>
      <td><strong>${escapeHtml(g.symbol)}</strong></td>
      <td>${escapeHtml(g.started_at || '')}</td>
      <td>${live ? '—' : escapeHtml(g.ended_at || '')}</td>
      <td>${dur}</td>
      <td>${fmtPx(g.nse_ltp)}</td>
      <td>${fmtPx(g.bse_ltp)}</td>
      <td class="${gapCls}">${fmtPct(g.gap_pct)}</td>
      <td class="${dirClass(g.direction)}">${escapeHtml(g.direction || '')}</td>
      <td>${fmtPct(g.max_gap_pct)}</td>
      <td>${g.sample_count ?? '—'}</td>
      <td>${escapeHtml(g.isin || '')}</td>
    </tr>`;
  }

  function gapsTable(gaps, live) {
    if (!gaps || !gaps.length) {
      return '<div class="arb-empty">No gap episodes' + (live ? ' open right now' : ' for these filters') + '.</div>';
    }
    const head = `<thead><tr>
      <th>Symbol</th><th>Started</th><th>Ended</th><th>Duration</th>
      <th>NSE LTP</th><th>BSE LTP</th><th>Gap %</th><th>Direction</th>
      <th>Max gap %</th><th>Samples</th><th>ISIN</th>
    </tr></thead>`;
    const body = gaps.map(g => gapRow(g, live)).join('');
    return `<div class="arb-table-wrap"><table class="arb-table">${head}<tbody>${body}</tbody></table></div>`;
  }

  function renderArbLive(data) {
    const el = $('#arb-live-container');
    const st = $('#arb-live-status');
    if (!el) return;
    el.innerHTML = gapsTable(data.gaps || [], true);
    if (st) {
      const src = data.source || 'db';
      const asOf = data.as_of ? ` · ${data.as_of}` : '';
      st.textContent = `${data.count || 0} open · source: ${src}${asOf}`;
    }
    el.classList.remove('loading');
  }

  async function loadArbLive() {
    const el = $('#arb-live-container');
    if (!el) return;
    try {
      const data = await arbApi('/live/snapshot');
      renderArbLive(data);
    } catch (e) {
      el.innerHTML = `<div class="arb-empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  function ensureArbLiveStream() {
    if (_liveSource && _liveSource.readyState !== 2 /* CLOSED */) return;
    try {
      _liveSource = new EventSource('/api/arb/live/stream');
      _liveSource.onmessage = (ev) => {
        try { renderArbLive(JSON.parse(ev.data)); } catch (_) { /* ignore */ }
      };
      _liveSource.onerror = () => {
        if (_liveSource && _liveSource.readyState === 2) _liveSource = null;
      };
    } catch (_) { /* SSE unsupported — manual refresh only */ }
  }

  function stopArbLiveStream() {
    if (_liveSource) {
      _liveSource.close();
      _liveSource = null;
    }
  }

  async function loadArbHistory() {
    const el = $('#arb-history-container');
    if (!el) return;
    const qs = new URLSearchParams();
    const from = $('#arb-hist-from')?.value;
    const to = $('#arb-hist-to')?.value;
    const sym = ($('#arb-hist-symbol')?.value || '').trim();
    const minGap = $('#arb-hist-min-gap')?.value;
    const minDur = $('#arb-hist-min-dur')?.value;
    if (from) qs.set('from', from);
    if (to) qs.set('to', to);
    if (sym) qs.set('symbol', sym.toUpperCase());
    if (minGap !== '' && minGap != null) qs.set('min_gap_pct', minGap);
    if (minDur !== '' && minDur != null) qs.set('min_duration_sec', minDur);
    qs.set('limit', '300');
    try {
      const data = await arbApi('/gaps?' + qs.toString());
      el.innerHTML = gapsTable(data.gaps || [], false);
      el.classList.remove('loading');
    } catch (e) {
      el.innerHTML = `<div class="arb-empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function loadArbPairs() {
    const el = $('#arb-pairs-container');
    const st = $('#arb-pairs-status');
    if (!el) return;
    try {
      const data = await arbApi('/pairs');
      const rows = data.pairs || [];
      if (!rows.length) {
        el.innerHTML = '<div class="arb-empty">No pairs yet — click Rebuild from master (requires Zerodha login).</div>';
      } else {
        const body = rows.map(p => `<tr>
          <td>${escapeHtml(p.symbol)}</td>
          <td>${escapeHtml(p.nse_symbol)}</td>
          <td>${escapeHtml(p.bse_symbol)}</td>
          <td>${escapeHtml(p.isin || '')}</td>
          <td>${p.active ? 'yes' : 'no'}</td>
        </tr>`).join('');
        el.innerHTML = `<div class="arb-table-wrap"><table class="arb-table">
          <thead><tr><th>Symbol</th><th>NSE</th><th>BSE</th><th>ISIN</th><th>Active</th></tr></thead>
          <tbody>${body}</tbody></table></div>`;
      }
      if (st) st.textContent = `${data.count || rows.length} pairs`;
      el.classList.remove('loading');
    } catch (e) {
      el.innerHTML = `<div class="arb-empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  async function refreshPairs() {
    const st = $('#arb-pairs-status');
    if (st) st.textContent = 'Refreshing…';
    try {
      const data = await arbApi('/pairs/refresh', { method: 'POST', body: '{}' });
      if (typeof window.toast === 'function') {
        window.toast(`Rebuilt ${data.pairs_refreshed} pairs (${data.universe})`, 'ok');
      }
      await loadArbPairs();
    } catch (e) {
      if (typeof window.toast === 'function') window.toast(e.message, 'err');
      if (st) st.textContent = e.message;
    }
  }

  function setArbConfigNotice(msg, ok) {
    const el = $('#arb-config-notice');
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

  function fillArbConfigForm(settings) {
    const form = $('#arb-config-form');
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
    setVal('universe', settings.universe || 'nifty50_dual');
    setVal('tick_staleness_sec', settings.tick_staleness_sec);
    setVal('leg_stale_close_sec', settings.leg_stale_close_sec);
    setVal('min_gap_store_pct', settings.min_gap_store_pct);
    setVal('min_duration_store_sec', settings.min_duration_store_sec);
  }

  function readArbConfigForm() {
    const form = $('#arb-config-form');
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
      leg_stale_close_sec: num('leg_stale_close_sec'),
      min_gap_store_pct: num('min_gap_store_pct'),
      min_duration_store_sec: num('min_duration_store_sec'),
    };
  }

  async function loadArbConfig() {
    const form = $('#arb-config-form');
    if (!form) return;
    setArbConfigNotice('');
    try {
      const data = await arbApi('/config');
      fillArbConfigForm(data.settings || {});
    } catch (e) {
      setArbConfigNotice('Failed to load settings: ' + e.message, false);
    }
  }

  async function saveArbConfig(ev) {
    if (ev) ev.preventDefault();
    const body = readArbConfigForm();
    try {
      const data = await arbApi('/config', {
        method: 'PUT',
        body: JSON.stringify(body),
      });
      fillArbConfigForm(data.settings || body);
      setArbConfigNotice('Settings saved.', true);
      if (typeof window.toast === 'function') window.toast('Arb settings saved', 'info');
    } catch (e) {
      setArbConfigNotice('Save failed: ' + e.message, false);
      if (typeof window.toast === 'function') window.toast('Save failed: ' + e.message, 'err');
    }
  }

  function startLivePoll() {
    stopLivePoll();
    loadArbLive().then(() => ensureArbLiveStream());
  }

  function stopLivePoll() {
    stopArbLiveStream();
  }

  function onArbTabEnter(tab) {
    updateArbSubtabs(tab);
    if (tab === 'arb-live') {
      loadArbLive().then(() => startLivePoll());
    } else {
      stopLivePoll();
      if (tab === 'arb-history') loadArbHistory();
      if (tab === 'arb-pairs') loadArbPairs();
      if (tab === 'arb-config') loadArbConfig();
    }
  }

  function onArbTabLeave() {
    stopLivePoll();
  }

  ARB_TABS.forEach(tab => {
    if (typeof window.registerDashboardTab === 'function') {
      window.registerDashboardTab(tab, () => onArbTabEnter(tab), onArbTabLeave);
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    bindArbSubtabs();
    $('#arb-live-refresh')?.addEventListener('click', loadArbLive);
    $('#arb-hist-apply')?.addEventListener('click', loadArbHistory);
    $('#arb-pairs-refresh')?.addEventListener('click', refreshPairs);
    $('#arb-config-form')?.addEventListener('submit', saveArbConfig);
    $('#arb-config-reload')?.addEventListener('click', () => loadArbConfig());
    const today = new Date();
    const iso = d => d.toISOString().slice(0, 10);
    const fromEl = $('#arb-hist-from');
    const toEl = $('#arb-hist-to');
    if (fromEl && !fromEl.value) fromEl.value = iso(today);
    if (toEl && !toEl.value) toEl.value = iso(today);
  });
})();
