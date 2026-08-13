/**
 * Intraday Scout — signals, watchlist, trades (Zerodha fills), history.
 */
(function () {
  'use strict';

  const SCOUT_TABS = ['scout-signals', 'scout-trades', 'scout-history', 'scout-errors', 'scout-watchlist', 'scout-config'];
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
  function renderAlarmBanner(health, targetIds) {
    const ids = targetIds || ['scout-alarm-banner', 'scout-trades-alarm-banner'];
    const alarms = (health && health.alarms) || [];
    const critical = alarms.filter(a => a.level === 'critical');
    const warnings = alarms.filter(a => a.level === 'warning');
    const show = critical.length ? critical : warnings;
    const cls = critical.length ? 'scout-alarm-banner--critical' : 'scout-alarm-banner--warning';
    const html = show.length
      ? `<strong>${critical.length ? 'Critical' : 'Warning'}:</strong> `
        + show.map(a => escapeHtml(a.message)).join(' · ')
      : '';
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      if (!html) {
        el.hidden = true;
        el.innerHTML = '';
        return;
      }
      el.hidden = false;
      el.className = 'scout-alarm-banner ' + cls;
      el.innerHTML = html;
    });
  }

  function renderStatus(st) {
    const bar = $('#scout-status-bar');
    if (!bar || !st) return;
    renderAlarmBanner(st.health);
    const parts = [];
    parts.push(st.market_open ? '🟢 Market open' : '⚫ Market closed');
    parts.push(st.zerodha_ok ? '🔑 Zerodha OK' : '🔑 ' + (st.zerodha_message || 'Not logged in'));
    const execMode = st.zerodha_execute_orders
      ? '💰 Zerodha LIVE orders'
      : '📝 Paper (DB only)';
    parts.push(execMode);
    if (st.wallet && st.zerodha_execute_orders && st.wallet.balance_inr != null) {
      const w = st.wallet;
      parts.push(
        `💳 Bal ₹${fmtNum(w.balance_inr, 0)} · `
        + `deployed ₹${fmtNum(w.deployed_inr, 0)} · `
        + `free ₹${fmtNum(w.free_inr, 0)}`
      );
    } else if (st.wallet && st.wallet.error && st.wallet.error !== 'paper_mode') {
      parts.push('💳 Wallet unavailable');
    }
    if (st.square_off_time) parts.push('⏱ Exit by ' + st.square_off_time);
    parts.push('📡 WebSocket push');
    if (st.last_signal && st.last_signal.triggered_at) {
      parts.push(
        'Last signal: ' + st.last_signal.symbol + ' ' + st.last_signal.action
        + ' (' + ageLabel(st.last_signal.triggered_at) + ')'
      );
    }
    parts.push(st.watchlist_count + ' symbols watched');
    if (st.automation) {
      const auto = [];
      if (st.automation.auto_execute_signals) auto.push('auto-enter');
      if (st.automation.auto_close_trades) auto.push('auto-close');
      parts.push(auto.length ? '⚡ ' + auto.join(' + ') : '✋ manual mode');
    }
    if (st.settings) {
      const s = st.settings;
      if (s.max_trades_per_day > 0) {
        const n = st.trades_opened_today != null ? st.trades_opened_today : '—';
        parts.push(`📊 ${n}/${s.max_trades_per_day} trades today`);
      }
      if (s.use_investment_sizing) {
        parts.push('₹' + Number(s.investment_per_trade_inr || 0).toLocaleString('en-IN') + '/trade');
      }
      if (s.trade_window_start && s.trade_window_end) {
        parts.push(`🕐 ${s.trade_window_start}–${s.trade_window_end} IST`);
      }
    }
    bar.innerHTML = parts.map(p => `<span class="scout-stat">${escapeHtml(p)}</span>`).join('');
    syncAutomationUI(st.automation);
  }

  let _automationSaving = false;

  function syncAutomationUI(automation) {
    if (!automation || _automationSaving) return;
    const exec = $('#scout-auto-execute');
    const close = $('#scout-auto-close');
    if (exec) exec.checked = !!automation.auto_execute_signals;
    if (close) close.checked = !!automation.auto_close_trades;
  }

  async function saveAutomation(patch) {
    if (_automationSaving) return;
    _automationSaving = true;
    const exec = $('#scout-auto-execute');
    const close = $('#scout-auto-close');
    const body = {
      auto_execute_signals: patch.auto_execute_signals ?? !!(exec && exec.checked),
      auto_close_trades: patch.auto_close_trades ?? !!(close && close.checked),
    };
    try {
      const data = await scoutApi('/automation', {
        method: 'PUT',
        body: JSON.stringify(body),
      });
      syncAutomationUI(data.automation || body);
      const labels = [];
      if (body.auto_execute_signals) labels.push('auto-enter');
      if (body.auto_close_trades) labels.push('auto-close');
      toast(
        labels.length
          ? 'Automation on: ' + labels.join(', ')
          : 'Manual mode — mark taken and close trades yourself',
        'info'
      );
      const st = await scoutApi('/status');
      renderStatus(st);
    } catch (e) {
      toast('Automation save failed: ' + e.message, 'error');
    } finally {
      _automationSaving = false;
    }
  }

  function bindAutomationControls() {
    $('#scout-auto-execute')?.addEventListener('change', (ev) => {
      saveAutomation({ auto_execute_signals: ev.target.checked });
    });
    $('#scout-auto-close')?.addEventListener('change', (ev) => {
      saveAutomation({ auto_close_trades: ev.target.checked });
    });
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
    'EXIT BY': 'Intraday square-off — close the position before this time (default 15:10 IST).',
    ENTRY: 'Your Zerodha fill price when you marked the trade taken.',
    '→ Target': 'Distance from live price to the profit target.',
    STRUCT: 'Measured-move target from the pattern (OR/box height projected from breakout).',
    'SIG #': 'Signal ID — match this with the same SIG # on My Trades.',
    'TRD #': 'Trade ID — your open/closed trade record linked to the signal.',
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
    automation: 'Auto-enter is enabled in Scout Config.',
    scout_on: 'Scout module is enabled on the server.',
    market: 'NSE regular session is open.',
    validity: 'Signal status is ACTIVE (not expired, out of range, or invalidated).',
    trade_window: 'Current time is inside the Config trading window (not the same as signal validity).',
    strength: 'Signal strength is allowed for auto-enter.',
    pattern: 'Signal pattern type is allowed for auto-enter.',
    profit: 'Expected net profit at 2R target meets the minimum after Zerodha charges.',
    daily_cap: 'Daily auto-trade limit not reached.',
    symbol_day: 'This symbol has not been traded yet today (when one-per-symbol is on).',
    symbol_open: 'No other open trade on this symbol.',
    not_taken: 'This signal has not been marked taken yet.',
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

  function renderAutoEnterPanel(ae) {
    if (!ae) return '';
    const checks = ae.checks || [];
    if (!checks.length) return renderStatusPills(null);

    const readyCls = ae.ready ? 'scout-auto-enter--ready' : (ae.enabled ? 'scout-auto-enter--pending' : 'scout-auto-enter--off');
    const headline = ae.ready
      ? 'Auto-enter ready'
      : (ae.enabled ? 'Auto-enter blocked' : 'Auto-enter off');
    const block = ae.block_reason && !ae.ready
      ? `<p class="scout-auto-enter-block muted">${escapeHtml(ae.block_reason)}</p>`
      : '';

    const rows = checks.map(c => {
      const cls = c.ok ? 'scout-auto-check--ok' : 'scout-auto-check--bad';
      const sym = c.ok ? '✓' : '✗';
      const hint = scoutHint(c.id);
      const titleAttr = hint ? ` title="${escapeHtml(hint)}"` : '';
      const detail = c.detail ? `<span class="scout-auto-check-detail">${escapeHtml(c.detail)}</span>` : '';
      return `<li class="scout-auto-check ${cls}" data-auto-check-id="${escapeHtml(c.id)}"${titleAttr} tabindex="0">
        <span class="scout-auto-check-mark">${sym}</span>
        <span class="scout-auto-check-label">${escapeHtml(c.label)}</span>
        ${detail}
      </li>`;
    }).join('');

    return `<div class="scout-auto-enter ${readyCls}">
      <div class="scout-auto-enter-head">
        <strong>${escapeHtml(headline)}</strong>
        ${ae.quantity ? `<span class="muted scout-auto-enter-qty">qty ${ae.quantity}</span>` : ''}
      </div>
      ${block}
      <ul class="scout-auto-checks">${rows}</ul>
    </div>`;
  }

  function syncAutoEnterCheck(card, checkId, ok, detail) {
    if (ok == null) return;
    const li = card.querySelector(`[data-auto-check-id="${checkId}"]`);
    if (!li) return;
    li.classList.remove('scout-auto-check--ok', 'scout-auto-check--bad');
    li.classList.add(ok ? 'scout-auto-check--ok' : 'scout-auto-check--bad');
    const mark = li.querySelector('.scout-auto-check-mark');
    if (mark) mark.textContent = ok ? '✓' : '✗';
    if (detail != null) {
      let det = li.querySelector('.scout-auto-check-detail');
      if (!det) {
        det = document.createElement('span');
        det.className = 'scout-auto-check-detail';
        li.appendChild(det);
      }
      det.textContent = detail;
    }
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
        ${renderAutoEnterPanel(s.auto_enter)}
      </div>`;
  }

  function isSignalTradeOpen(s) {
    return !!(s && (s.trade_open === true || s.trade_open === 1));
  }

  function canMarkSignalTaken(s) {
    if (!s) return false;
    if (isSignalTradeOpen(s)) return false;
    if (s.can_mark_taken === false || s.symbol_trade_blocked === true) return false;
    return true;
  }

  function renderMarkTakenBlock(s) {
    const entryDefault = s.live_ltp != null ? Number(s.live_ltp) : Number(s.ltp || 0);
    const qtyDefault = Math.max(1, parseInt(s.suggested_quantity, 10) || 1);
    if (isSignalTradeOpen(s)) {
      return `<span class="muted scout-trade-badge">Trade open${s.trade_id ? ` · TRD #${s.trade_id}` : ''}</span>`;
    }
    if (!canMarkSignalTaken(s)) {
      const parts = ['Open trade exists'];
      if (s.blocking_signal_id != null) parts.push(`SIG #${s.blocking_signal_id}`);
      if (s.blocking_trade_id != null) parts.push(`TRD #${s.blocking_trade_id}`);
      const detail = parts.length > 1 ? ` · ${parts.slice(1).join(' · ')}` : '';
      return `<span class="muted scout-trade-badge scout-trade-badge--blocked" title="Close or void the open ${escapeHtml(s.symbol || '')} trade before taking another signal on this symbol">${escapeHtml(parts[0])}${escapeHtml(detail)}</span>`;
    }
    return `<div class="scout-mark-row">
          <input type="number" step="0.05" class="scout-entry-input" value="${entryDefault}"
            data-signal-id="${s.id}" aria-label="Entry fill price" title="Your Zerodha fill price">
          <input type="number" step="1" min="1" class="scout-qty-input" value="${qtyDefault}"
            data-signal-id="${s.id}" aria-label="Quantity" title="Suggested from investment per trade setting">
          <button type="button" class="btn btn-sm btn-accent scout-mark-btn" data-signal-id="${s.id}">Mark taken</button>
        </div>`;
  }

  function cloneSignal(s) {
    return JSON.parse(JSON.stringify(s));
  }

  function renderScoutRefIds(signalId, tradeId) {
    const parts = [];
    if (signalId != null && signalId !== '') {
      parts.push(`<span class="scout-ref-id" title="${escapeHtml(scoutHint('SIG #'))}">SIG #${escapeHtml(String(signalId))}</span>`);
    }
    if (tradeId != null && tradeId !== '') {
      parts.push(`<span class="scout-ref-id scout-ref-id--trade" title="${escapeHtml(scoutHint('TRD #'))}">TRD #${escapeHtml(String(tradeId))}</span>`);
    }
    return parts.length ? `<span class="scout-ref-ids">${parts.join('')}</span>` : '';
  }

  function renderExitAlertBanner(exitAlerts) {
    if (!exitAlerts || !exitAlerts.alerts || !exitAlerts.alerts.length) return '';
    const urg = exitAlerts.urgency || 'none';
    if (urg === 'none') return '';
    const detail = exitAlerts.alerts.map(a => escapeHtml(a.label || '')).join(' · ');
    const headline = exitAlerts.close_now ? 'Close now' : 'Exit approaching';
    return `<div class="scout-exit-alert scout-exit-alert--${urg}" role="alert">
      <span class="scout-exit-alert-dot" aria-hidden="true"></span>
      <strong>${headline}</strong>
      <span class="scout-exit-alert-detail">${detail}</span>
    </div>`;
  }

  function renderSingleSignalCard(s) {
    const action = (s.action || '').toUpperCase();
    const cls = action === 'BUY' ? 'scout-buy' : action === 'SELL' ? 'scout-sell' : 'scout-wait';
    const strength = (s.strength || 'WEAK').toLowerCase();
    const d = s.dashboard || {};
    const setupCode = d.setup_code || (s.signal_type || '').replace(/_/g, ' ');
    const markBlock = renderMarkTakenBlock(s);
    const setupHint = scoutHint(setupCode);
    const setupTitle = setupHint ? ` title="${escapeHtml(setupHint)}"` : '';
    return `
      <div class="scout-card ${cls}" data-signal-id="${s.id}" data-symbol="${escapeHtml(s.symbol)}"
        data-action="${escapeHtml(action)}"
        data-entry-min="${s.entry_min}" data-entry-max="${s.entry_max}"
        data-invalidation="${s.invalidation != null ? s.invalidation : ''}"
        data-valid-until="${escapeHtml(s.valid_until || '')}"
        data-trade-open="${isSignalTradeOpen(s) ? '1' : '0'}"
        data-can-mark="${canMarkSignalTaken(s) ? '1' : '0'}"
        data-trade-id="${s.trade_id != null ? s.trade_id : ''}"
        data-blocking-trade-id="${s.blocking_trade_id != null ? s.blocking_trade_id : ''}"
        data-blocking-signal-id="${s.blocking_signal_id != null ? s.blocking_signal_id : ''}"
        data-auto-ready="${s.auto_enter && s.auto_enter.ready ? '1' : '0'}">
        <div class="scout-card-head">
          <strong class="scout-symbol">${escapeHtml(s.symbol)}</strong>
          <span class="scout-action tag tag-${action === 'BUY' ? 'ok' : action === 'SELL' ? 'err' : 'muted'}" title="Suggested direction for this intraday setup">${escapeHtml(action)}</span>
          <span class="scout-setup-code"${setupTitle} tabindex="0">${escapeHtml(setupCode)}</span>
          <span class="scout-strength scout-strength--${strength}" title="Signal strength from pattern quality and relative strength">${escapeHtml(s.strength || '')}</span>
          ${renderScoutRefIds(s.id, isSignalTradeOpen(s) ? s.trade_id : null)}
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

  function signalCardNeedsRebuild(card, s) {
    const tradeOpen = isSignalTradeOpen(s) ? '1' : '0';
    if (card.dataset.tradeOpen !== tradeOpen) return true;
    const canMark = canMarkSignalTaken(s) ? '1' : '0';
    if ((card.dataset.canMark || '') !== canMark) return true;
    const tid = isSignalTradeOpen(s) && s.trade_id != null ? String(s.trade_id) : '';
    if ((card.dataset.tradeId || '') !== tid) return true;
    const blockTid = s.blocking_trade_id != null ? String(s.blocking_trade_id) : '';
    if ((card.dataset.blockingTradeId || '') !== blockTid) return true;
    const ready = s.auto_enter && s.auto_enter.ready ? '1' : '0';
    if ((card.dataset.autoReady || '') !== ready) return true;
    return false;
  }

  function replaceSignalCard(card, s) {
    const wrap = document.createElement('div');
    wrap.innerHTML = renderSingleSignalCard(s);
    const next = wrap.firstElementChild;
    card.replaceWith(next);
    bindSignalMarkButtons(next);
    return next;
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
          const resp = await scoutApi('/signals/' + sid + '/mark-taken', {
            method: 'POST',
            body: JSON.stringify({ entry_price: entry, quantity: qty }),
          });
          toast(`Trade marked — TRD #${resp.trade_id}`, 'ok');
          await loadScoutFlow();
          await loadScoutSignals();
          startScoutFlowAutoRefresh();
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
      const validityOk = evaluateClientValidity(s, liveLtp) === 'ACTIVE';
      syncAutoEnterCheck(card, 'band', bandOk, null);
      syncAutoEnterCheck(card, 'validity', validityOk, evaluateClientValidity(s, liveLtp));
      syncAutoEnterCheck(card, 'stop', stopOk, null);
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
        if (s && !isSignalTradeOpen(s)) dropInvalidSignal(id, card, 'EXPIRED');
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
      if (isSignalTradeOpen(s)) return;
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
        if (isSignalTradeOpen(s)) return;
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

  function clearContainerPlaceholder(c) {
    Array.from(c.childNodes).forEach(n => {
      if (n.nodeType === Node.TEXT_NODE) n.remove();
    });
  }

  function signalSortRank(s) {
    if (canMarkSignalTaken(s)) return 0;
    if (isSignalTradeOpen(s)) return 1;
    return 2;
  }

  function sortSignalsForDisplay(signals) {
    return [...(signals || [])].sort((a, b) => {
      const tierDiff = signalSortRank(a) - signalSortRank(b);
      if (tierDiff !== 0) return tierDiff;
      const ta = String(a.triggered_at || '');
      const tb = String(b.triggered_at || '');
      return tb.localeCompare(ta);
    });
  }

  function syncSignalCards(signals) {
    const c = $('#scout-signals-container');
    if (!c) return;
    const ordered = sortSignalsForDisplay(signals);
    if (!ordered.length) {
      _scoutSignals.clear();
      showEmptySignals();
      return;
    }
    c.className = 'scout-signal-list';
    clearContainerPlaceholder(c);
    const newIds = new Set(ordered.map(s => String(s.id)));
    c.querySelectorAll('.scout-card[data-signal-id]').forEach(card => {
      if (!newIds.has(card.dataset.signalId)) removeSignalCard(card);
    });
    _scoutSignals.clear();
    ordered.forEach(s => {
      const snap = cloneSignal(s);
      _scoutSignals.set(String(snap.id), snap);
      let card = c.querySelector(`.scout-card[data-signal-id="${snap.id}"]`);
      if (!card) {
        const wrap = document.createElement('div');
        wrap.innerHTML = renderSingleSignalCard(snap);
        card = wrap.firstElementChild;
        c.appendChild(card);
        bindSignalMarkButtons(card);
      } else if (signalCardNeedsRebuild(card, snap)) {
        replaceSignalCard(card, snap);
      } else {
        updateSignalCardLive(card, snap, snap.live_ltp, snap.live_as_of);
      }
    });
    ordered.forEach(s => {
      const card = c.querySelector(`.scout-card[data-signal-id="${s.id}"]`);
      if (card) c.appendChild(card);
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

  // ---------- Execution flow ----------
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

  function renderExecutionStep(step) {
    const stCls = 'scout-exec-step--' + (step.status || 'pending');
    const ordersHtml = (step.orders || []).map(o => {
      const px = o.trigger_price != null
        ? `trigger ₹${fmtPx(o.trigger_price)} · limit ₹${fmtPx(o.price)}`
        : (o.price != null ? `₹${fmtPx(o.price)}` : '—');
      const oid = o.kite_order_id ? ` · #${escapeHtml(String(o.kite_order_id))}` : '';
      return `<li class="scout-exec-order scout-exec-order--${escapeHtml(o.status_class || 'pending')}">
        <span class="scout-exec-order-leg">${escapeHtml(o.leg_label || o.leg || '')}</span>
        <span class="scout-exec-order-detail">${escapeHtml(o.transaction_type || '')} ${escapeHtml(o.order_type || '')}
          × ${o.quantity || '—'} @ ${px}${oid}</span>
        <span class="scout-exec-order-status">${escapeHtml(o.status || '')}</span>
      </li>`;
    }).join('');
    return `<div class="scout-exec-step ${stCls}">
      <div class="scout-exec-step-head">
        <span class="scout-exec-step-num">${step.step}</span>
        <strong>${escapeHtml(step.label || '')}</strong>
        <span class="scout-exec-step-badge">${escapeHtml(step.status || '')}</span>
      </div>
      ${ordersHtml ? `<ul class="scout-exec-orders">${ordersHtml}</ul>` : '<p class="muted scout-exec-empty">No orders yet</p>'}
    </div>`;
  }

  function renderExecutionFlowCard(item) {
    if (item.kind === 'signal' && !item.trade) {
      const s = item.signal || {};
      return `<div class="scout-card scout-exec-card scout-exec-card--signal">
        <div class="scout-card-head">
          <strong>${escapeHtml(s.symbol || '')}</strong>
          <span class="muted">Awaiting entry · Step 1</span>
          ${renderScoutRefIds(s.id, null)}
        </div>
        <p class="muted">Signal active — auto-enter or mark taken to start execution flow.</p>
      </div>`;
    }
    const ex = item.execution || {};
    const t = item.trade || {};
    const tradeStatus = String(t.status || ex.trade_status || '').toUpperCase();
    const unprot = tradeStatus === 'UNPROTECTED';
    const cardCls = unprot ? ' scout-exec-card--unprotected' : '';
    const stepsHtml = (ex.steps || []).map(renderExecutionStep).join('');
    const modeLabel = ex.zerodha_live ? 'Zerodha LIVE' : (ex.execution_mode === 'manual' ? 'Manual' : 'Paper');
    const modeCls = ex.zerodha_live ? 'scout-exec-mode--live' : 'scout-exec-mode--paper';
    const mtm = ex.mtm || {};
    const plan = ex.exit_plan || {};
    const prices = (plan.dashboard && plan.dashboard.prices) || {};
    return `<div class="scout-card scout-exec-card${cardCls}" data-trade-id="${t.id || ''}">
      <div class="scout-card-head">
        <strong>${escapeHtml(t.symbol || ex.symbol || '')}</strong>
        <span class="scout-exec-mode ${modeCls}">${escapeHtml(modeLabel)}</span>
        <span class="tag tag-muted">${escapeHtml(t.status || ex.trade_status || '')}</span>
        ${unprot ? '<span class="tag tag-danger">NO STOP ON ZERODHA</span>' : ''}
        ${renderScoutRefIds(ex.signal_id, ex.trade_id)}
      </div>
      ${renderExitAlertBanner(ex.exit_alerts)}
      <div class="scout-exec-summary">
        <span>Entry ${fmtPx(prices.entry)}</span>
        <span>Stop ${fmtPx(prices.stop)}</span>
        <span>Target ${fmtPx(prices.target)}</span>
        <span>Exit by ${escapeHtml(ex.square_off_time || '')}</span>
        ${mtm.mtm != null ? `<span class="scout-exec-mtm">MTM ${fmtPnl(mtm.mtm)}</span>` : ''}
      </div>
      <div class="scout-exec-flow">${stepsHtml}</div>
      ${t.status === 'OPEN' || unprot ? renderTradeCloseRow(t, ex.exit_alerts) : ''}
    </div>`;
  }

  function renderTradeCloseRow(t, exitAlerts) {
    const urg = (exitAlerts && exitAlerts.urgency) || 'none';
    const closeBtnCls = urg === 'now' ? ' scout-close-btn--pulse' : '';
    const exitCode = (exitAlerts && exitAlerts.close_now && exitAlerts.alerts && exitAlerts.alerts[0])
      ? String(exitAlerts.alerts[0].code || 'manual').toLowerCase()
      : 'manual';
    const pos = tradePositionMeta(String(t.action || 'BUY').toUpperCase());
    return `<div class="scout-close-row" data-exit-reason="${escapeHtml(exitCode)}">
      <label class="muted">${escapeHtml(pos.exitLabel)}</label>
      <input type="number" step="0.05" class="scout-exit-input" placeholder="Exit price" data-trade-id="${t.id}">
      <button type="button" class="btn btn-sm btn-accent scout-close-btn${closeBtnCls}" data-trade-id="${t.id}">Close trade</button>
      <button type="button" class="btn btn-sm btn-ghost scout-void-btn" data-trade-id="${t.id}">Void</button>
    </div>`;
  }

  function renderExecutionFlow(items) {
    const c = $('#scout-trades-container');
    if (!c) return;
    const trades = (items || []).filter(it => it.kind === 'trade');
    if (!trades.length && !(items || []).length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No active executions. Enable auto-enter on the <strong>Signals</strong> tab or mark a signal taken.</div>';
      return;
    }
    c.className = 'scout-exec-list';
    c.innerHTML = (items || []).map(renderExecutionFlowCard).join('');
    bindExecutionCloseButtons(c);
  }

  function bindExecutionCloseButtons(root) {
    (root || document).querySelectorAll('.scout-close-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const tid = btn.getAttribute('data-trade-id');
        const row = btn.closest('.scout-close-row');
        const inp = row && row.querySelector(`.scout-exit-input[data-trade-id="${tid}"]`);
        const px = parseFloat(inp && inp.value);
        if (!px || px <= 0) {
          toast('Enter exit fill price', 'err');
          return;
        }
        btn.disabled = true;
        try {
          const exitReason = (row && row.dataset.exitReason) || 'manual';
          await scoutApi('/trades/' + tid + '/close', {
            method: 'POST',
            body: JSON.stringify({ exit_price: px, exit_reason: exitReason }),
          });
          toast('Trade closed', 'ok');
          await loadScoutFlow();
          await loadScoutSignals();
        } catch (e) {
          toast(e.message, 'err');
          btn.disabled = false;
        }
      });
    });
    (root || document).querySelectorAll('.scout-void-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const tid = btn.getAttribute('data-trade-id');
        if (!confirm('Remove this open trade record?')) return;
        try {
          await scoutApi('/trades/' + tid, { method: 'DELETE' });
          toast('Trade voided', 'info');
          await loadScoutFlow();
          await loadScoutSignals();
        } catch (e) {
          toast(e.message, 'err');
        }
      });
    });
  }

  async function loadScoutFlow() {
    const c = $('#scout-trades-container');
    if (!c) return;
    try {
      const [data, health] = await Promise.all([
        scoutApi('/flow'),
        scoutApi('/health').catch(() => null),
      ]);
      if (health) renderAlarmBanner(health, ['scout-trades-alarm-banner', 'scout-alarm-banner']);
      if (data.poll_seconds) _scoutPollMs = Math.max(5, Number(data.poll_seconds) * 1000);
      renderExecutionFlow(data.items || []);
    } catch (e) {
      c.className = '';
      c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------- Config ----------
  let _scoutSettings = null;

  function defaultErrDates() {
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - 30);
    const fmt = d => d.toISOString().slice(0, 10);
    const f = $('#scout-err-from');
    const t = $('#scout-err-to');
    if (f && !f.value) f.value = fmt(from);
    if (t && !t.value) t.value = fmt(to);
  }

  function renderZerodhaCheckStatus(summary) {
    const el = $('#scout-err-status');
    if (!el) return;
    if (!summary) {
      el.hidden = true;
      el.innerHTML = '';
      return;
    }
    const ok = !!summary.overall_ok;
    el.hidden = false;
    el.className = 'scout-err-status ' + (ok ? 'scout-err-status--ok' : 'scout-err-status--fail');
    const checks = (summary.checks || []).map(c => {
      const icon = c.ok ? '✓' : '✗';
      const cls = c.ok ? 'scout-err-check--ok' : 'scout-err-check--fail';
      const detail = c.error || c.detail || '';
      return `<li class="scout-err-check ${cls}"><span>${icon} ${escapeHtml(c.label || c.check_id)}</span>`
        + (detail ? `<span class="muted">${escapeHtml(detail)}</span>` : '')
        + '</li>';
    }).join('');
    el.innerHTML = `
      <div class="scout-err-status-head">
        <strong>${ok ? 'All checks passed' : 'Checks failing — live orders blocked'}</strong>
        <span class="muted">${escapeHtml(summary.checked_at || '')} · ${escapeHtml(summary.trigger || '')}</span>
      </div>
      <ul class="scout-err-checklist">${checks}</ul>`;
  }

  async function loadScoutErrors() {
    defaultErrDates();
    const c = $('#scout-errors-container');
    if (!c) return;
    c.className = 'loading';
    c.textContent = 'Loading…';
    const params = new URLSearchParams();
    const from = $('#scout-err-from')?.value;
    const to = $('#scout-err-to')?.value;
    const sev = $('#scout-err-severity')?.value;
    const q = $('#scout-err-search')?.value;
    if (from) params.set('from_date', from);
    if (to) params.set('to_date', to);
    if (sev) params.set('severity', sev);
    if (q) params.set('search', q);
    try {
      const [logData, latest] = await Promise.all([
        scoutApi('/zerodha-log?' + params),
        scoutApi('/zerodha-check/latest'),
      ]);
      renderZerodhaCheckStatus(latest.summary);
      const rows = logData.entries || [];
      if (!rows.length) {
        c.className = '';
        c.innerHTML = '<div class="empty">No log entries for this filter.</div>';
        return;
      }
      c.className = '';
      c.innerHTML = `<table class="dt scout-err-table"><thead><tr>
        <th>Time</th><th>Severity</th><th>Code</th><th>Trigger</th><th>Message</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td>${escapeHtml(r.logged_at || '')}</td>
          <td><span class="tag tag-${errLevelClass(r.severity)}">${escapeHtml(r.severity || '')}</span></td>
          <td><code>${escapeHtml(r.code || '')}</code></td>
          <td>${escapeHtml(r.trigger_source || '')}</td>
          <td>${escapeHtml(r.message || '')}</td>
        </tr>`).join('')}
        </tbody></table>
        <p class="muted scout-err-count">${rows.length} shown · ${logData.total != null ? logData.total : rows.length} total in range</p>`;
    } catch (e) {
      c.className = '';
      c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    }
  }

  function errLevelClass(sev) {
    const s = String(sev || '').toUpperCase();
    if (s === 'ERROR' || s === 'CRITICAL') return 'err';
    if (s === 'WARNING') return 'warn';
    return 'info';
  }

  async function rerunZerodhaChecks() {
    const btn = $('#scout-err-recheck');
    if (btn) btn.disabled = true;
    try {
      const data = await scoutApi('/zerodha-check', { method: 'POST', body: '{}' });
      renderZerodhaCheckStatus(data.summary);
      toast(data.summary?.overall_ok ? 'All checks passed' : 'Some checks failed — see list', data.summary?.overall_ok ? 'ok' : 'err');
      await loadScoutErrors();
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ---------- Config (settings form) ----------

  function setConfigNotice(msg, ok) {
    const el = $('#scout-config-notice');
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

  function fillConfigForm(settings, tradesToday) {
    const form = $('#scout-config-form');
    if (!form || !settings) return;
    _scoutSettings = settings;
    const setCheck = (name, val) => {
      const el = form.querySelector(`[name="${name}"]`);
      if (el && el.type === 'checkbox') el.checked = !!val;
    };
    const setVal = (name, val) => {
      const el = form.querySelector(`[name="${name}"]`);
      if (el && el.type !== 'checkbox') el.value = val != null ? String(val) : '';
    };
    setCheck('auto_execute_signals', settings.auto_execute_signals);
    setCheck('auto_close_trades', settings.auto_close_trades);
    setCheck('use_investment_sizing', settings.use_investment_sizing);
    setCheck('one_trade_per_symbol_per_day', settings.one_trade_per_symbol_per_day);
    setCheck('dedupe_per_symbol', settings.dedupe_per_symbol);
    setVal('investment_per_trade_inr', settings.investment_per_trade_inr);
    setVal('auto_trade_quantity', settings.auto_trade_quantity);
    setVal('max_trades_per_day', settings.max_trades_per_day);
    setVal('trade_window_start', settings.trade_window_start);
    setVal('trade_window_end', settings.trade_window_end);
    setVal('push_dedupe_minutes', settings.push_dedupe_minutes);
    setVal('signal_valid_minutes', settings.signal_valid_minutes);
    setVal('max_move_from_open_pct', settings.max_move_from_open_pct);
    setVal('rs_margin_pct', settings.rs_margin_pct);
    setVal('compression_range_pct', settings.compression_range_pct);
    setVal('entry_slippage_pct', settings.entry_slippage_pct);
    setVal('min_candles', settings.min_candles);
    const allowed = new Set((settings.auto_enter_strengths || []).map(s => String(s).toUpperCase()));
    form.querySelectorAll('input[name="auto_enter_strengths"]').forEach(cb => {
      cb.checked = allowed.has(cb.value);
    });
    const types = new Set((settings.auto_enter_signal_types || []).map(s => String(s).toUpperCase()));
    form.querySelectorAll('input[name="auto_enter_signal_types"]').forEach(cb => {
      cb.checked = types.has(cb.value);
    });
    setVal('min_net_profit_inr', settings.min_net_profit_inr);
    setVal('min_target_r', settings.min_target_r);
    setVal('breakeven_at_r', settings.breakeven_at_r);
    setVal('trail_stop_r_fraction', settings.trail_stop_r_fraction);
    setCheck('zerodha_execute_orders', settings.zerodha_execute_orders);
    setVal('square_off_time', settings.square_off_time || '15:10');
    setVal('square_off_warn_minutes', settings.square_off_warn_minutes);
    setVal('wallet_utilization_pct', settings.wallet_utilization_pct ?? 90);
    setVal('wallet_reserve_inr', settings.wallet_reserve_inr ?? 2000);
    const execHint = $('#cfg-zerodha-exec');
    if (execHint) {
      const mode = settings.zerodha_execute_orders
        ? 'Live mode — real Kite MIS orders will be placed when auto-enter runs.'
        : 'Paper mode — database updates only, no Kite order calls.';
      const sq = settings.square_off_time || '15:10';
      execHint.textContent = `${mode} Square-off: ${sq} IST.`;
    }
    const hint = $('#cfg-trades-today');
    if (hint) {
      const max = settings.max_trades_per_day || 0;
      hint.textContent = max > 0
        ? `Trades opened today: ${tradesToday != null ? tradesToday : '—'} / ${max} max`
        : 'Daily trade cap disabled (0 = unlimited)';
    }
    syncAutomationUI({
      auto_execute_signals: settings.auto_execute_signals,
      auto_close_trades: settings.auto_close_trades,
    });
  }

  function readConfigForm() {
    const form = $('#scout-config-form');
    if (!form) return {};
    const strengths = [];
    form.querySelectorAll('input[name="auto_enter_strengths"]:checked').forEach(cb => {
      strengths.push(cb.value);
    });
    const signalTypes = [];
    form.querySelectorAll('input[name="auto_enter_signal_types"]:checked').forEach(cb => {
      signalTypes.push(cb.value);
    });
    const num = (name) => {
      const el = form.querySelector(`[name="${name}"]`);
      if (!el) return undefined;
      if (el.type === 'checkbox') return el.checked;
      if (el.type === 'number') return el.value === '' ? undefined : Number(el.value);
      return el.value;
    };
    return {
      auto_execute_signals: !!form.querySelector('[name="auto_execute_signals"]')?.checked,
      auto_close_trades: !!form.querySelector('[name="auto_close_trades"]')?.checked,
      use_investment_sizing: !!form.querySelector('[name="use_investment_sizing"]')?.checked,
      one_trade_per_symbol_per_day: !!form.querySelector('[name="one_trade_per_symbol_per_day"]')?.checked,
      dedupe_per_symbol: !!form.querySelector('[name="dedupe_per_symbol"]')?.checked,
      investment_per_trade_inr: num('investment_per_trade_inr'),
      auto_trade_quantity: num('auto_trade_quantity'),
      max_trades_per_day: num('max_trades_per_day'),
      trade_window_start: form.querySelector('[name="trade_window_start"]')?.value,
      trade_window_end: form.querySelector('[name="trade_window_end"]')?.value,
      push_dedupe_minutes: num('push_dedupe_minutes'),
      signal_valid_minutes: num('signal_valid_minutes'),
      max_move_from_open_pct: num('max_move_from_open_pct'),
      rs_margin_pct: num('rs_margin_pct'),
      compression_range_pct: num('compression_range_pct'),
      entry_slippage_pct: num('entry_slippage_pct'),
      min_candles: num('min_candles'),
      auto_enter_strengths: strengths,
      auto_enter_signal_types: signalTypes,
      min_net_profit_inr: num('min_net_profit_inr'),
      min_target_r: num('min_target_r'),
      breakeven_at_r: num('breakeven_at_r'),
      trail_stop_r_fraction: num('trail_stop_r_fraction'),
      zerodha_execute_orders: !!form.querySelector('[name="zerodha_execute_orders"]')?.checked,
      square_off_time: form.querySelector('[name="square_off_time"]')?.value,
      square_off_warn_minutes: num('square_off_warn_minutes'),
      wallet_utilization_pct: num('wallet_utilization_pct'),
      wallet_reserve_inr: num('wallet_reserve_inr'),
    };
  }

  async function loadScoutConfig() {
    const form = $('#scout-config-form');
    if (!form) return;
    setConfigNotice('');
    try {
      const data = await scoutApi('/settings');
      fillConfigForm(data.settings || {}, data.trades_opened_today);
    } catch (e) {
      setConfigNotice('Failed to load settings: ' + e.message, false);
    }
  }

  async function saveScoutConfig(ev) {
    if (ev) ev.preventDefault();
    const body = readConfigForm();
    try {
      const data = await scoutApi('/settings', {
        method: 'PUT',
        body: JSON.stringify(body),
      });
      const fresh = await scoutApi('/settings');
      fillConfigForm(data.settings || fresh.settings || body, fresh.trades_opened_today);
      setConfigNotice('Settings saved.', true);
      toast('Scout settings saved', 'info');
      const st = await scoutApi('/status');
      renderStatus(st);
    } catch (e) {
      setConfigNotice('Save failed: ' + e.message, false);
      toast('Settings save failed: ' + e.message, 'error');
    }
  }

  function bindConfigForm() {
    $('#scout-config-form')?.addEventListener('submit', saveScoutConfig);
    $('#scout-config-reload')?.addEventListener('click', () => loadScoutConfig());
  }

  // ---------- History (expandable grids) ----------
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

  function fmtDateTimeShort(iso) {
    if (!iso) return '—';
    const s = String(iso).replace(' ', 'T');
    try {
      const d = new Date(s);
      if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
      return d.toLocaleString('en-IN', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      });
    } catch (_) {
      return String(iso).slice(0, 16);
    }
  }

  function setupLabel(raw) {
    return String(raw || '—').replace(/_/g, ' ');
  }

  function modePill(mode, label) {
    const cls = mode === 'auto' ? 'scout-hist-pill--auto' : 'scout-hist-pill--manual';
    return `<span class="scout-hist-pill ${cls}">${escapeHtml(label || (mode === 'auto' ? 'Auto' : 'Manual'))}</span>`;
  }

  function tradeExecutionModes(t) {
    const entry = ((t.execution || {}).entry || {}).mode === 'auto' ? 'auto' : 'manual';
    const exit = ((t.execution || {}).exit || {}).mode === 'auto' ? 'auto' : 'manual';
    return { entry, exit, flowKey: `${entry}-${exit}` };
  }

  function flowLabel(entry, exit) {
    const e = entry === 'auto' ? 'Auto' : 'Manual';
    const x = exit === 'auto' ? 'Auto' : 'Manual';
    return `${e} → ${x}`;
  }

  function flowCell(entry, exit) {
    const key = `${entry}-${exit}`;
    const clsMap = {
      'auto-auto': 'scout-hist-flow--aa',
      'auto-manual': 'scout-hist-flow--am',
      'manual-auto': 'scout-hist-flow--ma',
      'manual-manual': 'scout-hist-flow--mm',
    };
    const cls = clsMap[key] || 'scout-hist-flow--mm';
    const title = key === 'auto-auto'
      ? 'Fully automated — auto-enter and auto-close'
      : `Entry: ${entry === 'auto' ? 'Auto' : 'Manual'}, Exit: ${exit === 'auto' ? 'Auto' : 'Manual'}`;
    return `<span class="scout-hist-flow ${cls}" title="${escapeHtml(title)}">${escapeHtml(flowLabel(entry, exit))}</span>`;
  }

  let _scoutHistTrades = [];
  let _scoutHistStats = null;
  let _scoutHistSummaryExtra = '';
  let _scoutHistFilters = { entry: 'all', exit: 'all', preset: '' };

  function defaultHistFilters() {
    return { entry: 'all', exit: 'all', preset: '' };
  }

  function matchesHistFilter(t, filters) {
    const { entry, exit } = tradeExecutionModes(t);
    const f = filters || _scoutHistFilters;
    if (f.preset === 'full-auto') return entry === 'auto' && exit === 'auto';
    if (f.preset === 'auto-enter') return entry === 'auto';
    if (f.preset === 'auto-close') return exit === 'auto';
    if (f.entry !== 'all' && entry !== f.entry) return false;
    if (f.exit !== 'all' && exit !== f.exit) return false;
    return true;
  }

  function filterHistTrades(trades, filters) {
    return (trades || []).filter(t => matchesHistFilter(t, filters));
  }

  function countFullAuto(trades) {
    return (trades || []).filter(t => {
      const m = tradeExecutionModes(t);
      return m.entry === 'auto' && m.exit === 'auto';
    }).length;
  }

  function renderHistFilterBar(totalCount) {
    const f = _scoutHistFilters;
    const presetActive = p => f.preset === p ? ' active' : '';
    const entryChecked = v => f.preset ? '' : (f.entry === v ? ' checked' : '');
    const exitChecked = v => f.preset ? '' : (f.exit === v ? ' checked' : '');
    const entryDisabled = f.preset ? ' disabled' : '';
    const exitDisabled = f.preset ? ' disabled' : '';
    return `
      <div class="scout-hist-filters-grid" id="scout-hist-main-filters">
        <div class="scout-hist-filter-group">
          <span class="muted">Entry</span>
          <label class="scout-hist-filter-opt"><input type="radio" name="hist-f-entry" value="all"${entryChecked('all')}${entryDisabled}> All</label>
          <label class="scout-hist-filter-opt"><input type="radio" name="hist-f-entry" value="auto"${entryChecked('auto')}${entryDisabled}> Auto</label>
          <label class="scout-hist-filter-opt"><input type="radio" name="hist-f-entry" value="manual"${entryChecked('manual')}${entryDisabled}> Manual</label>
        </div>
        <div class="scout-hist-filter-group">
          <span class="muted">Exit</span>
          <label class="scout-hist-filter-opt"><input type="radio" name="hist-f-exit" value="all"${exitChecked('all')}${exitDisabled}> All</label>
          <label class="scout-hist-filter-opt"><input type="radio" name="hist-f-exit" value="auto"${exitChecked('auto')}${exitDisabled}> Auto</label>
          <label class="scout-hist-filter-opt"><input type="radio" name="hist-f-exit" value="manual"${exitChecked('manual')}${exitDisabled}> Manual</label>
        </div>
        <div class="scout-hist-filter-group">
          <span class="muted">Presets</span>
          <button type="button" class="btn btn-sm btn-ghost scout-hist-preset-btn${presetActive('full-auto')}" data-hist-preset="full-auto">Full auto</button>
          <button type="button" class="btn btn-sm btn-ghost scout-hist-preset-btn${presetActive('auto-enter')}" data-hist-preset="auto-enter">Auto-enter</button>
          <button type="button" class="btn btn-sm btn-ghost scout-hist-preset-btn${presetActive('auto-close')}" data-hist-preset="auto-close">Auto-close</button>
          <button type="button" class="btn btn-sm btn-ghost" id="scout-hist-filter-clear">Clear</button>
        </div>
        <div class="scout-hist-filter-result" id="scout-hist-filter-result"></div>
      </div>`;
  }

  function renderHistFilterResult(filtered, total) {
    const agg = aggregateTrades(filtered);
    const fullAuto = countFullAuto(filtered);
    const totalFullAuto = countFullAuto(total);
    const pnlCls = agg.pnl >= 0 ? 'pnl-profit' : 'pnl-loss';
    const showing = filtered.length === total.length
      ? `Showing all <strong>${total.length}</strong> trades`
      : `Showing <strong>${filtered.length}</strong> of <strong>${total.length}</strong> trades`;
    return `${showing} · Win <strong>${agg.win_pct}%</strong> · P&amp;L <strong class="${pnlCls}">${fmtPnl(agg.pnl)}</strong> · Full auto <strong>${fullAuto}</strong>${filtered.length !== total.length ? ` <span class="muted">(${totalFullAuto} in period)</span>` : ''}`;
  }

  function updateAllTradesGridView(panel) {
    const host = panel.querySelector('#scout-hist-all-grid-host');
    const resultEl = panel.querySelector('#scout-hist-filter-result');
    const summaryEl = panel.querySelector('#scout-hist-all-summary-extra');
    if (!host) return;
    const filtered = filterHistTrades(_scoutHistTrades, _scoutHistFilters);
    host.innerHTML = renderTradeGrid(filtered, 'scout-hist-all');
    bindHistoryGridToggles(host);
    if (resultEl) {
      resultEl.innerHTML = renderHistFilterResult(filtered, _scoutHistTrades);
    }
    if (summaryEl) {
      const agg = aggregateTrades(filtered);
      const isFiltered = filtered.length !== _scoutHistTrades.length || _scoutHistFilters.preset
        || _scoutHistFilters.entry !== 'all' || _scoutHistFilters.exit !== 'all';
      summaryEl.innerHTML = isFiltered
        ? renderSummaryCells(agg, `<span class="scout-hist-sum-meta"><span class="muted">Filtered</span> <strong>${filtered.length}/${_scoutHistTrades.length}</strong></span>`)
        : renderSummaryCells(aggregateTrades(_scoutHistTrades), _scoutHistSummaryExtra);
    }
  }

  function bindHistoryFilters(panel) {
    const bar = panel.querySelector('#scout-hist-main-filters');
    if (!bar || bar.dataset.bound === '1') return;
    bar.dataset.bound = '1';

    bar.querySelectorAll('input[name="hist-f-entry"]').forEach(inp => {
      inp.addEventListener('change', () => {
        if (inp.disabled) return;
        _scoutHistFilters = { entry: inp.value, exit: _scoutHistFilters.exit, preset: '' };
        syncHistFilterControls(panel);
        updateAllTradesGridView(panel);
      });
    });
    bar.querySelectorAll('input[name="hist-f-exit"]').forEach(inp => {
      inp.addEventListener('change', () => {
        if (inp.disabled) return;
        _scoutHistFilters = { entry: _scoutHistFilters.entry, exit: inp.value, preset: '' };
        syncHistFilterControls(panel);
        updateAllTradesGridView(panel);
      });
    });
    bar.querySelectorAll('[data-hist-preset]').forEach(btn => {
      btn.addEventListener('click', () => {
        const p = btn.getAttribute('data-hist-preset');
        _scoutHistFilters = _scoutHistFilters.preset === p
          ? defaultHistFilters()
          : { entry: 'all', exit: 'all', preset: p };
        syncHistFilterControls(panel);
        updateAllTradesGridView(panel);
      });
    });
    bar.querySelector('#scout-hist-filter-clear')?.addEventListener('click', () => {
      _scoutHistFilters = defaultHistFilters();
      syncHistFilterControls(panel);
      updateAllTradesGridView(panel);
    });
  }

  function syncHistFilterControls(panel) {
    const bar = panel.querySelector('#scout-hist-main-filters');
    if (!bar) return;
    const f = _scoutHistFilters;
    const presetActive = !!f.preset;
    bar.querySelectorAll('input[name="hist-f-entry"]').forEach(inp => {
      inp.disabled = presetActive;
      inp.checked = !presetActive && inp.value === f.entry;
    });
    bar.querySelectorAll('input[name="hist-f-exit"]').forEach(inp => {
      inp.disabled = presetActive;
      inp.checked = !presetActive && inp.value === f.exit;
    });
    bar.querySelectorAll('[data-hist-preset]').forEach(btn => {
      btn.classList.toggle('active', btn.getAttribute('data-hist-preset') === f.preset);
    });
  }

  function pnlCell(pnl, pct, netPnl, charges) {
    const n = Number(pnl || 0);
    const net = netPnl != null ? Number(netPnl) : n;
    const cls = net >= 0 ? 'pnl-profit' : 'pnl-loss';
    const pill = net >= 0 ? 'scout-hist-pill--win' : 'scout-hist-pill--loss';
    const chargeHint = charges != null && Number(charges) > 0
      ? `<span class="muted" title="Gross ${fmtPnl(n)} − charges ${fmtPnl(charges)}">net</span> `
      : '';
    return `${chargeHint}<span class="${cls}"><strong>${fmtPnl(net)}</strong></span> <span class="scout-hist-pill ${pill}">${Number(pct || 0).toFixed(2)}%</span>`;
  }

  function conditionsShort(entry, exit) {
    const parts = [];
    (entry && entry.conditions || []).slice(0, 2).forEach(c => {
      parts.push(`${c.label}: ${c.value}`);
    });
    if (exit && exit.trigger_label) parts.push(`Exit: ${exit.trigger_label}`);
    return parts.join(' · ') || '—';
  }

  function renderConditionDetail(title, execInfo) {
    if (!execInfo) return '';
    const items = (execInfo.conditions || []).map(c => {
      const cls = c.ok === true ? 'scout-hist-cond-ok' : (c.ok === false ? 'scout-hist-cond-bad' : '');
      return `<li class="${cls}"><strong>${escapeHtml(c.label)}</strong> — ${escapeHtml(c.value || '')}</li>`;
    }).join('');
    return `
      <div class="scout-hist-detail-col">
        <h4>${escapeHtml(title)} · ${escapeHtml(execInfo.mode_label || '')}${execInfo.trigger_label ? ' · ' + escapeHtml(execInfo.trigger_label) : ''}</h4>
        ${items ? `<ul>${items}</ul>` : '<span class="muted">No detail recorded</span>'}
      </div>`;
  }

  function tradeNetPnl(t) {
    if (t.net_pnl != null && t.net_pnl !== '') return Number(t.net_pnl);
    return Number(t.pnl || 0);
  }

  function aggregateTrades(trades) {
    let wins = 0;
    let grossPnl = 0;
    let netPnl = 0;
    let charges = 0;
    const winAmounts = [];
    const lossAmounts = [];
    (trades || []).forEach(t => {
      const gross = Number(t.gross_pnl != null ? t.gross_pnl : (t.pnl || 0));
      const net = tradeNetPnl(t);
      const ch = Number(t.total_charges || Math.max(0, gross - net));
      grossPnl += gross;
      netPnl += net;
      charges += ch;
      if (net > 0) {
        wins += 1;
        winAmounts.push(net);
      } else if (net < 0) {
        lossAmounts.push(net);
      }
    });
    const n = (trades || []).length;
    const winSum = winAmounts.reduce((a, b) => a + b, 0);
    const lossSum = Math.abs(lossAmounts.reduce((a, b) => a + b, 0));
    return {
      count: n,
      wins,
      win_pct: n ? Math.round(wins / n * 100) : 0,
      pnl: Math.round(grossPnl * 100) / 100,
      net_pnl: Math.round(netPnl * 100) / 100,
      total_charges: Math.round(charges * 100) / 100,
      avg_win: winAmounts.length ? Math.round(winSum / winAmounts.length * 100) / 100 : 0,
      avg_loss: lossAmounts.length ? Math.round(lossAmounts.reduce((a, b) => a + b, 0) / lossAmounts.length * 100) / 100 : 0,
      profit_factor: lossSum > 0 ? Math.round(winSum / lossSum * 100) / 100 : null,
    };
  }

  function renderTradeGrid(trades, gridId) {
    if (!trades || !trades.length) {
      return '<div class="empty" style="padding:12px">No trades in this group.</div>';
    }
    const rows = trades.map(t => {
      const pnl = Number(t.gross_pnl != null ? t.gross_pnl : (t.pnl || 0));
      const net = tradeNetPnl(t);
      const charges = t.total_charges != null ? Number(t.total_charges) : null;
      const pos = tradePositionMeta(t.action);
      const exec = t.execution || {};
      const entry = exec.entry || {};
      const exit = exec.exit || {};
      const modes = tradeExecutionModes(t);
      const rowId = `${gridId}-r-${t.id}`;
      return `
        <tr data-hist-row="${rowId}" data-flow="${modes.flowKey}">
          <td><button type="button" class="scout-hist-expand-btn" data-hist-toggle="${rowId}" aria-label="Toggle detail">▶</button></td>
          <td>TRD #${escapeHtml(String(t.id))}</td>
          <td>${t.signal_id != null ? 'SIG #' + escapeHtml(String(t.signal_id)) : '—'}</td>
          <td><strong>${escapeHtml(t.symbol)}</strong></td>
          <td>${escapeHtml(setupLabel(t.signal_type))}</td>
          <td><span class="tag tag-${pos.sideCls}">${escapeHtml(pos.side)}</span></td>
          <td class="num">${escapeHtml(String(t.quantity || 1))}</td>
          <td class="num">${fmtPx(t.entry_price)}</td>
          <td class="num">${fmtPx(t.exit_price)}</td>
          <td>${fmtDateTimeShort(t.closed_at || t.exited_at)}</td>
          <td class="num">${pnlCell(pnl, t.pnl_pct, net, charges)}</td>
          <td>${flowCell(modes.entry, modes.exit)}</td>
          <td>${modePill(entry.mode, entry.mode === 'auto' ? 'Auto-enter' : 'Manual')}</td>
          <td>${modePill(exit.mode, exit.mode === 'auto' ? 'Auto-close' : 'Manual')}</td>
          <td>${escapeHtml(exit.trigger_label || '—')}</td>
          <td class="scout-hist-cond-short">${escapeHtml(conditionsShort(entry, exit))}</td>
        </tr>
        <tr class="scout-hist-detail-row" data-hist-detail="${rowId}" hidden>
          <td colspan="16">
            <div class="scout-hist-detail-inner">
              ${renderConditionDetail('Entry', entry)}
              ${renderConditionDetail('Exit', exit)}
              ${t.signal_reason ? `<div class="scout-hist-detail-col"><h4>Signal reason</h4><p class="muted">${escapeHtml(t.signal_reason)}</p></div>` : ''}
            </div>
          </td>
        </tr>`;
    }).join('');

    return `
      <div class="scout-hist-grid-wrap">
        <table class="scout-hist-grid" id="${escapeHtml(gridId)}">
          <thead>
            <tr>
              <th></th>
              <th>TRD#</th>
              <th>SIG#</th>
              <th>Symbol</th>
              <th>Setup</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Entry ₹</th>
              <th>Exit ₹</th>
              <th>Closed</th>
              <th>P&amp;L (net)</th>
              <th>Flow</th>
              <th>Entry mode</th>
              <th>Exit mode</th>
              <th>Exit trigger</th>
              <th>Conditions</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function renderSummaryCells(agg, extra) {
    const netCls = (agg.net_pnl != null ? agg.net_pnl : agg.pnl) >= 0 ? 'pnl-profit' : 'pnl-loss';
    const netVal = agg.net_pnl != null ? agg.net_pnl : agg.pnl;
    const pf = agg.profit_factor != null ? `<span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">PF</span> <strong>${agg.profit_factor}</strong></span>` : '';
    const avgWin = agg.avg_win ? `<span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Avg win</span> <strong class="pnl-profit">${fmtPnl(agg.avg_win)}</strong></span>` : '';
    const avgLoss = agg.avg_loss ? `<span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Avg loss</span> <strong class="pnl-loss">${fmtPnl(agg.avg_loss)}</strong></span>` : '';
    const charges = agg.total_charges ? `<span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Charges</span> <strong>${fmtPnl(agg.total_charges)}</strong></span>` : '';
    return `
      <span class="scout-hist-sum-meta"><span class="muted">Trades</span> <strong>${agg.count}</strong></span>
      <span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Win%</span> <strong>${agg.win_pct}%</strong></span>
      <span class="scout-hist-sum-meta"><span class="muted">Net P&amp;L</span> <strong class="${netCls}">${fmtPnl(netVal)}</strong></span>
      ${charges}${pf}${avgWin}${avgLoss}
      ${extra || ''}`;
  }

  function renderHistoryStatsBanner(stats) {
    if (!stats) return '';
    const agg = {
      count: stats.total_trades || 0,
      wins: stats.wins || 0,
      win_pct: stats.win_rate_pct || 0,
      pnl: stats.total_pnl || 0,
      net_pnl: stats.total_net_pnl != null ? stats.total_net_pnl : stats.total_pnl,
      total_charges: stats.total_charges || 0,
      avg_win: stats.avg_win || 0,
      avg_loss: stats.avg_loss || 0,
      profit_factor: stats.profit_factor,
    };
    const grossWr = stats.gross_win_rate_pct != null
      ? `<span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Gross win%</span> <strong>${stats.gross_win_rate_pct}%</strong></span>`
      : '';
    return `
      <div class="scout-hist-stats-banner">
        ${renderSummaryCells(agg, grossWr)}
      </div>`;
  }

  function groupTradesByType(trades) {
    const map = {};
    (trades || []).forEach(t => {
      const k = String(t.signal_type || 'UNKNOWN');
      (map[k] = map[k] || []).push(t);
    });
    return Object.keys(map).sort().map(k => ({ key: k, trades: map[k], agg: aggregateTrades(map[k]) }));
  }

  function bindHistoryGridToggles(root) {
    (root || document).querySelectorAll('[data-hist-toggle]').forEach(btn => {
      if (btn.dataset.bound === '1') return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-hist-toggle');
        const detail = (root || document).querySelector(`[data-hist-detail="${id}"]`);
        if (!detail) return;
        const open = detail.hidden;
        detail.hidden = !open;
        btn.textContent = open ? '▼' : '▶';
      });
    });
  }

  function renderHistoryPanel(stats, trades) {
    const panel = $('#scout-history-panel');
    if (!panel) return;

    _scoutHistTrades = trades || [];
    _scoutHistStats = stats;
    _scoutHistFilters = defaultHistFilters();

    if (!trades || !trades.length) {
      panel.className = 'scout-history-panel';
      panel.innerHTML = '<div class="empty">No closed trades in this period.</div>';
      return;
    }

    const allAgg = aggregateTrades(trades);
    const auto = (stats && stats.automation) || {};
    const fullAutoTotal = countFullAuto(trades);
    const summaryExtra = `
            <span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Auto-in</span> <strong>${auto.auto_entry_count || 0}</strong></span>
            <span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Auto-out</span> <strong>${auto.auto_exit_count || 0}</strong></span>
            <span class="scout-hist-sum-meta"><span class="muted">Full auto</span> <strong>${fullAutoTotal}</strong></span>`;
    _scoutHistSummaryExtra = summaryExtra;
    const typeGroups = groupTradesByType(trades);

    const typeSubsections = typeGroups.map((g, i) => `
      <details class="scout-hist-subsection">
        <summary>
          <span class="scout-hist-sum-label">${escapeHtml(setupLabel(g.key))}</span>
          ${renderSummaryCells(g.agg)}
        </summary>
        ${renderTradeGrid(g.trades, `scout-hist-type-${i}`)}
      </details>`).join('');

    panel.className = 'scout-history-panel';
    panel.innerHTML = `
      ${renderHistoryStatsBanner(stats)}
      <details class="scout-hist-section" open>
        <summary>
          <span class="scout-hist-sum-label">All closed trades</span>
          <span id="scout-hist-all-summary-extra">${renderSummaryCells(allAgg, summaryExtra)}</span>
        </summary>
        ${renderHistFilterBar(trades.length)}
        <div id="scout-hist-all-grid-host">${renderTradeGrid(trades, 'scout-hist-all')}</div>
      </details>

      <details class="scout-hist-section">
        <summary>
          <span class="scout-hist-sum-label">By signal type</span>
          <span class="scout-hist-sum-meta"><span class="muted">Types</span> <strong>${typeGroups.length}</strong></span>
          <span class="scout-hist-sum-meta scout-hist-sum-hide-sm"><span class="muted">Trades</span> <strong>${allAgg.count}</strong></span>
          <span class="scout-hist-sum-meta"><span class="muted">Total net P&amp;L</span> <strong class="${allAgg.net_pnl >= 0 ? 'pnl-profit' : 'pnl-loss'}">${fmtPnl(allAgg.net_pnl)}</strong></span>
        </summary>
        <div class="scout-hist-subsections">${typeSubsections}</div>
      </details>`;

    bindHistoryGridToggles(panel);
    bindHistoryFilters(panel);
    const resultEl = panel.querySelector('#scout-hist-filter-result');
    if (resultEl) {
      resultEl.innerHTML = renderHistFilterResult(trades, trades);
    }
  }

  async function loadScoutHistory() {
    const dates = defaultHistDates();
    const q = '?from_date=' + encodeURIComponent(dates.from) + '&to_date=' + encodeURIComponent(dates.to);
    const panel = $('#scout-history-panel');
    try {
      const [stats, hist] = await Promise.all([
        scoutApi('/history/stats' + q),
        scoutApi('/history/trades' + q),
      ]);
      renderHistoryPanel(stats, hist.trades || []);
    } catch (e) {
      if (panel) {
        panel.className = 'scout-history-panel';
        panel.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
      }
    }
  }

  // ---------- Tab lifecycle ----------
  let _scoutTimer = null;
  let _scoutFlowTimer = null;

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

  function stopScoutFlowAutoRefresh() {
    if (_scoutFlowTimer) {
      clearInterval(_scoutFlowTimer);
      _scoutFlowTimer = null;
    }
  }

  function startScoutAutoRefresh() {
    stopScoutAutoRefresh();
    _scoutTimer = setInterval(loadScoutSignals, _scoutPollMs);
  }

  function startScoutFlowAutoRefresh() {
    stopScoutFlowAutoRefresh();
    _scoutFlowTimer = setInterval(loadScoutFlow, _scoutPollMs);
  }

  function onScoutTabEnter(tab) {
    updateScoutSubtabs(tab);
    if (tab === 'scout-signals') {
      loadScoutSignals().then(() => {
        startScoutAutoRefresh();
        startScoutLiveRefresh();
      });
      loadScoutFlow().then(() => startScoutFlowAutoRefresh());
    } else {
      stopScoutAutoRefresh();
      stopScoutLiveRefresh();
      if (tab === 'scout-watchlist') {
        _wlSearch = ($('#scout-wl-search')?.value || '').trim();
        loadScoutWatchlist({ reset: true });
      }
      if (tab === 'scout-trades') {
        loadScoutFlow().then(() => startScoutFlowAutoRefresh());
      } else if (tab !== 'scout-signals') {
        stopScoutFlowAutoRefresh();
      }
      if (tab === 'scout-history') loadScoutHistory();
      if (tab === 'scout-errors') loadScoutErrors();
      if (tab === 'scout-config') loadScoutConfig();
    }
  }

  function onScoutTabLeave() {
    stopScoutAutoRefresh();
    stopScoutFlowAutoRefresh();
    stopScoutLiveRefresh();
  }

  SCOUT_TABS.forEach(tab => {
    if (typeof window.registerDashboardTab === 'function') {
      window.registerDashboardTab(tab, () => onScoutTabEnter(tab), onScoutTabLeave);
    }
  });

  window.loadScoutFlow = loadScoutFlow;
  window.loadScoutSignals = loadScoutSignals;
  window.loadScoutErrors = loadScoutErrors;
  window.loadScoutHistory = loadScoutHistory;
  window.loadScoutConfig = loadScoutConfig;

  document.addEventListener('DOMContentLoaded', () => {
    bindScoutSubtabs();
    bindNavSections();
    bindWatchlistToolbar();
    bindAutomationControls();
    bindConfigForm();
    $('#scout-err-apply')?.addEventListener('click', () => loadScoutErrors());
    $('#scout-err-recheck')?.addEventListener('click', () => rerunZerodhaChecks());
    $('#scout-err-search')?.addEventListener('keydown', e => { if (e.key === 'Enter') loadScoutErrors(); });
    $('#scout-refresh')?.addEventListener('click', () => loadScoutSignals());
    $('#scout-hist-apply')?.addEventListener('click', () => loadScoutHistory());
  });
})();
