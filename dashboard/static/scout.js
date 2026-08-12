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
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_) {
      if (!res.ok) {
        throw new Error(res.statusText || 'Request failed');
      }
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

  function fmtTimeShort(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso.replace(' ', 'T'));
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (_) {
      return iso;
    }
  }

  function fmtPct(v) {
    if (v == null || isNaN(v)) return '—';
    const n = Number(v);
    const sign = n >= 0 ? '+' : '';
    return sign + n.toFixed(2) + '%';
  }

  function fmtNum(v, digits) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toLocaleString('en-IN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function fmtTimer(secs) {
    const s = Math.max(0, parseInt(secs, 10) || 0);
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m + ':' + String(r).padStart(2, '0');
  }

  const SCOUT_HINTS = {
    LIVE: 'Last traded price from WebSocket ticks — updates every few seconds without refresh.',
    TRIG: 'Price when the 1-minute bar closed and this signal was triggered.',
    BAND: 'Price band — enter the trade only while live price stays inside this range.',
    STOP: 'Invalidation stop — signal is removed if price crosses below (BUY) or above (SELL) this level.',
    TARGET: 'Profit target at 1.5× risk (entry to stop). Book full or partial here.',
    'EXIT BY': 'Intraday square-off — close the position before this time (default 15:15 IST).',
    ENTRY: 'Your Zerodha fill price when you marked the trade taken.',
    '→ Target': 'Distance from live price to the profit target.',
    STRUCT: 'Measured-move target from the pattern (OR/box height projected from breakout).',
    Stock: 'This stock\'s percentage change from today\'s opening price.',
    Nifty: 'Nifty 50 index percentage change from today\'s open.',
    RS: 'Relative strength = stock % − Nifty %. Positive means the stock is outperforming the index.',
    '⏱': 'Time left before this signal expires and disappears from the list.',
    '→ Stop': 'Distance from live price to the invalidation stop — cushion before the signal is killed.',
    OR: 'Opening range (first 15 minutes) — high and low levels that were broken.',
    BOX: 'Compression box — tight range the price consolidated in before breaking out.',
    Move: 'How far the stock had moved from open when the pullback signal fired.',
    band: 'Live price is inside the entry band (green = OK to enter).',
    time: 'Signal is still within its validity window.',
    stop: 'Price has not hit the invalidation stop yet.',
  };

  const SCOUT_SETUP_HINTS = {
    'OR ↑': 'Opening range breakout to the upside — price cleared the first 15m high.',
    'OR ↓': 'Opening range breakdown — price broke below the first 15m low.',
    'BOX ↑': 'Compression breakout up — price escaped a tight sideways box.',
    'BOX ↓': 'Compression breakdown — price broke down from a tight range.',
    'PB ↑': 'Pullback buy — uptrend paused, bullish reversal candle on 1m.',
    'PB ↓': 'Pullback sell — downtrend pause, bearish reversal candle on 1m.',
  };

  function scoutHint(key) {
    return SCOUT_HINTS[key] || SCOUT_SETUP_HINTS[key] || '';
  }

  function renderMetricTile(label, value, extraCls, hintKey) {
    const hint = scoutHint(hintKey || label);
    const titleAttr = hint ? ` title="${escapeHtml(hint)}"` : '';
    return `<div class="scout-metric${extraCls ? ' ' + extraCls : ''}"${titleAttr} tabindex="0">
      <span class="scout-metric-k">${escapeHtml(label)}</span>
      <span class="scout-metric-v">${value}</span>
    </div>`;
  }

  function renderStatusPills(gates) {
    if (!gates) return '';
    const items = [
      { id: 'band', label: 'Band', ok: gates.band_ok },
      { id: 'time', label: 'Time', ok: gates.time_ok },
      { id: 'stop', label: 'Stop', ok: gates.stop_ok },
    ];
    return `<div class="scout-gates">${items.map(it => {
      const cls = it.ok == null ? 'scout-gate--na' : (it.ok ? 'scout-gate--ok' : 'scout-gate--bad');
      const sym = it.ok == null ? '·' : (it.ok ? '✓' : '✗');
      const hint = scoutHint(it.id);
      const titleAttr = hint ? ` title="${escapeHtml(hint)}"` : '';
      return `<span class="scout-gate ${cls}" data-gate-id="${it.id}"${titleAttr} tabindex="0">${sym} ${escapeHtml(it.label)}</span>`;
    }).join('')}</div>`;
  }

  function renderSignalDashboard(s) {
    const d = s.dashboard || {};
    const p = d.prices || {};
    const action = (s.action || '').toUpperCase();
    const live = p.live != null ? Number(p.live) : null;
    const inBand = live != null && live >= Number(s.entry_min) && live <= Number(s.entry_max);
    const liveCls = live == null ? 'scout-metric--muted' : (inBand ? 'scout-metric--live-ok' : 'scout-metric--live-warn');

    let levelsHtml = '';
    if (d.levels) {
      const lv = d.levels;
      const extra = lv.range_pct != null ? ` · ${fmtNum(lv.range_pct, 2)}%` : '';
      levelsHtml = renderMetricTile(lv.kind, `${fmtPx(lv.low)} – ${fmtPx(lv.high)}${extra}`, 'scout-metric--wide');
    } else if (d.move_from_open_pct != null) {
      levelsHtml = renderMetricTile('Move', fmtPct(d.move_from_open_pct));
    }

    const statsHtml = (d.stats || []).map(st => {
      const raw = st.raw != null ? Number(st.raw) : 0;
      const cls = st.key === 'rs' ? (raw >= 0 ? 'scout-metric--pos' : 'scout-metric--neg') : '';
      return renderMetricTile(st.label, escapeHtml(st.value || fmtPct(st.raw)), cls, st.label);
    }).join('');

    const stopDist = d.stop_dist;
    const stopDistHtml = stopDist
      ? renderMetricTile('→ Stop', `${fmtPx(stopDist.rs)} · ${fmtPct(stopDist.pct)}`)
      : '';

    return `
      <div class="scout-dash">
        <div class="scout-dash-prices">
          ${renderMetricTile('LIVE', `<strong class="scout-live-val">${live != null ? fmtPx(live) : '—'}</strong>`, liveCls)}
          ${renderMetricTile('TRIG', fmtPx(p.trigger))}
          ${renderMetricTile('BAND', `${fmtPx(p.band_lo)} – ${fmtPx(p.band_hi)}`, 'scout-metric--wide')}
          ${p.stop != null ? renderMetricTile('STOP', fmtPx(p.stop)) : ''}
        </div>
        <div class="scout-dash-stats">
          ${statsHtml}
          ${renderMetricTile('⏱', `<span class="scout-timer" data-timer-secs="${d.timer_secs || 0}">${fmtTimer(d.timer_secs)}</span> → ${escapeHtml(d.timer_until || '')}`, 'scout-metric--timer')}
          ${stopDistHtml}
          ${levelsHtml}
        </div>
        ${renderStatusPills(d.gates)}
      </div>`;
  }

  function renderSingleSignalCard(s) {
    const action = (s.action || '').toUpperCase();
    const cls = action === 'BUY' ? 'scout-buy' : action === 'SELL' ? 'scout-sell' : 'scout-wait';
    const strength = (s.strength || 'WEAK').toLowerCase();
    const d = s.dashboard || {};
    const setupCode = d.setup_code || (s.signal_type || '').replace(/_/g, ' ');
    const entryDefault = s.live_ltp != null ? Number(s.live_ltp) : Number(s.ltp || 0);
    const markBlock = s.trade_open
      ? '<span class="muted scout-trade-badge">Trade open</span>'
      : `<div class="scout-mark-row">
          <input type="number" step="0.05" class="scout-entry-input" value="${entryDefault}"
            data-signal-id="${s.id}" aria-label="Entry fill price" title="Your Zerodha fill price">
          <input type="number" step="1" min="1" class="scout-qty-input" value="1"
            data-signal-id="${s.id}" aria-label="Quantity">
          <button type="button" class="btn btn-sm btn-accent scout-mark-btn" data-signal-id="${s.id}">Mark taken</button>
        </div>`;
    const setupHint = scoutHint(setupCode);
    const setupTitle = setupHint ? ` title="${escapeHtml(setupHint)}"` : '';
    return `
      <div class="scout-card ${cls}" data-signal-id="${s.id}" data-symbol="${escapeHtml(s.symbol)}"
        data-action="${escapeHtml(action)}"
        data-entry-min="${s.entry_min}" data-entry-max="${s.entry_max}"
        data-invalidation="${s.invalidation != null ? s.invalidation : ''}"
        data-valid-until="${escapeHtml(s.valid_until || '')}"
        data-trade-open="${s.trade_open ? '1' : '0'}">
        <div class="scout-card-head">
          <strong class="scout-symbol">${escapeHtml(s.symbol)}</strong>
          <span class="scout-action tag tag-${action === 'BUY' ? 'ok' : action === 'SELL' ? 'err' : 'muted'}" title="Suggested direction for this intraday setup">${escapeHtml(action)}</span>
          <span class="scout-setup-code"${setupTitle} tabindex="0">${escapeHtml(setupCode)}</span>
          <span class="scout-strength scout-strength--${strength}" title="Signal strength from pattern quality and relative strength">${escapeHtml(s.strength || '')}</span>
          <span class="muted scout-age">${escapeHtml(ageLabel(s.triggered_at))}</span>
        </div>
        <div class="scout-card-body">
          ${renderSignalDashboard(s)}
          <div class="scout-card-actions">${markBlock}</div>
        </div>
      </div>`;
  }

  function showEmptySignals() {
    const c = $('#scout-signals-container');
    if (!c) return;
    c.className = '';
    c.innerHTML = '<div class="empty">No actionable scout signals right now. Valid signals appear during market hours; expired or invalid ones drop off automatically.</div>';
  }

  function bindSignalMarkButtons(root) {
    (root || document).querySelectorAll('.scout-mark-btn').forEach(btn => {
      if (btn.dataset.bound === '1') return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', async () => {
        const sid = btn.getAttribute('data-signal-id');
        const card = btn.closest('.scout-card');
        const entryInp = card && card.querySelector(`.scout-entry-input[data-signal-id="${sid}"]`);
        const qtyInp = card && card.querySelector(`.scout-qty-input[data-signal-id="${sid}"]`);
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

  let _scoutSignals = new Map();
  let _scoutPollMs = 10000;
  let _scoutLivePollMs = 3000;
  let _scoutLiveTimer = null;
  let _scoutExpiryTimer = null;

  function parseValidUntilMs(iso) {
    if (!iso) return NaN;
    return new Date(String(iso).replace(' ', 'T')).getTime();
  }

  function evaluateClientValidity(s, liveLtp) {
    const now = Date.now();
    const until = parseValidUntilMs(s.valid_until);
    if (!isNaN(until) && now > until) return 'EXPIRED';
    const action = (s.action || '').toUpperCase();
    const ltp = liveLtp != null ? Number(liveLtp) : Number(s.live_ltp);
    const inv = s.invalidation != null ? Number(s.invalidation) : null;
    if (inv != null && !isNaN(inv) && ltp > 0) {
      if (action === 'BUY' && ltp < inv) return 'INVALIDATED';
      if (action === 'SELL' && ltp > inv) return 'INVALIDATED';
    }
    const min = Number(s.entry_min);
    const max = Number(s.entry_max);
    if (ltp > 0 && !isNaN(min) && !isNaN(max) && (ltp < min || ltp > max)) return 'OUT_OF_RANGE';
    return 'ACTIVE';
  }

  function updateGatePill(card, id, ok) {
    const el = card.querySelector(`.scout-gate[data-gate-id="${id}"]`);
    if (!el) return;
    el.classList.remove('scout-gate--ok', 'scout-gate--bad', 'scout-gate--na');
    if (ok == null) {
      el.classList.add('scout-gate--na');
      el.textContent = '· ' + id.charAt(0).toUpperCase() + id.slice(1);
    } else {
      el.classList.add(ok ? 'scout-gate--ok' : 'scout-gate--bad');
      const label = id.charAt(0).toUpperCase() + id.slice(1);
      el.textContent = (ok ? '✓ ' : '✗ ') + label;
    }
  }

  function updateSignalCardLive(card, s, liveLtp, liveAsOf) {
    s.live_ltp = liveLtp;
    s.live_as_of = liveAsOf;
    if (s.dashboard && s.dashboard.prices) s.dashboard.prices.live = liveLtp;

    const valEl = card.querySelector('.scout-live-val');
    const liveMetric = valEl && valEl.closest('.scout-metric');
    if (valEl && liveLtp != null && liveLtp > 0) {
      valEl.textContent = fmtPx(liveLtp);
      const inBand = liveLtp >= Number(s.entry_min) && liveLtp <= Number(s.entry_max);
      if (liveMetric) {
        liveMetric.classList.remove('scout-metric--muted', 'scout-metric--live-ok', 'scout-metric--live-warn');
        liveMetric.classList.add(inBand ? 'scout-metric--live-ok' : 'scout-metric--live-warn');
      }
    }

    const untilOk = Date.now() <= parseValidUntilMs(s.valid_until);
    const action = (s.action || '').toUpperCase();
    const inv = s.invalidation != null ? Number(s.invalidation) : null;
    let stopOk = null;
    if (inv != null && liveLtp > 0) {
      stopOk = action === 'BUY' ? liveLtp >= inv : liveLtp <= inv;
    }
    const bandOk = liveLtp > 0 ? (liveLtp >= Number(s.entry_min) && liveLtp <= Number(s.entry_max)) : null;

    updateGatePill(card, 'time', untilOk);
    updateGatePill(card, 'band', bandOk);
    updateGatePill(card, 'stop', stopOk);

    if (s.dashboard) {
      s.dashboard.gates = { band_ok: bandOk, time_ok: untilOk, stop_ok: stopOk };
      if (inv != null && liveLtp > 0) {
        const dist = action === 'BUY' ? liveLtp - inv : inv - liveLtp;
        s.dashboard.stop_dist = {
          rs: Math.round(dist * 100) / 100,
          pct: Math.round(dist / liveLtp * 10000) / 100,
        };
      }
    }
  }

  function tickScoutTimers() {
    const c = $('#scout-signals-container');
    if (!c) return;
    c.querySelectorAll('.scout-timer[data-timer-secs]').forEach(el => {
      let secs = parseInt(el.getAttribute('data-timer-secs'), 10);
      if (isNaN(secs)) return;
      if (secs > 0) {
        secs -= 1;
        el.setAttribute('data-timer-secs', String(secs));
      }
      el.textContent = fmtTimer(secs);
      const card = el.closest('.scout-card');
      if (card && secs <= 0) {
        const id = card.dataset.signalId;
        const s = _scoutSignals.get(String(id));
        if (s && !s.trade_open) dropInvalidSignal(id, card, 'EXPIRED');
      } else if (card && secs > 0 && secs % 5 === 0) {
        updateGatePill(card, 'time', true);
      }
    });
  }

  function removeSignalCard(card, _reason) {
    if (!card || card.dataset.removing === '1') return;
    card.dataset.removing = '1';
    card.classList.add('scout-card--exit');
    setTimeout(() => {
      card.remove();
      const c = $('#scout-signals-container');
      if (c && !c.querySelector('.scout-card')) showEmptySignals();
    }, 280);
  }

  function dropInvalidSignal(id, card, reason) {
    _scoutSignals.delete(String(id));
    removeSignalCard(card, reason);
  }

  function checkScoutSignalExpiry() {
    const c = $('#scout-signals-container');
    if (!c || !_scoutSignals.size) return;
    _scoutSignals.forEach((s, id) => {
      if (s.trade_open) return;
      const card = c.querySelector(`.scout-card[data-signal-id="${id}"]`);
      if (!card) return;
      const status = evaluateClientValidity(s, s.live_ltp);
      if (status !== 'ACTIVE') dropInvalidSignal(id, card, status);
      else updateCheckState(card.querySelector('[data-check-id="window"] [data-check-state]'), true);
    });
  }

  async function pollScoutLiveQuotes() {
    const c = $('#scout-signals-container');
    if (!c || !_scoutSignals.size) return;
    const symbols = [...new Set([..._scoutSignals.values()].map(s => s.symbol))].join(',');
    try {
      const data = await scoutApi('/live-quotes?symbols=' + encodeURIComponent(symbols));
      if (data.live_poll_seconds) _scoutLivePollMs = Math.max(2, Number(data.live_poll_seconds) * 1000);
      const quotes = data.quotes || {};
      _scoutSignals.forEach((s, id) => {
        if (s.trade_open) return;
        const card = c.querySelector(`.scout-card[data-signal-id="${id}"]`);
        if (!card) return;
        const sym = String(s.symbol).toUpperCase();
        const q = quotes[sym];
        const liveLtp = q && q.ltp != null ? Number(q.ltp) : null;
        if (liveLtp == null) return;
        updateSignalCardLive(card, s, liveLtp, q.as_of);
        const status = evaluateClientValidity(s, liveLtp);
        if (status !== 'ACTIVE') dropInvalidSignal(id, card, status);
      });
    } catch (_) { /* keep last price */ }
  }

  function syncSignalCards(signals) {
    const c = $('#scout-signals-container');
    if (!c) return;
    if (!signals || !signals.length) {
      _scoutSignals.clear();
      showEmptySignals();
      return;
    }
    c.className = 'scout-signal-list';
    const newIds = new Set(signals.map(s => String(s.id)));
    c.querySelectorAll('.scout-card[data-signal-id]').forEach(card => {
      if (!newIds.has(card.dataset.signalId)) removeSignalCard(card);
    });
    _scoutSignals.clear();
    signals.forEach(s => {
      _scoutSignals.set(String(s.id), s);
      let card = c.querySelector(`.scout-card[data-signal-id="${s.id}"]`);
      if (!card) {
        const wrap = document.createElement('div');
        wrap.innerHTML = renderSingleSignalCard(s);
        card = wrap.firstElementChild;
        c.appendChild(card);
        bindSignalMarkButtons(card);
      } else {
        updateSignalCardLive(card, s, s.live_ltp, s.live_as_of);
      }
    });
    bindSignalMarkButtons(c);
  }

  function renderSignals(signals) {
    syncSignalCards(signals);
  }

  async function loadScoutSignals() {
    const c = $('#scout-signals-container');
    try {
      const [st, sig] = await Promise.all([
        scoutApi('/status'),
        scoutApi('/signals?limit=40'),
      ]);
      if (sig.poll_seconds) _scoutPollMs = Math.max(5, Number(sig.poll_seconds) * 1000);
      if (sig.live_poll_seconds) _scoutLivePollMs = Math.max(2, Number(sig.live_poll_seconds) * 1000);
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
  let _wlNiftyBank = [];
  let _wlOffset = 0;
  let _wlLimit = 80;
  let _wlTotal = 0;
  let _wlSearch = '';
  let _wlSearchTimer = null;
  let _wlLoading = false;
  let _wlRefreshedAt = '';
  let _wlZerodhaOk = true;
  let _wlNotice = '';
  let _wlIndexGroups = {
    nifty50: { label: 'Nifty 50', badge: '50' },
    nifty_bank: { label: 'Nifty Bank', badge: 'BN' },
  };

  const WL_SECTIONS = [
    { tag: 'nifty50', fallbackTitle: 'Nifty 50' },
    { tag: 'nifty_bank', fallbackTitle: 'Nifty Bank' },
    { tag: '_other', fallbackTitle: 'Other NSE' },
  ];
  let _wlSectionOpen = { nifty50: true, nifty_bank: true, _other: false };

  function indexTagsForSymbol(sym) {
    const tags = [];
    if (_wlNifty50.includes(sym)) tags.push('nifty50');
    if (_wlNiftyBank.includes(sym)) tags.push('nifty_bank');
    return tags;
  }

  function fullSectionSymbolList(tag, visibleList) {
    if (tag === 'nifty50') return _wlNifty50.slice();
    if (tag === 'nifty_bank') return _wlNiftyBank.slice();
    return visibleList.map(s => s.symbol);
  }

  function sectionAllSelected(symbols) {
    return symbols.length > 0 && symbols.every(sym => _wlSelected.has(sym));
  }

  function sectionToggleLabel(symbols) {
    return sectionAllSelected(symbols) ? 'Clear section' : 'Select all';
  }

  function toggleSectionSelection(tag, symbols) {
    if (!symbols.length) return;
    if (sectionAllSelected(symbols)) {
      symbols.forEach(sym => _wlSelected.delete(sym));
      toast(watchlistSectionTitle(tag) + ' cleared', 'info');
    } else {
      symbols.forEach(sym => _wlSelected.add(sym));
      ensureWatchlistRows(symbols);
      toast(watchlistSectionTitle(tag) + ' selected (' + symbols.length + ')', 'info');
    }
    renderWatchlist();
  }

  function selectIndexList(symbols, label) {
    if (!symbols.length) {
      toast(label + ' list not loaded yet', 'err');
      return;
    }
    symbols.forEach(sym => _wlSelected.add(sym));
    ensureWatchlistRows(symbols);
    renderWatchlist();
    toast(label + ' selected (' + symbols.length + ')', 'info');
  }

  function watchlistSectionTitle(tag) {
    const g = _wlIndexGroups[tag];
    return (g && g.label) || WL_SECTIONS.find(s => s.tag === tag)?.fallbackTitle || tag;
  }

  function renderWatchlistBadges(tags) {
    if (!tags || !tags.length) return '';
    return tags.map(t => {
      const g = _wlIndexGroups[t] || {};
      const badge = g.badge || t.slice(0, 2).toUpperCase();
      const title = g.label || t;
      return `<span class="scout-wl-badge scout-wl-badge--${escapeHtml(t)}" title="${escapeHtml(title)}">${escapeHtml(badge)}</span>`;
    }).join('');
  }

  function renderWatchlistItem(s) {
    const tags = s.index_tags || [];
    const checked = _wlSelected.has(s.symbol);
    return `
      <label class="scout-wl-item${checked ? ' scout-wl-item--checked' : ''}">
        <input type="checkbox" class="scout-wl-cb" data-symbol="${escapeHtml(s.symbol)}"${checked ? ' checked' : ''}>
        <span class="scout-wl-item-body">
          <span class="scout-wl-symbol">${escapeHtml(s.symbol)}</span>
          ${s.name ? `<span class="scout-wl-name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>` : ''}
          ${tags.length ? `<span class="scout-wl-badges">${renderWatchlistBadges(tags)}</span>` : ''}
        </span>
      </label>`;
  }

  function bucketWatchlistStocks(stocks) {
    const buckets = { nifty50: [], nifty_bank: [], _other: [] };
    stocks.forEach(s => {
      const tags = s.index_tags || [];
      if (tags.includes('nifty50')) buckets.nifty50.push(s);
      else if (tags.includes('nifty_bank')) buckets.nifty_bank.push(s);
      else buckets._other.push(s);
    });
    return buckets;
  }

  function bindWatchlistSections(root) {
    root.querySelectorAll('.scout-wl-sec-toggle').forEach(btn => {
      btn.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const tag = btn.dataset.section;
        let syms = [];
        const raw = btn.getAttribute('data-symbols') || '';
        if (raw) syms = raw.split(',').filter(Boolean);
        toggleSectionSelection(tag, syms);
      });
    });
    root.querySelectorAll('.scout-wl-section').forEach(det => {
      const tag = det.dataset.section;
      if (tag && tag in _wlSectionOpen) det.open = _wlSectionOpen[tag];
      det.addEventListener('toggle', () => {
        if (tag) _wlSectionOpen[tag] = det.open;
      });
    });
  }

  function bindWatchlistCheckboxes(root) {
    root.querySelectorAll('.scout-wl-cb').forEach(cb => {
      cb.addEventListener('change', () => {
        const sym = cb.getAttribute('data-symbol');
        if (cb.checked) _wlSelected.add(sym);
        else _wlSelected.delete(sym);
        cb.closest('.scout-wl-item')?.classList.toggle('scout-wl-item--checked', cb.checked);
        renderWatchlistMeta({
          total_equity_count: _wlTotal,
          search: _wlSearch,
          instrument_refreshed_at: _wlRefreshedAt,
        });
      });
    });
  }
  function renderWatchlistNotice() {
    const el = $('#scout-wl-notice');
    if (!el) return;
    if (_wlZerodhaOk || !_wlNotice) {
      el.hidden = true;
      el.textContent = '';
      return;
    }
    el.hidden = false;
    el.textContent = _wlNotice + ' You can still select Nifty 50 below; use 🔑 Login for the full NSE list and search.';
  }

  function ensureWatchlistRows(symbols) {
    const seen = new Set(_wlStocks.map(s => s.symbol));
    symbols.forEach(sym => {
      if (!sym || seen.has(sym)) return;
      seen.add(sym);
      const tags = indexTagsForSymbol(sym);
      _wlStocks.push({
        symbol: sym,
        name: '',
        is_nifty50: tags.includes('nifty50'),
        index_tags: tags,
      });
    });
  }

  function renderWatchlistMeta(data) {
    const meta = $('#scout-wl-meta');
    const cnt = $('#scout-wl-count');
    renderWatchlistNotice();
    if (meta) {
      const parts = [];
      if (data && data.total_equity_count) {
        parts.push(data.total_equity_count.toLocaleString() + ' NSE stocks');
      } else if (!_wlZerodhaOk && _wlNifty50.length) {
        parts.push('Showing Nifty 50 (' + _wlNifty50.length + ') — log in for full list');
      }
      if (data && data.instrument_refreshed_at) {
        parts.push('master updated ' + data.instrument_refreshed_at);
      }
      if (data && data.search) parts.push('search: “' + data.search + '”');
      meta.textContent = parts.join(' · ');
    }
    if (cnt) cnt.textContent = _wlSelected.size + ' selected';
    const more = $('#scout-wl-more');
    if (more) {
      const hasMore = _wlOffset + _wlStocks.length < _wlTotal;
      more.hidden = !hasMore;
    }
  }

  function renderWatchlist() {
    const c = $('#scout-watchlist-container');
    if (!c) return;
    if (!_wlStocks.length) {
      c.className = '';
      let msg = 'No stocks to show.';
      if (_wlSearch) msg = 'No stocks match your search.';
      else if (!_wlZerodhaOk) msg = 'Log in with Zerodha (🔑) to load the full NSE list, or use Select Nifty 50.';
      c.innerHTML = '<div class="empty">' + escapeHtml(msg) + '</div>';
      renderWatchlistMeta(null);
      return;
    }

    let html = '';
    if (_wlSearch) {
      c.className = 'scout-wl-grid';
      html = _wlStocks.map(renderWatchlistItem).join('');
    } else {
      c.className = 'scout-watchlist-wrap';
      const buckets = bucketWatchlistStocks(_wlStocks);
      html = WL_SECTIONS.map(sec => {
        const list = sec.tag === '_other' ? buckets._other : buckets[sec.tag];
        if (!list.length) return '';
        const allSyms = fullSectionSymbolList(sec.tag, list);
        const open = _wlSectionOpen[sec.tag] !== false;
        const toggleLabel = sectionToggleLabel(allSyms);
        const selectedInSection = allSyms.filter(s => _wlSelected.has(s)).length;
        return `
          <details class="scout-wl-section" data-section="${escapeHtml(sec.tag)}"${open ? ' open' : ''}>
            <summary class="scout-wl-section-summary">
              <span class="scout-wl-section-heading">
                <span class="scout-wl-section-title">${escapeHtml(watchlistSectionTitle(sec.tag))}</span>
                <span class="muted scout-wl-section-count">${list.length} shown · ${selectedInSection}/${allSyms.length} selected</span>
              </span>
              <span class="scout-wl-section-actions">
                <button type="button" class="btn btn-sm btn-ghost scout-wl-sec-toggle"
                  data-section="${escapeHtml(sec.tag)}"
                  data-symbols="${escapeHtml(allSyms.join(','))}">${escapeHtml(toggleLabel)}</button>
              </span>
            </summary>
            <div class="scout-wl-grid">${list.map(renderWatchlistItem).join('')}</div>
          </details>`;
      }).join('');
    }
    c.innerHTML = html;
    renderWatchlistMeta({
      total_equity_count: _wlTotal,
      search: _wlSearch,
      instrument_refreshed_at: _wlRefreshedAt,
    });
    bindWatchlistCheckboxes(c);
    bindWatchlistSections(c);
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
      if (data.index_groups) _wlIndexGroups = data.index_groups;
      _wlNifty50 = data.nifty50 || [];
      _wlNiftyBank = data.nifty_bank || [];
      _wlTotal = data.total_equity_count || 0;
      _wlRefreshedAt = data.instrument_refreshed_at || '';
      _wlZerodhaOk = data.zerodha_ok !== false;
      _wlNotice = data.notice || '';
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
      selectIndexList(_wlNifty50, 'Nifty 50');
    });
    $('#scout-wl-select-nbank')?.addEventListener('click', () => {
      selectIndexList(_wlNiftyBank, 'Nifty Bank');
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
  function renderTradeMtm(t) {
    if (t.mtm == null) {
      return '<div class="scout-mtm muted">Live P&L: waiting for WS ticks…</div>';
    }
    const pnl = Number(t.mtm);
    const pct = t.mtm_pct != null ? Number(t.mtm_pct) : 0;
    const cls = pnl >= 0 ? 'scout-mtm--profit' : 'scout-mtm--loss';
    const sign = pnl >= 0 ? '+' : '';
    const live = t.live_ltp != null ? ` · LTP ${fmtPx(t.live_ltp)}` : '';
    return `<div class="scout-mtm ${cls}">Live P&L: <strong>${sign}${pnl.toFixed(2)}</strong> (${sign}${pct.toFixed(2)}%)${live}</div>`;
  }

  function tradePositionMeta(action) {
    const a = (action || '').toUpperCase();
    if (a === 'SELL') {
      return {
        side: 'SHORT',
        sideCls: 'err',
        entryVerb: 'Sold (short)',
        exitAction: 'BUY',
        exitLabel: 'Buy-back fill (Zerodha)',
        exitHint: 'You sold to open this short — enter the price where you bought back to close.',
      };
    }
    return {
      side: 'LONG',
      sideCls: 'ok',
      entryVerb: 'Bought (long)',
      exitAction: 'SELL',
      exitLabel: 'Sell fill (Zerodha)',
      exitHint: 'You bought to open this long — enter the price where you sold to close.',
    };
  }

  function renderTradeExitPlan(t) {
    const plan = t.exit_plan;
    if (!plan || !plan.dashboard) return '';
    const d = plan.dashboard;
    const p = d.prices || {};
    const targetDist = d.target_dist;
    const stopDist = d.stop_dist;
    const struct = plan.structural_target;
    const structHtml = struct != null && struct !== p.target
      ? renderMetricTile('STRUCT', fmtPx(struct), 'scout-metric--wide', 'STRUCT')
      : '';
    const targetDistHtml = targetDist
      ? renderMetricTile('→ Target', `${fmtPx(targetDist.rs)} · ${fmtPct(targetDist.pct)}`, '', '→ Target')
      : '';
    const stopDistHtml = stopDist
      ? renderMetricTile('→ Stop', `${fmtPx(stopDist.rs)} · ${fmtPct(stopDist.pct)}`, '', '→ Stop')
      : '';
    const rLabel = d.target_r != null ? ` (${d.target_r}R)` : '';
    return `
      <div class="scout-dash scout-dash--exit">
        <div class="muted scout-exit-heading">Exit plan</div>
        <div class="scout-dash-prices">
          ${renderMetricTile('ENTRY', fmtPx(p.entry), '', 'ENTRY')}
          ${p.target != null ? renderMetricTile('TARGET', fmtPx(p.target) + escapeHtml(rLabel), 'scout-metric--pos', 'TARGET') : ''}
          ${p.stop != null ? renderMetricTile('STOP', fmtPx(p.stop), 'scout-metric--neg', 'STOP') : ''}
          ${renderMetricTile('EXIT BY', `<span class="scout-timer" data-timer-secs="${d.timer_secs || 0}">${fmtTimer(d.timer_secs)}</span> → ${escapeHtml(d.timer_until || '')}`, 'scout-metric--timer', 'EXIT BY')}
        </div>
        <div class="scout-dash-stats">
          ${structHtml}
          ${targetDistHtml}
          ${stopDistHtml}
        </div>
      </div>`;
  }

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
      const pos = tradePositionMeta(action);
      const sig = t.signal || {};
      return `
        <div class="scout-card scout-trade-card">
          <div class="scout-card-head">
            <strong>${escapeHtml(t.symbol)}</strong>
            <span class="tag tag-${pos.sideCls}" title="Open position side">${escapeHtml(pos.side)}</span>
            <span class="scout-trade-exit-action">Close with <strong class="tag tag-${pos.sideCls === 'err' ? 'ok' : 'err'}">${escapeHtml(pos.exitAction)}</strong></span>
            <span class="muted">${escapeHtml(t.signal_type || '')}</span>
          </div>
          <div class="scout-card-body">
            <div>${escapeHtml(pos.entryVerb)} @ ${fmtPx(t.entry_price)} × ${t.quantity || 1} · ${escapeHtml(ageLabel(t.executed_at))}</div>
            ${renderTradeExitPlan(t)}
            ${renderTradeMtm(t)}
            ${sig.reason ? `<div class="muted scout-reason">${escapeHtml(sig.reason)}</div>` : ''}
            <div class="scout-close-row">
              <label class="muted" title="${escapeHtml(pos.exitHint)}">${escapeHtml(pos.exitLabel)}</label>
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
      if (data.poll_seconds) _scoutPollMs = Math.max(5, Number(data.poll_seconds) * 1000);
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
      const pos = tradePositionMeta(t.action);
      return `
        <div class="scout-card">
          <div class="scout-card-head">
            <strong>${escapeHtml(t.symbol)}</strong>
            <span class="tag tag-${pos.sideCls}">${escapeHtml(pos.side)}</span>
            <span class="muted">${escapeHtml(t.signal_type || '')}</span>
          </div>
          <div class="scout-card-body">
            <div>${escapeHtml(pos.entryVerb.split(' ')[0])} @ ${fmtPx(t.entry_price)} → ${escapeHtml(pos.exitAction)} @ ${fmtPx(t.exit_price)} · ${escapeHtml(String(closed).slice(0, 16))}</div>
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
  let _scoutTradesTimer = null;

  function stopScoutLiveRefresh() {
    if (_scoutLiveTimer) {
      clearInterval(_scoutLiveTimer);
      _scoutLiveTimer = null;
    }
    if (_scoutExpiryTimer) {
      clearInterval(_scoutExpiryTimer);
      _scoutExpiryTimer = null;
    }
  }

  function startScoutLiveRefresh() {
    stopScoutLiveRefresh();
    pollScoutLiveQuotes();
    _scoutLiveTimer = setInterval(pollScoutLiveQuotes, _scoutLivePollMs);
    _scoutExpiryTimer = setInterval(() => {
      checkScoutSignalExpiry();
      tickScoutTimers();
    }, 1000);
  }

  function stopScoutAutoRefresh() {
    if (_scoutTimer) {
      clearInterval(_scoutTimer);
      _scoutTimer = null;
    }
  }

  function stopScoutTradesAutoRefresh() {
    if (_scoutTradesTimer) {
      clearInterval(_scoutTradesTimer);
      _scoutTradesTimer = null;
    }
  }

  function startScoutAutoRefresh() {
    stopScoutAutoRefresh();
    _scoutTimer = setInterval(loadScoutSignals, _scoutPollMs);
  }

  function startScoutTradesAutoRefresh() {
    stopScoutTradesAutoRefresh();
    _scoutTradesTimer = setInterval(loadScoutTrades, _scoutPollMs);
  }

  function onScoutTabEnter(tab) {
    updateScoutSubtabs(tab);
    if (tab === 'scout-signals') {
      loadScoutSignals().then(() => {
        startScoutAutoRefresh();
        startScoutLiveRefresh();
      });
      stopScoutTradesAutoRefresh();
    } else {
      stopScoutAutoRefresh();
      stopScoutLiveRefresh();
      if (tab === 'scout-watchlist') {
        _wlSearch = ($('#scout-wl-search')?.value || '').trim();
        loadScoutWatchlist({ reset: true });
      }
      if (tab === 'scout-trades') {
        loadScoutTrades().then(() => startScoutTradesAutoRefresh());
      } else {
        stopScoutTradesAutoRefresh();
      }
      if (tab === 'scout-history') loadScoutHistory();
    }
  }

  function onScoutTabLeave() {
    stopScoutAutoRefresh();
    stopScoutTradesAutoRefresh();
    stopScoutLiveRefresh();
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
