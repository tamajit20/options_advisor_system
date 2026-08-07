/**
 * Intraday Scout — standalone UI (Options Advisor code is not imported here).
 */
(function () {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);

  async function scoutApi(path, opts = {}) {
    const res = await fetch('/api/scout' + path, {
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
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

  function ageLabel(iso) {
    if (!iso) return '';
    const t = new Date(String(iso).replace(' ', 'T'));
    if (isNaN(t.getTime())) return '';
    const sec = Math.round((Date.now() - t.getTime()) / 1000);
    if (sec < 60) return sec + 's ago';
    if (sec < 3600) return Math.round(sec / 60) + 'm ago';
    return Math.round(sec / 3600) + 'h ago';
  }

  function renderStatus(st) {
    const bar = $('#scout-status-bar');
    if (!bar || !st) return;
    const parts = [];
    parts.push(st.market_open ? '🟢 Market open' : '⚫ Market closed');
    parts.push(st.zerodha_ok ? '🔑 Zerodha OK' : '🔑 ' + (st.zerodha_message || 'Not logged in'));
    if (st.last_scan && st.last_scan.finished_at) {
      parts.push('Last scan: ' + st.last_scan.signals_found + ' signal(s)');
    }
    parts.push(st.watchlist_count + ' symbols watched');
    bar.innerHTML = parts.map(p => `<span class="scout-stat">${escapeHtml(p)}</span>`).join('');
  }

  function renderSignals(signals) {
    const c = $('#scout-container');
    if (!c) return;
    if (!signals || !signals.length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No scout signals in the last 2 hours. Try <strong>Scan now</strong> during market hours.</div>';
      return;
    }
    c.className = 'scout-signal-list';
    c.innerHTML = signals.map(s => {
      const action = (s.action || '').toUpperCase();
      const cls = action === 'BUY' ? 'scout-buy' : action === 'SELL' ? 'scout-sell' : 'scout-wait';
      const strength = (s.strength || 'WEAK').toLowerCase();
      return `
        <div class="scout-card ${cls}">
          <div class="scout-card-head">
            <strong class="scout-symbol">${escapeHtml(s.symbol)}</strong>
            <span class="scout-action tag tag-${action === 'BUY' ? 'ok' : action === 'SELL' ? 'err' : 'muted'}">${escapeHtml(action)}</span>
            <span class="scout-strength scout-strength--${strength}">${escapeHtml(s.strength || '')}</span>
          </div>
          <div class="scout-card-body">
            <div class="scout-price">${fmtPx(s.ltp)} <span class="muted scout-age">${escapeHtml(ageLabel(s.triggered_at))}</span></div>
            <div class="scout-reason">${escapeHtml(s.reason)}</div>
            ${s.invalidation != null ? `<div class="scout-inval muted">Invalid if below/above: ${fmtPx(s.invalidation)}</div>` : ''}
            <div class="scout-type muted">${escapeHtml(s.signal_type || '')}</div>
          </div>
        </div>`;
    }).join('');
  }

  let _scoutTimer = null;

  async function loadScout() {
    const c = $('#scout-container');
    try {
      const [st, sig] = await Promise.all([
        scoutApi('/status'),
        scoutApi('/signals?limit=40&since_minutes=120'),
      ]);
      renderStatus(st);
      renderSignals(sig.signals || []);
    } catch (e) {
      if (c) {
        c.className = '';
        c.innerHTML = `<div class="empty">Scout error: ${escapeHtml(e.message)}</div>`;
      }
    }
  }

  function stopScoutAutoRefresh() {
    if (_scoutTimer) {
      clearInterval(_scoutTimer);
      _scoutTimer = null;
    }
  }

  function startScoutAutoRefresh() {
    stopScoutAutoRefresh();
    _scoutTimer = setInterval(loadScout, 60000);
  }

  function bindScoutUi() {
    $('#scout-refresh')?.addEventListener('click', () => loadScout());
    $('#scout-scan-now')?.addEventListener('click', async () => {
      const btn = $('#scout-scan-now');
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Scanning…';
      }
      try {
        const r = await scoutApi('/scan', { method: 'POST' });
        if (typeof window.toast === 'function') {
          window.toast('Scout scan done — ' + (r.signals_found || 0) + ' signal(s)', 'info');
        }
        await loadScout();
      } catch (e) {
        if (typeof window.toast === 'function') window.toast(e.message, 'err');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = 'Scan now';
        }
      }
    });
  }

  function onScoutTabActive() {
    loadScout();
    startScoutAutoRefresh();
  }

  function onScoutTabLeave() {
    stopScoutAutoRefresh();
  }

  window.loadScout = loadScout;
  window.onScoutTabActive = onScoutTabActive;
  window.onScoutTabLeave = onScoutTabLeave;

  document.addEventListener('DOMContentLoaded', () => {
    bindScoutUi();
    if (typeof window.registerDashboardTab === 'function') {
      window.registerDashboardTab('scout', onScoutTabActive, onScoutTabLeave);
    }
  });
})();
