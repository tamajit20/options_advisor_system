/**
 * Intraday Scout — signals, watchlist, trades (Zerodha fills), history.
 */
(function () {
  'use strict';

  const SCOUT_TABS = ['scout-signals', 'scout-watchlist', 'scout-trades', 'scout-history'];
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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

  function fmtPnl(v) {
    if (v == null || isNaN(v)) return '—';
    const n = Number(v);
    const sign = n >= 0 ? '+' : '';
    return sign + '₹' + n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
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

  function toast(msg, kind) {
    if (typeof window.toast === 'function') window.toast(msg, kind || 'info');
  }

  function updateScoutSubtabs(activeTab) {
    $$('.scout-subtab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.stab === activeTab);
    });
  }

  function bindScoutSubtabs() {
    $$('.scout-subtab').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.stab;
        if (tab && typeof window.switchTab === 'function') window.switchTab(tab);
      });
    });
  }

  function bindNavSections() {
    $$('.nav-section').forEach(sec => {
      const id = sec.id;
      if (!id) return;
      try {
        const saved = localStorage.getItem('navSection:' + id);
        if (saved === 'closed') sec.open = false;
      } catch (_) {}
      sec.addEventListener('toggle', () => {
        try {
          localStorage.setItem('navSection:' + id, sec.open ? 'open' : 'closed');
        } catch (_) {}
      });
    });
  }

  // ---------- Signals ----------
  function renderStatus(st) {
    const bar = $('#scout-status-bar');
    if (!bar || !st) return;
    const parts = [];
    parts.push(st.market_open ? '🟢 Market open' : '⚫ Market closed');
    parts.push(st.zerodha_ok ? '🔑 Zerodha OK' : '🔑 ' + (st.zerodha_message || 'Not logged in'));
    parts.push('📡 WebSocket push');
    if (st.last_signal && st.last_signal.triggered_at) {
      parts.push(
        'Last signal: ' + st.last_signal.symbol + ' ' + st.last_signal.action
        + ' (' + ageLabel(st.last_signal.triggered_at) + ')'
      );
    }
    parts.push(st.watchlist_count + ' symbols watched');
    bar.innerHTML = parts.map(p => `<span class="scout-stat">${escapeHtml(p)}</span>`).join('');
  }

  function renderSignals(signals) {
    const c = $('#scout-signals-container');
    if (!c) return;
    if (!signals || !signals.length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No scout signals in the last 2 hours. Ensure <strong>WS Monitor</strong> is connected during market hours.</div>';
      return;
    }
    c.className = 'scout-signal-list';
    c.innerHTML = signals.map(s => {
      const action = (s.action || '').toUpperCase();
      const cls = action === 'BUY' ? 'scout-buy' : action === 'SELL' ? 'scout-sell' : 'scout-wait';
      const strength = (s.strength || 'WEAK').toLowerCase();
      const ltp = Number(s.ltp || 0);
      const markBlock = s.trade_open
        ? '<span class="muted scout-trade-badge">Trade open</span>'
        : `<div class="scout-mark-row">
            <input type="number" step="0.05" class="scout-entry-input" value="${ltp}"
              data-signal-id="${s.id}" aria-label="Entry fill price" title="Your Zerodha fill price">
            <input type="number" step="1" min="1" class="scout-qty-input" value="1"
              data-signal-id="${s.id}" aria-label="Quantity">
            <button type="button" class="btn btn-sm btn-accent scout-mark-btn" data-signal-id="${s.id}">Mark taken</button>
          </div>`;
      return `
        <div class="scout-card ${cls}">
          <div class="scout-card-head">
            <strong class="scout-symbol">${escapeHtml(s.symbol)}</strong>
            <span class="scout-action tag tag-${action === 'BUY' ? 'ok' : action === 'SELL' ? 'err' : 'muted'}">${escapeHtml(action)}</span>
            <span class="scout-strength scout-strength--${strength}">${escapeHtml(s.strength || '')}</span>
          </div>
          <div class="scout-card-body">
            <div class="scout-price">Signal @ ${fmtPx(s.ltp)} <span class="muted scout-age">${escapeHtml(ageLabel(s.triggered_at))}</span></div>
            <div class="scout-reason">${escapeHtml(s.reason)}</div>
            ${s.invalidation != null ? `<div class="scout-inval muted">Invalid if below/above: ${fmtPx(s.invalidation)}</div>` : ''}
            <div class="scout-type muted">${escapeHtml(s.signal_type || '')}</div>
            <div class="scout-card-actions">${markBlock}</div>
          </div>
        </div>`;
    }).join('');

    c.querySelectorAll('.scout-mark-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const sid = btn.getAttribute('data-signal-id');
        const entryInp = c.querySelector(`.scout-entry-input[data-signal-id="${sid}"]`);
        const qtyInp = c.querySelector(`.scout-qty-input[data-signal-id="${sid}"]`);
        const entry = parseFloat(entryInp && entryInp.value);
        const qty = parseInt(qtyInp && qtyInp.value, 10) || 1;
        if (!entry || entry <= 0) {
          toast('Enter your Zerodha entry fill price', 'err');
          return;
        }
        btn.disabled = true;
        try {
          await scoutApi('/signals/' + sid + '/mark-taken', {
            method: 'POST',
            body: JSON.stringify({ entry_price: entry, quantity: qty }),
          });
          toast('Trade marked — see My Trades', 'ok');
          await loadScoutSignals();
          if (typeof window.loadScoutTrades === 'function') window.loadScoutTrades();
        } catch (e) {
          toast(e.message, 'err');
          btn.disabled = false;
        }
      });
    });
  }

  async function loadScoutSignals() {
    const c = $('#scout-signals-container');
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

  // ---------- Watchlist ----------
  let _wlStocks = [];
  let _wlSelected = new Set();
  let _wlNifty50 = [];
  let _wlOffset = 0;
  let _wlLimit = 80;
  let _wlTotal = 0;
  let _wlSearch = '';
  let _wlSearchTimer = null;
  let _wlLoading = false;
  let _wlRefreshedAt = '';

  function renderWatchlistMeta(data) {
    const meta = $('#scout-wl-meta');
    const cnt = $('#scout-wl-count');
    if (meta && data) {
      const parts = [];
      parts.push((data.total_equity_count || 0).toLocaleString() + ' NSE stocks');
      if (data.instrument_refreshed_at) {
        parts.push('master updated ' + data.instrument_refreshed_at);
      }
      if (data.search) parts.push('search: “' + data.search + '”');
      meta.textContent = parts.join(' · ');
    }
    if (cnt) cnt.textContent = _wlSelected.size + ' selected';
    const more = $('#scout-wl-more');
    if (more) {
      const hasMore = _wlOffset + _wlStocks.filter(s => !s.pinned_selected).length < _wlTotal;
      more.hidden = !hasMore;
    }
  }

  function renderWatchlist() {
    const c = $('#scout-watchlist-container');
    if (!c) return;
    if (!_wlStocks.length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No stocks match your search.</div>';
      renderWatchlistMeta(null);
      return;
    }
    c.className = 'scout-wl-grid';
    c.innerHTML = _wlStocks.map(s => `
      <label class="scout-wl-item${s.is_nifty50 ? ' scout-wl-item--n50' : ''}${s.pinned_selected ? ' scout-wl-item--pinned' : ''}">
        <input type="checkbox" class="scout-wl-cb" data-symbol="${escapeHtml(s.symbol)}"
          ${_wlSelected.has(s.symbol) ? 'checked' : ''}>
        <span>
          <span>${escapeHtml(s.symbol)}</span>
          ${s.name ? `<span class="scout-wl-name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>` : ''}
        </span>
      </label>
    `).join('');
    renderWatchlistMeta({
      total_equity_count: _wlTotal,
      search: _wlSearch,
      instrument_refreshed_at: _wlRefreshedAt,
    });
    c.querySelectorAll('.scout-wl-cb').forEach(cb => {
      cb.addEventListener('change', () => {
        const sym = cb.getAttribute('data-symbol');
        if (cb.checked) _wlSelected.add(sym);
        else _wlSelected.delete(sym);
        renderWatchlistMeta({
          total_equity_count: _wlTotal,
          search: _wlSearch,
          instrument_refreshed_at: _wlRefreshedAt,
        });
      });
    });
  }

  async function loadScoutWatchlist(opts = {}) {
    const c = $('#scout-watchlist-container');
    const append = !!opts.append;
    const refresh = !!opts.refresh;
    if (_wlLoading) return;
    _wlLoading = true;
    if (!append && c) {
      c.className = 'loading';
      c.textContent = refresh ? 'Refreshing from Zerodha…' : 'Loading…';
    }
    try {
      if (opts.reset) {
        _wlOffset = 0;
      }
      const q = new URLSearchParams({
        search: _wlSearch,
        offset: String(_wlOffset),
        limit: String(_wlLimit),
      });
      if (refresh) q.set('refresh', '1');
      const data = await scoutApi('/watchlist?' + q.toString());
      _wlNifty50 = data.nifty50 || [];
      _wlTotal = data.total_equity_count || 0;
      _wlRefreshedAt = data.instrument_refreshed_at || '';
      _wlSelected = new Set(data.selected || _wlSelected);
      const page = data.stocks || [];
      if (append) {
        const seen = new Set(_wlStocks.map(s => s.symbol));
        page.forEach(s => { if (!seen.has(s.symbol)) _wlStocks.push(s); });
      } else {
        _wlStocks = page;
      }
      renderWatchlist();
    } catch (e) {
      if (c) c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    } finally {
      _wlLoading = false;
    }
  }

  async function saveWatchlist() {
    try {
      const r = await scoutApi('/watchlist', {
        method: 'PUT',
        body: JSON.stringify({ symbols: Array.from(_wlSelected) }),
      });
      toast('Watchlist saved (' + r.selected_count + ' symbols)', 'ok');
      _wlSelected = new Set(r.selected || []);
      await loadScoutWatchlist({ reset: true });
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  function bindWatchlistToolbar() {
    $('#scout-wl-select-shown')?.addEventListener('click', () => {
      _wlStocks.forEach(s => _wlSelected.add(s.symbol));
      renderWatchlist();
    });
    $('#scout-wl-select-n50')?.addEventListener('click', () => {
      (_wlNifty50.length ? _wlNifty50 : []).forEach(sym => _wlSelected.add(sym));
      renderWatchlist();
      toast('Nifty 50 selected', 'info');
    });
    $('#scout-wl-clear')?.addEventListener('click', () => {
      _wlSelected.clear();
      renderWatchlist();
    });
    $('#scout-wl-save')?.addEventListener('click', () => saveWatchlist());
    $('#scout-wl-refresh-inst')?.addEventListener('click', async () => {
      try {
        await scoutApi('/watchlist/refresh-instruments', { method: 'POST' });
        toast('Instrument list refreshed from Zerodha', 'ok');
        await loadScoutWatchlist({ reset: true, refresh: true });
      } catch (e) {
        toast(e.message, 'err');
      }
    });
    $('#scout-wl-more')?.addEventListener('click', () => {
      _wlOffset += _wlLimit;
      loadScoutWatchlist({ append: true });
    });
    $('#scout-wl-search')?.addEventListener('input', (ev) => {
      _wlSearch = (ev.target.value || '').trim();
      clearTimeout(_wlSearchTimer);
      _wlSearchTimer = setTimeout(() => loadScoutWatchlist({ reset: true }), 300);
    });
  }

  // ---------- My Trades ----------
  function renderOpenTrades(trades) {
    const c = $('#scout-trades-container');
    if (!c) return;
    if (!trades || !trades.length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No open trades. Take a signal from the <strong>Signals</strong> tab after placing the order in Zerodha.</div>';
      return;
    }
    c.className = 'scout-trade-list';
    c.innerHTML = trades.map(t => {
      const action = (t.action || '').toUpperCase();
      const sig = t.signal || {};
      return `
        <div class="scout-card scout-trade-card">
          <div class="scout-card-head">
            <strong>${escapeHtml(t.symbol)}</strong>
            <span class="tag tag-${action === 'BUY' ? 'ok' : 'err'}">${escapeHtml(action)}</span>
            <span class="muted">${escapeHtml(t.signal_type || '')}</span>
          </div>
          <div class="scout-card-body">
            <div>Entry fill: ${fmtPx(t.entry_price)} × ${t.quantity || 1} · ${escapeHtml(ageLabel(t.executed_at))}</div>
            ${sig.reason ? `<div class="muted scout-reason">${escapeHtml(sig.reason)}</div>` : ''}
            <div class="scout-close-row">
              <label class="muted">Exit fill (Zerodha)</label>
              <input type="number" step="0.05" class="scout-exit-input" placeholder="Exit price"
                data-trade-id="${t.id}" aria-label="Exit fill price">
              <button type="button" class="btn btn-sm btn-accent scout-close-btn" data-trade-id="${t.id}">Close trade</button>
              <button type="button" class="btn btn-sm btn-ghost scout-void-btn" data-trade-id="${t.id}">Void</button>
            </div>
          </div>
        </div>`;
    }).join('');

    c.querySelectorAll('.scout-close-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const tid = btn.getAttribute('data-trade-id');
        const inp = c.querySelector(`.scout-exit-input[data-trade-id="${tid}"]`);
        const px = parseFloat(inp && inp.value);
        if (!px || px <= 0) {
          toast('Enter your Zerodha exit fill price', 'err');
          return;
        }
        btn.disabled = true;
        try {
          await scoutApi('/trades/' + tid + '/close', {
            method: 'POST',
            body: JSON.stringify({ exit_price: px }),
          });
          toast('Trade closed', 'ok');
          await loadScoutTrades();
          if (typeof window.loadScoutHistory === 'function') window.loadScoutHistory();
        } catch (e) {
          toast(e.message, 'err');
          btn.disabled = false;
        }
      });
    });

    c.querySelectorAll('.scout-void-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const tid = btn.getAttribute('data-trade-id');
        if (!confirm('Remove this open trade record?')) return;
        try {
          await scoutApi('/trades/' + tid, { method: 'DELETE' });
          toast('Trade voided', 'info');
          await loadScoutTrades();
          await loadScoutSignals();
        } catch (e) {
          toast(e.message, 'err');
        }
      });
    });
  }

  async function loadScoutTrades() {
    const c = $('#scout-trades-container');
    try {
      const data = await scoutApi('/trades/open');
      renderOpenTrades(data.trades || []);
    } catch (e) {
      if (c) c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------- History ----------
  function defaultHistDates() {
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - 30);
    const fmt = d => d.toISOString().slice(0, 10);
    const fEl = $('#scout-hist-from');
    const tEl = $('#scout-hist-to');
    if (fEl && !fEl.value) fEl.value = fmt(from);
    if (tEl && !tEl.value) tEl.value = fmt(to);
    return { from: fEl?.value || fmt(from), to: tEl?.value || fmt(to) };
  }

  function renderHistoryStats(stats) {
    const el = $('#scout-history-stats');
    if (!el || !stats) return;
    el.className = 'scout-history-stats';
    const types = stats.by_signal_type || {};
    const typeRows = Object.keys(types).sort().map(k => {
      const b = types[k];
      const wr = b.count ? Math.round(b.wins / b.count * 100) : 0;
      return `<tr><td>${escapeHtml(k)}</td><td>${b.count}</td><td>${wr}%</td><td>${fmtPnl(b.pnl)}</td></tr>`;
    }).join('');
    el.innerHTML = `
      <div class="scout-stats-grid">
        <div class="scout-stat-box"><span class="muted">Trades</span><strong>${stats.total_trades || 0}</strong></div>
        <div class="scout-stat-box"><span class="muted">Win rate</span><strong>${stats.win_rate_pct || 0}%</strong></div>
        <div class="scout-stat-box"><span class="muted">Total P&amp;L</span><strong class="${(stats.total_pnl || 0) >= 0 ? 'pnl-profit' : 'pnl-loss'}">${fmtPnl(stats.total_pnl)}</strong></div>
        <div class="scout-stat-box"><span class="muted">Avg P&amp;L</span><strong>${fmtPnl(stats.avg_pnl)}</strong></div>
      </div>
      ${typeRows ? `<table class="scout-stats-table"><thead><tr><th>Signal type</th><th>Count</th><th>Win%</th><th>P&amp;L</th></tr></thead><tbody>${typeRows}</tbody></table>` : ''}`;
  }

  function renderHistoryTrades(trades) {
    const c = $('#scout-history-container');
    if (!c) return;
    if (!trades || !trades.length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No closed trades in this period.</div>';
      return;
    }
    c.className = 'scout-trade-list';
    c.innerHTML = trades.map(t => {
      const pnl = Number(t.pnl || 0);
      const cls = pnl >= 0 ? 'pnl-profit' : 'pnl-loss';
      const closed = t.closed_at || t.exited_at || '';
      return `
        <div class="scout-card">
          <div class="scout-card-head">
            <strong>${escapeHtml(t.symbol)}</strong>
            <span class="tag tag-${(t.action || '').toUpperCase() === 'BUY' ? 'ok' : 'err'}">${escapeHtml(t.action)}</span>
            <span class="muted">${escapeHtml(t.signal_type || '')}</span>
          </div>
          <div class="scout-card-body">
            <div>${fmtPx(t.entry_price)} → ${fmtPx(t.exit_price)} · ${escapeHtml(String(closed).slice(0, 16))}</div>
            <div class="${cls}"><strong>${fmtPnl(t.pnl)}</strong> (${Number(t.pnl_pct || 0).toFixed(2)}%)</div>
            ${t.signal_reason ? `<div class="muted scout-reason">${escapeHtml(t.signal_reason)}</div>` : ''}
          </div>
        </div>`;
    }).join('');
  }

  async function loadScoutHistory() {
    const dates = defaultHistDates();
    const q = '?from_date=' + encodeURIComponent(dates.from) + '&to_date=' + encodeURIComponent(dates.to);
    try {
      const [stats, hist] = await Promise.all([
        scoutApi('/history/stats' + q),
        scoutApi('/history/trades' + q),
      ]);
      renderHistoryStats(stats);
      renderHistoryTrades(hist.trades || []);
    } catch (e) {
      const c = $('#scout-history-container');
      if (c) c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------- Tab lifecycle ----------
  let _scoutTimer = null;

  function stopScoutAutoRefresh() {
    if (_scoutTimer) {
      clearInterval(_scoutTimer);
      _scoutTimer = null;
    }
  }

  function startScoutAutoRefresh() {
    stopScoutAutoRefresh();
    _scoutTimer = setInterval(loadScoutSignals, 60000);
  }

  function onScoutTabEnter(tab) {
    updateScoutSubtabs(tab);
    if (tab === 'scout-signals') {
      loadScoutSignals();
      startScoutAutoRefresh();
    } else {
      stopScoutAutoRefresh();
      if (tab === 'scout-watchlist') {
        _wlSearch = ($('#scout-wl-search')?.value || '').trim();
        loadScoutWatchlist({ reset: true });
      }
      if (tab === 'scout-trades') loadScoutTrades();
      if (tab === 'scout-history') loadScoutHistory();
    }
  }

  function onScoutTabLeave() {
    stopScoutAutoRefresh();
  }

  SCOUT_TABS.forEach(tab => {
    if (typeof window.registerDashboardTab === 'function') {
      window.registerDashboardTab(tab, () => onScoutTabEnter(tab), onScoutTabLeave);
    }
  });

  window.loadScoutSignals = loadScoutSignals;
  window.loadScoutTrades = loadScoutTrades;
  window.loadScoutHistory = loadScoutHistory;

  document.addEventListener('DOMContentLoaded', () => {
    bindScoutSubtabs();
    bindNavSections();
    bindWatchlistToolbar();
    $('#scout-refresh')?.addEventListener('click', () => loadScoutSignals());
    $('#scout-hist-apply')?.addEventListener('click', () => loadScoutHistory());
  });
})();
