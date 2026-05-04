// =====================================================================
// Options Advisor — Dashboard JS
// Vanilla JS, no framework. Mobile-first.
// =====================================================================
'use strict';

const API = (path, opts={}) => fetch(path, opts).then(async r => {
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).error || r.statusText);
  return r.json();
});

const $ = sel => document.querySelector(sel);
const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));
const fmt = n => (n == null ? '—' : Number(n).toLocaleString('en-IN', {maximumFractionDigits: 2}));
const fmtPct = n => (n == null ? '—' : Number(n).toFixed(1) + '%');
const fmtDt   = s => { if (!s) return '—'; try { const d = new Date(s); return d.toLocaleString('en-IN', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:false}); } catch(e) { return String(s); } };
const fmtDate = s => { if (!s) return '—'; try { const d = new Date(s); return d.toLocaleDateString('en-IN', {day:'2-digit', month:'short', year:'numeric'}); } catch(e) { return String(s); } };

// "₹500 (67% of credit ₹750)" — shows a derived value as % of its base
const pctHint = (val, base, label = '') => {
  if (val == null || base == null || base === 0) return '';
  const p = (Math.abs(val) / Math.abs(base) * 100).toFixed(0);
  const lbl = label ? `${label} ` : '';
  return `<span class="pct-hint"> (${p}% of ${lbl}₹${fmt(base)})</span>`;
};
// "+400 pts, +1.6%" from spot — shows price level relative to spot
const spotDist = (level, spot) => {
  if (level == null || spot == null || spot === 0) return '';
  const diff = level - spot;
  const pct = (Math.abs(diff) / spot * 100).toFixed(1);
  const sign = diff >= 0 ? '+' : '\u2212';
  return `<span class="pct-hint">\u00a0(${sign}${fmt(Math.abs(diff))} pts, ${sign}${pct}% from \u20b9${fmt(spot)})</span>`;
};

const toast = (msg, kind='info') => {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toast-container').appendChild(el);
  setTimeout(() => el.remove(), 4000);
};

// ---------------- Tab switching ----------------
const TABS = ['suggestion', 'trades', 'history', 'logs', 'jobs', 'wsmon', 'config'];
function switchTab(name) {
  TABS.forEach(t => {
    const panel = document.getElementById(`panel-${t}`);
    if (!panel) return;
    panel.classList.toggle('active', t === name);
    panel.setAttribute('aria-hidden', t !== name);
  });
  $$('.nav-item, .bnav-item').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  if (name === 'suggestion') loadSuggestion();
  if (name === 'trades')     loadTrades();
  if (name === 'history')    loadHistory();
  if (name === 'logs')       loadLogs();
  if (name === 'jobs')       loadJobs();
  if (name === 'wsmon')      loadWsMonitor();
  if (name === 'config')     loadConfig();
  // Stop jobs auto-refresh when leaving the tab
  if (name !== 'jobs')  stopJobsAutoRefresh();
  if (name !== 'wsmon') stopWsMonitorAutoRefresh();
}
$$('.nav-item, .bnav-item').forEach(b =>
  b.addEventListener('click', () => switchTab(b.dataset.tab))
);

// ---------------- Notifications ----------------
async function refreshNotifBadge() {
  try {
    const data = await API('/api/notifications?unread=1');
    const n = data.notifications.length;
    const c = $('#notif-count');
    c.textContent = n;
    c.hidden = n === 0;
  } catch {}
}
async function openNotifDrawer() {
  const drawer = $('#notif-drawer');
  drawer.hidden = false;
  const data = await API('/api/notifications');
  const list = $('#notif-list');
  list.innerHTML = '';
  if (!data.notifications.length) {
    list.innerHTML = '<div class="empty">No notifications.</div>';
    return;
  }
  for (const n of data.notifications) {
    const div = document.createElement('div');
    div.className = 'notif' + (n.is_read ? '' : ' unread');
    div.innerHTML = `
      <h4>${escapeHtml(n.title)}</h4>
      <p>${escapeHtml(n.body || '')}</p>
      <p class="muted" style="font-size:.75rem">${escapeHtml(n.created_at || '')}</p>`;
    div.addEventListener('click', async () => {
      if (!n.is_read) {
        await API(`/api/notifications/${n.id}/read`, {method:'POST'});
        div.classList.remove('unread');
        refreshNotifBadge();
      }
    });
    list.appendChild(div);
  }
}
$('#notif-btn').addEventListener('click', openNotifDrawer);
$('#notif-close').addEventListener('click', () => $('#notif-drawer').hidden = true);
$('#notif-mark-all').addEventListener('click', async () => {
  await API('/api/notifications/read-all', {method:'POST'});
  refreshNotifBadge(); openNotifDrawer();
});

// ---------------- Tab 1: Suggestion ----------------
async function loadSuggestion() {
  const c = $('#suggestion-container');
  c.className = 'loading'; c.textContent = 'Loading…';
  try {
    // Fetch system status (best-effort) for the top banner; never blocks
    // the suggestion render if it fails.
    let bannerHtml = '';
    try {
      const st = await API('/api/system-status');
      const banners = [];
      if (st.circuit_breaker_active) {
        banners.push(`<div class="sys-banner sys-banner-err">\ud83d\udea8 Daily P&amp;L circuit breaker is <strong>ACTIVE</strong> \u2014 new executions are blocked until reset.</div>`);
      }
      if (st.kill_switch) {
        banners.push(`<div class="sys-banner sys-banner-err">\ud83d\uded1 Kill switch is ON \u2014 all alerts and execution are paused.</div>`);
      }
      if (st.trade_execution_enabled === false) {
        banners.push(`<div class="sys-banner sys-banner-warn">\u26a0\ufe0f Trade execution disabled by runtime flag.</div>`);
      }
      bannerHtml = banners.join('');
    } catch {}
    const data = await API('/api/suggestion/today');
    const list = data.suggestions || [];
    if (!list.length) {
      c.className = '';
      c.innerHTML = bannerHtml + '<div class="empty">No suggestion yet.</div>';
      return;
    }
    c.className = '';
    c.innerHTML = bannerHtml + list.map(s => renderSuggestion(s, false, list)).join('');
    bindSuggestionActions();
  } catch (e) {
    c.className = ''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

// ── Computed Exit Plan ───────────────────────────────────────────────────────
// Derives profit target and per-side stop loss entirely from suggestion data.
// Works for every strategy, every suggestion (old or new) — no plain_english parsing.
function renderExitPlan(s) {
  const legs     = s.legs || [];
  const strategy = s.strategy || '';
  const np       = s.net_credit    != null ? parseFloat(s.net_credit)    : null;
  const dte      = s.dte           != null ? parseInt(s.dte)             : null;
  const slLevel  = s.stop_loss_level != null ? parseFloat(s.stop_loss_level) : null;
  const und      = s.underlying || 'Index';
  const isCredit = np != null && np > 0;
  const isDebit  = np != null && np < 0;

  const scLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE');
  const spLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'PE');

  const rows = [];

  // ── 1. Profit target — strategy-specific capture % ───────────────────────
  // Iron Butterfly: narrow wings → gamma risk rises fast → exit at 25%
  // All other credit spreads: 50% (Tastyworks research, EV maximised at ~50%)
  // Long Straddle/Strangle: DTE-aware multiple of debit (mirrors
  //   engine.leg_builder.long_premium_target_multiple). Replaces the
  //   historical flat 2× target which was unrealistic at short DTE.
  // Long Call/Put: DTE-aware (same formula)
  // Debit spreads (Bull Call, Bear Put): limited profit → 50% of spread width
  if (isCredit) {
    const pct     = strategy === 'IRON_BUTTERFLY' ? 0.25 : 0.50;
    const pctLabel = strategy === 'IRON_BUTTERFLY' ? '25%' : '50%';
    const target  = Math.round(np * pct * 10) / 10;
    const dteStr  = dte ? ` — around day ${Math.round(dte * 0.35)}–${Math.round(dte * 0.55)}` : '';
    const reason  = strategy === 'IRON_BUTTERFLY' ? ' (narrow wings — exit earlier)' : '';
    rows.push({ label: 'Profit target', val: `close when ${pctLabel} of credit is captured (₹${fmt(target)}/unit retained)${dteStr}${reason}`, key: true });
  } else if (isDebit) {
    const debit = Math.abs(np);
    // DTE-aware multiplier — see engine.leg_builder.long_premium_target_multiple
    // Defaults (config.py): base=0.50, dte_scale=14, cap=1.50.
    const TARGET_BASE = 0.50, TARGET_DTE_SCALE = 14.0, TARGET_MAX = 1.50;
    const dteSafe = (typeof dte === 'number' && dte > 0) ? dte : 0;
    const mult = dteSafe === 0
      ? TARGET_BASE
      : Math.min(TARGET_MAX, TARGET_BASE + dteSafe / TARGET_DTE_SCALE);
    if (['LONG_STRADDLE', 'LONG_STRANGLE'].includes(strategy)) {
      const target = Math.round(debit * mult * 10) / 10;
      const pctLabel = `${Math.round(mult * 100)}%`;
      rows.push({ label: 'Profit target', val: `close when position gains ₹${fmt(target)}/unit (${pctLabel} of debit, scaled to ${dteSafe} DTE)`, key: true });
    } else if (['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD'].includes(strategy)) {
      const target50 = Math.round(debit * 0.5 * 10) / 10;
      rows.push({ label: 'Profit target', val: `close when spread gains ₹${fmt(target50)}/unit (50% of debit paid)`, key: true });
    } else {
      // LONG_CALL, LONG_PUT — directional, also DTE-aware
      const target = Math.round(debit * mult * 10) / 10;
      const pctLabel = `${Math.round(mult * 100)}%`;
      rows.push({ label: 'Profit target', val: `close when position gains ₹${fmt(target)}/unit (${pctLabel} of premium, scaled to ${dteSafe} DTE)`, key: true });
    }
  }

  // ── 2. Stop loss — strategy-specific, clearly labelled ───────────────────
  const twoSided    = ['IRON_CONDOR', 'IRON_BUTTERFLY', 'JADE_LIZARD'].includes(strategy);
  const callSideOnly = ['BEAR_CALL_SPREAD', 'BULL_CALL_SPREAD'].includes(strategy);
  const putSideOnly  = ['BULL_PUT_SPREAD',  'BEAR_PUT_SPREAD' ].includes(strategy);

  if (twoSided && scLeg && spLeg) {
    // sl_level is the call-side SL (above short call). Derive put-side symmetrically.
    if (slLevel != null) {
      const buf = Math.round(slLevel - scLeg.strike);
      rows.push({ label: 'Call-side SL', val: `exit call spread if ${und} rises above ${fmt(slLevel)} (${buf} pts above short call ${fmt(scLeg.strike)})` });
      const putSl = Math.round(spLeg.strike - buf);
      rows.push({ label: 'Put-side SL',  val: `exit put spread if ${und} falls below ${fmt(putSl)} (${buf} pts below short put ${fmt(spLeg.strike)})` });
    } else {
      rows.push({ label: 'Call-side SL', val: `exit call spread if ${und} rises above short call ${fmt(scLeg.strike)}` });
      rows.push({ label: 'Put-side SL',  val: `exit put spread if ${und} falls below short put ${fmt(spLeg.strike)}` });
    }
  } else if (callSideOnly && scLeg) {
    if (slLevel != null) {
      const buf = Math.round(slLevel - scLeg.strike);
      const bufStr = buf > 0 ? ` (${buf} pts above short call ${fmt(scLeg.strike)})` : '';
      rows.push({ label: 'Call-side SL', val: `exit call spread if ${und} rises above ${fmt(slLevel)}${bufStr}` });
    } else {
      rows.push({ label: 'Call-side SL', val: `exit call spread if ${und} rises above short call ${fmt(scLeg.strike)}` });
    }
  } else if (putSideOnly && spLeg) {
    if (slLevel != null) {
      const buf = Math.round(spLeg.strike - slLevel);
      const bufStr = buf > 0 ? ` (${buf} pts below short put ${fmt(spLeg.strike)})` : '';
      rows.push({ label: 'Put-side SL', val: `exit put spread if ${und} falls below ${fmt(slLevel)}${bufStr}` });
    } else {
      rows.push({ label: 'Put-side SL', val: `exit put spread if ${und} falls below short put ${fmt(spLeg.strike)}` });
    }
  } else if (strategy === 'LONG_CALL') {
    rows.push({ label: 'Stop loss', val: `exit if position loses 50% of premium paid${slLevel ? ` or ${und} falls below ${fmt(slLevel)}` : ''}` });
  } else if (strategy === 'LONG_PUT') {
    rows.push({ label: 'Stop loss', val: `exit if position loses 50% of premium paid${slLevel ? ` or ${und} rises above ${fmt(slLevel)}` : ''}` });
  } else if (['LONG_STRADDLE', 'LONG_STRANGLE'].includes(strategy) && isDebit) {
    const slVal = Math.round(Math.abs(np) * 0.5 * 10) / 10;
    rows.push({ label: 'Stop loss', val: `exit if position value decays to 50% of debit paid (₹${fmt(slVal)}/unit lost)` });
  }

  if (!rows.length) return '';

  const rowsHtml = rows.map(r =>
    `<div class="tl-row${r.key ? ' tl-key' : ''}">
      <span class="tl-label">${escapeHtml(r.label)}</span>
      <span class="tl-val">${escapeHtml(r.val)}</span>
    </div>`
  ).join('');

  return `<div class="sug-section sug-exit-section">
    <div class="sug-section-title">Exit Plan</div>
    <div class="sug-timeline">${rowsHtml}</div>
  </div>`;
}

// ── Confidence checks breakdown ──────────────────────────────────────────────
// conditions_json is now always [{label, passed, detail}, ...] (array format).
// Legacy {conditions:[...strings...]} kept as safety fallback.
function renderConfidenceChecks(s) {
  let raw = s.conditions_json;
  if (!raw) return '';
  if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch { return ''; } }

  let checks = [];
  if (Array.isArray(raw)) {
    checks = raw.map(c => ({
      label:  c.label  || '',
      status: c.status || (c.passed === true ? 'PASS' : c.passed === false ? 'FAIL' : 'PASS'),
      detail: c.detail || '',
    }));
  } else if (raw.conditions && Array.isArray(raw.conditions)) {
    // Legacy fallback — DB row not yet migrated
    checks = raw.conditions.map(lbl => ({ label: lbl, status: 'PASS', detail: '(legacy format — no detail stored)' }));
  }
  if (!checks.length) return '';

  const STATUS_CLASS = { PASS: 'conf-pass', FAIL: 'conf-fail', SOFT_FAIL: 'conf-soft-fail', PASS_WARN: 'conf-warn', PASS_ERROR: 'conf-error' };
  const STATUS_ICON  = { PASS: '\u2713', FAIL: '\u2717', SOFT_FAIL: '\u2717', PASS_WARN: '\u26a0', PASS_ERROR: '\u26a1' };

  const nFail     = checks.filter(c => c.status === 'FAIL').length;
  const nSoftFail = checks.filter(c => c.status === 'SOFT_FAIL').length;
  const nWarn     = checks.filter(c => c.status === 'PASS_WARN').length;
  const nError    = checks.filter(c => c.status === 'PASS_ERROR').length;
  const total     = checks.length;
  const passed    = total - nFail - nSoftFail;
  const allPass   = nFail === 0 && nSoftFail === 0;
  const sid       = escapeHtml(s.suggestion_id || Math.random().toString(36).slice(2));

  let titleSuffix = '';
  if (nSoftFail > 0) titleSuffix += ` \u00b7 \u26a0 ${nSoftFail} soft gate${nSoftFail > 1 ? 's' : ''} not met — trade proceeds with caution`;
  if (nWarn  > 0) titleSuffix += ` \u00b7 \u26a0 ${nWarn} with missing data`;
  if (nError > 0) titleSuffix += ` \u00b7 \u26a1 ${nError} gate error${nError > 1 ? 's' : ''}`;

  const rows = checks.map(c => {
    const rowClass   = STATUS_CLASS[c.status] || 'conf-pass';
    const icon       = STATUS_ICON[c.status]  || '\u2713';
    const detailHtml = c.detail
      ? `<span class="conf-detail-text">${escapeHtml(c.detail)}</span>`
      : '<span class="conf-detail-na">\u2014</span>';
    return `<tr class="conf-check-row ${rowClass}">
      <td class="conf-icon">${icon}</td>
      <td class="conf-label">${escapeHtml(c.label)}</td>
      <td class="conf-detail">${detailHtml}</td>
    </tr>`;
  }).join('');

  return `<div class="conf-checks-panel" id="conf-${sid}" hidden>
    <div class="conf-checks-title">${allPass ? 'All' : passed + ' of'} ${total} confidence checks ${allPass ? 'passed \u2713' : 'passed'}${titleSuffix}</div>
    <table class="conf-checks-table">
      <thead><tr><th></th><th>Check</th><th>What was verified</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

// Helper: parse plain_english text into structured display
function renderPlainEnglishStructured(s) {
  const text = (s.plain_english || '').trim();
  if (!text) {
    return s.execution_window
      ? `<div class="exec-window-badge">📅 Execute: ${escapeHtml(s.execution_window)}</div>`
      : '';
  }
  const rawLines = text.split('\n').map(l => l.trim()).filter(Boolean);
  const introLines = [], timelineItems = [], entryItems = [];
  let confLine = '', mode = 'intro';
  for (const line of rawLines) {
    if (/^ENTRY(\s+THRESHOLDS)?$/i.test(line)) { mode = 'entry'; continue; }
    if (/^TARGET CLOSE/i.test(line))     { mode = 'target'; continue; }
    if (/^TIMELINE/i.test(line))         { mode = 'timeline'; continue; }
    if (/All \d+ confidence/i.test(line)) { confLine = line; continue; }
    if (line.startsWith('\u2022') || line.startsWith('-')) {
      const bullet = line.replace(/^[\u2022\-]\s*/, '').trim();
      if (mode === 'timeline') {
        // Skip lines that will be shown in the computed Exit Plan section instead
        if (/target\s+\d+%\s+profit|target\s+(exit|close)\b|\bsl:|\bstop[- ]?loss\b|hard\s+sl|close\s+(immediately\s+)?if\b|exit\s+(call|put|spread)\s+if\b|exit\s+if\s|exit.*immediately/i.test(bullet)) continue;
        timelineItems.push(bullet);
      } else if (mode === 'entry') entryItems.push(bullet);
      // target bullets already shown on leg chips — skip
    } else if (mode === 'intro') {
      introLines.push(line);
    }
  }
  const intro = introLines.join(' ').replace(/^\[.*?\]\s*/, '').trim();
  const spotMatch = intro.match(/trading at ([0-9,]+)/i);
  const ivMatch   = intro.match(/IV Rank[^\d]*(\d+)%/i);
  const spot   = spotMatch ? spotMatch[1] : (s.spot_at_generation ? Number(s.spot_at_generation).toLocaleString('en-IN') : null);
  const ivRank = ivMatch   ? ivMatch[1]   : null;
  const chips = [];
  if (s.underlying)       chips.push(`<span class="ctx-chip">${escapeHtml(s.underlying)}</span>`);
  if (s.expiry_type) {
    const badgeClass = s.expiry_type === 'Monthly' ? 'ctx-chip ctx-expiry-monthly' : 'ctx-chip ctx-expiry-weekly';
    chips.push(`<span class="${badgeClass}">${escapeHtml(s.expiry_type)}</span>`);
  }
  if (spot)               chips.push(`<span class="ctx-chip">Spot ₹${escapeHtml(spot)}</span>`);
  if (ivRank)             chips.push(`<span class="ctx-chip ctx-iv">IV Rank ${escapeHtml(ivRank)}%</span>`);
  // IV/HV chip — parsed from confidence gate detail in conditions_json
  (() => {
    if (!s.conditions_json) return;
    let raw = s.conditions_json;
    if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch { return; } }
    if (!Array.isArray(raw)) return;
    const ivGate = raw.find(c => (c.label || '').toLowerCase().includes('iv premium'));
    if (!ivGate) return;
    const m = (ivGate.detail || '').match(/IV\/HV ratio\s+([\d.]+)/i);
    if (!m) return;
    const ratio = parseFloat(m[1]);
    const isStale = ratio < 1.0;
    const chipClass = isStale ? 'ctx-chip ctx-data-stale' : 'ctx-chip ctx-iv';
    const tooltip = ratio >= 1.40
      ? `IV/HV ${ratio.toFixed(2)} \u2014 options overpriced vs realised vol (butterfly eligible)`
      : ratio >= 1.0
      ? `IV/HV ${ratio.toFixed(2)} \u2014 options moderately priced (condor preferred)`
      : `IV/HV ${ratio.toFixed(2)} \u2014 options cheaper than realised vol`;
    chips.push(`<span class="${chipClass}" title="${escapeHtml(tooltip)}">IV/HV ${ratio.toFixed(2)}</span>`);
  })();
  // Data provenance: show which NSE feed dates were used, with a stale warning
  // when any secondary feed lags the primary FO+IV date.
  if (s.data_date) {
    const foIvDate  = s.data_date.slice(0, 10);
    const spotDate  = s.spot_data_date  ? s.spot_data_date.slice(0, 10)  : null;
    const fiiDate   = s.fii_data_date   ? s.fii_data_date.slice(0, 10)   : null;
    const vixDate   = s.vix_data_date   ? s.vix_data_date.slice(0, 10)   : null;

    function fmtShort(d) {
      return new Date(d + 'T00:00:00').toLocaleDateString('en-IN',
        { day:'2-digit', month:'short', year:'2-digit' });
    }

    const foIvFmt = fmtShort(foIvDate);
    const allSame = (!spotDate || spotDate === foIvDate)
                 && (!fiiDate  || fiiDate  === foIvDate)
                 && (!vixDate  || vixDate  === foIvDate);

    if (allSame) {
      // Happy path: every feed is from the same date
      const tipLines = [
        `FO chain:    ${foIvFmt}`,
        `IV history:  ${foIvFmt}`,
        spotDate ? `Spot EOD:    ${foIvFmt}` : '',
        fiiDate  ? `FII data:    ${foIvFmt}` : '',
        vixDate  ? `VIX:         ${foIvFmt}` : '',
      ].filter(Boolean).join('\n');
      chips.push(`<span class="ctx-chip ctx-data-date" title="${escapeHtml(tipLines)}"` +
        ` style="cursor:help">NSE data \u00b7 ${escapeHtml(foIvFmt)}</span>`);
    } else {
      // Some feeds lagged — show a warning chip plus a full breakdown
      const staleFeed = [
        spotDate && spotDate !== foIvDate ? `Spot (${fmtShort(spotDate)})` : null,
        fiiDate  && fiiDate  !== foIvDate ? `FII (${fmtShort(fiiDate)})`   : null,
        vixDate  && vixDate  !== foIvDate ? `VIX (${fmtShort(vixDate)})`   : null,
      ].filter(Boolean).join(', ');
      const tipLines = [
        `FO chain:    ${foIvFmt}`,
        `IV history:  ${foIvFmt}`,
        spotDate ? `Spot EOD:    ${fmtShort(spotDate)}${spotDate !== foIvDate ? ' \u26a0' : ''}` : '',
        fiiDate  ? `FII data:    ${fmtShort(fiiDate)}${fiiDate  !== foIvDate ? ' \u26a0' : ''}` : '',
        vixDate  ? `VIX:         ${fmtShort(vixDate)}${vixDate  !== foIvDate ? ' \u26a0' : ''}` : '',
        '',
        `\u26a0 ${staleFeed} used older data`,
      ].filter(l => l !== null).join('\n');
      chips.push(`<span class="ctx-chip ctx-data-date ctx-data-stale" title="${escapeHtml(tipLines)}"` +
        ` style="cursor:help">NSE data \u00b7 ${escapeHtml(foIvFmt)} \u26a0</span>`);
    }
  }
  if (s.entry_date) {
    const ed = s.entry_date.slice(0, 10);
    const eFmt = new Date(ed + 'T00:00:00').toLocaleDateString('en-IN',
      { weekday:'short', day:'2-digit', month:'short', year:'2-digit' });
    chips.push(`<span class="ctx-chip ctx-entry-date" title="Intended execution date">Execute \u2192 ${escapeHtml(eFmt)}</span>`);
  }
  // Phase 2c: validator status (set by 09:35 IST intraday_validator)
  if (s.validator_status) {
    const vs = s.validator_status;
    if (vs === 'STILL_GOOD_0935') {
      chips.push(`<span class="ctx-chip ctx-pass" title="Validated by 09:35 IST intraday validator">\u2713 Still good 09:35</span>`);
    } else if (vs === 'STALE_0935' || vs === 'STALE_INTRADAY') {
      chips.push(`<span class="ctx-chip ctx-fail" title="Re-priced after open and was no longer actionable">\u2717 Stale 09:35</span>`);
    }
  }
  // Phase 2c: provenance chips (data source / trigger)
  if (s.data_source) {
    const cls = s.data_source === 'LIVE' ? 'ctx-chip ctx-iv'
              : s.data_source === 'EOD'  ? 'ctx-chip'
              : 'ctx-chip';
    const tip = s.provider ? `Source: ${s.data_source} via ${s.provider}` : `Source: ${s.data_source}`;
    chips.push(`<span class="${cls}" title="${escapeHtml(tip)}">${escapeHtml(s.data_source)}</span>`);
  }
  if (s.trigger_type) {
    const label = s.trigger_type === 'EOD_RUN'              ? 'EOD'
                : s.trigger_type === 'INTRADAY_VALIDATOR'   ? '09:35 check'
                : s.trigger_type === 'WS_REGEN'             ? 'Tick regen'
                : s.trigger_type === 'MANUAL'               ? 'Manual'
                : s.trigger_type;
    const tip = s.trigger_reason ? `Trigger: ${s.trigger_type}\n${s.trigger_reason}` : `Trigger: ${s.trigger_type}`;
    chips.push(`<span class="ctx-chip" title="${escapeHtml(tip)}">${escapeHtml(label)}</span>`);
  }
  if (s.confidence_score != null) {
    let _warnCount = 0, _errorCount = 0, _failCount = 0, _softFailCount = 0, _total = 7, _passCount = null;
    if (s.conditions_json) {
      let _raw = s.conditions_json;
      if (typeof _raw === 'string') { try { _raw = JSON.parse(_raw); } catch { _raw = null; } }
      if (Array.isArray(_raw)) {
        _warnCount     = _raw.filter(c => c.status === 'PASS_WARN').length;
        _errorCount    = _raw.filter(c => c.status === 'PASS_ERROR').length;
        _failCount     = _raw.filter(c => c.status === 'FAIL').length;
        _softFailCount = _raw.filter(c => c.status === 'SOFT_FAIL').length;
        _total         = _raw.length;
        // Derive pass count from conditions_json so chip and panel are always consistent
        _passCount     = _total - _failCount - _softFailCount;
      }
    }
    // Fall back to DB column only if conditions_json is absent/unparseable
    const displayScore = _passCount !== null ? _passCount : s.confidence_score;
    const hasIssues  = _warnCount > 0 || _errorCount > 0 || _softFailCount > 0;
    const chipClass  = _failCount > 0      ? 'ctx-chip ctx-fail conf-chip'
                     : _softFailCount > 0  ? 'ctx-chip ctx-warn conf-chip'
                     : hasIssues           ? 'ctx-chip ctx-warn conf-chip'
                     :                       'ctx-chip ctx-pass conf-chip';
    const warnSuffix = _errorCount > 0
      ? ` \u26a1 ${_errorCount} error${_errorCount > 1 ? 's' : ''}`
      : _softFailCount > 0 ? ` \u26a0 ${_softFailCount} soft fail${_softFailCount > 1 ? 's' : ''}`
      : _warnCount > 0 ? ` \u26a0 ${_warnCount} warned` : '';
    chips.push(`<span class="${chipClass}" data-sug-id="${escapeHtml(s.suggestion_id||'')}" style="cursor:pointer" title="Click to see all checks">${displayScore}/${_total} checks \u2713${warnSuffix} <span style="font-size:.7rem;opacity:.7">\u25bc</span></span><span class="conf-logic-info" tabindex="0" aria-label="Confidence gate logic">\u24d8<span class="conf-logic-popup"><strong>How gating works</strong><br><br><span style="color:#f87171">\u2717 Hard gate</span> &mdash; always blocks:<br>&nbsp;&bull; DTE within target band<br><br><span style="color:#fbbf24">\u2717 Soft gates</span> &mdash; need \u22655 of 7:<br>&nbsp;&bull; IV Rank in actionable zone<br>&nbsp;&bull; VIX stable or falling<br>&nbsp;&bull; PCR in neutral band<br>&nbsp;&bull; OI walls visible<br>&nbsp;&bull; Trend identifiable<br>&nbsp;&bull; IV premium vs realised vol (HV-20)<br>&nbsp;&bull; FII positioning aligned with trend<br><br><span style="color:#fbbf24">\u26a0 Warning (never blocks):</span><br>&nbsp;&bull; High-impact event this week<br><br><span style="opacity:.6;font-size:.72rem">1\u20132 soft gate misses = trade proceeds with caution<br>3+ soft gate misses = blocked</span><br><br><span style="opacity:.5;font-size:.72rem">\u26a0 = data unavailable &nbsp;\u26a1 = gate error</span></span></span>`);
  }
  // Reference date for "day N" → actual date conversion.
  // Use generated_on if present, else today.
  const refDateStr = s.generated_on || s.executed_on || null;
  const refDate = refDateStr ? new Date(refDateStr) : new Date();
  function dayToDate(n) {
    const d = new Date(refDate);
    d.setDate(d.getDate() + n);
    return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
  }
  function expandDays(text) {
    // Replace "day N–M" or "day N-M" → "day N to M (DD Mon – DD Mon)"
    return text
      .replace(/\bday\s+(\d+)\s*[–\-]\s*(\d+)/gi, (_, a, b) =>
        `day ${a} to ${b} (${dayToDate(+a)} – ${dayToDate(+b)})`)
      // Only expand standalone "day N" — \b after digits prevents backtracking to partial matches
      .replace(/\bday\s+(\d+)\b(?!\s*(?:to\b|[–\-]))/gi, (_, n) =>
        `day ${n} (${dayToDate(+n)})`);
  }
  // Split "Label: value" but reject if the text before : ends with a digit
  // (prevents time strings like "09:20" being treated as label "09" + val "20…")
  function splitLabelVal(item) {
    const ci = item.indexOf(':');
    if (ci < 0) return null;
    const label = item.slice(0, ci).trim();
    if (/\d$/.test(label) || !/[a-zA-Z]/.test(label)) return null;
    return { label, val: item.slice(ci + 1).trim() };
  }
  // Strip hard-coded ₹ amounts from narrative bullets — computed rows show the
  // authoritative values instead.
  function stripRupeeAmounts(text) {
    return text
      // " between ₹112–₹124 combined credit" / " for ₹60–₹68 combined credit"
      .replace(/\s+(?:between|for)\s+\u20b9[\d,.]+(?:[\u2013\-]\u20b9?[\d,.]+)?[^•\n]*/gi, '')
      // parenthetical amounts like "(₹59 decay)" or "(₹38.4 target)"
      .replace(/\s*\(\u20b9[^)]+\)/g, '')
      .trim();
  }
  const tlRows = timelineItems.map(item => {
    const clean = stripRupeeAmounts(item);
    const split = splitLabelVal(clean);
    if (!split) return `<div class="tl-row"><span class="tl-val" style="grid-column:span 2">${escapeHtml(expandDays(clean))}</span></div>`;
    const { label } = split;
    const val   = expandDays(split.val);
    const isKey = /execute by/i.test(label);
    return `<div class="tl-row${isKey ? ' tl-key' : ''}">
      <span class="tl-label">${escapeHtml(label)}</span>
      <span class="tl-val">${escapeHtml(val)}</span>
    </div>`;
  }).join('');
  const contextHtml = chips.length
    ? `<div class="sug-context">${chips.join('<span class="ctx-sep">·</span>')}</div>`
    : '';
  // From the intro paragraph, keep only sentences that carry qualitative rationale —
  // filter out sentences that duplicate what is already shown in chips or kv-grid:
  //   • spot price  ("trading at …")
  //   • rupee amounts ("₹")
  //   • strategy/legs recap  ("Strategy:")
  //   • stop-loss level  ("stop-loss if …" / "stop loss if …")
  const introSentences = intro
    .split(/(?<=[.!?])\s+/)                 // split on sentence boundaries
    .map(s => s.trim())
    .filter(s => s.length > 0
      && !/trading at/i.test(s)
      && !/\u20b9/.test(s)                  // ₹ symbol
      && !/Strategy:/i.test(s)
      && !/stop[-\s]loss if/i.test(s));
  const introHtml = introSentences.length
    ? `<p class="sug-intro">${escapeHtml(introSentences.join(' '))}</p>`
    : '';
  const entryHtml = (() => {
    if (!entryItems.length && !s.execution_window) return '';
    // Compute credit range from per-leg price bands — authoritative source
    // (plain_english narrative may show a different number; this is computed from DB)
    let _lo = 0, _hi = 0;
    const hasLegs = !!(s.legs && s.legs.length);
    (s.legs || []).forEach(l => {
      const sign = l.action === 'SELL' ? 1 : -1;
      const pLo = parseFloat(l.suggested_price_low  || l.suggested_price || 0);
      const pHi = parseFloat(l.suggested_price_high || l.suggested_price || 0);
      _lo += sign * (l.action === 'SELL' ? pLo : pHi);
      _hi += sign * (l.action === 'SELL' ? pHi : pLo);
    });
    const crLo = Math.min(_lo, _hi), crHi = Math.max(_lo, _hi);
    const dateStr = refDateStr ? fmtDate(refDateStr) : '';
    let dateInjected = false;
    const itemRows = entryItems.map(item => {
      const clean = stripRupeeAmounts(item);
      const split = splitLabelVal(clean);
      if (!split) return `<div class="tl-row tl-key"><span class="tl-val" style="grid-column:span 2">${escapeHtml(clean)}</span></div>`;
      const { label, val } = split;
      // Append date inline on the Execute row (first occurrence only)
      let valHtml = escapeHtml(val);
      if (!dateInjected && /^execute/i.test(label) && dateStr) {
        valHtml += ` <span class="muted" style="font-size:.8rem">\u00b7 ${escapeHtml(dateStr)}</span>`;
        dateInjected = true;
      }
      return `<div class="tl-row tl-key"><span class="tl-label">${escapeHtml(label)}</span><span class="tl-val">${valHtml}</span></div>`;
    }).join('');
    // If no Execute bullet absorbed the date, show it as its own row
    const dateRow = (dateStr && !dateInjected)
      ? `<div class="tl-row tl-key"><span class="tl-label">Date</span><span class="tl-val"><strong>${escapeHtml(dateStr)}</strong></span></div>`
      : '';
    const creditRow = hasLegs && crHi > crLo + 0.5
      ? `<div class="tl-row tl-key"><span class="tl-label">Acceptable credit</span>` +
        `<span class="tl-val"><strong>\u20b9${fmt(crLo)}\u2013\u20b9${fmt(crHi)}</strong><span class="muted" style="font-size:.75rem"> /unit \u00b7 from leg price bands</span></span></div>`
      : '';
    // If we have date + execute window + credit range, collapse into one summary row
    const execItem = entryItems.find(i => /^execute/i.test(i));
    const execTimeVal = execItem ? (() => {
      const split = splitLabelVal(execItem);
      // strip any "between ₹... credit" trailing text — shown separately as authoritative range
      const raw = split ? split.val : execItem.replace(/^execute\s*:?\s*/i, '');
      return raw.replace(/\s*between\s+[\u20b9₹][\d,.]+[–\-].*$/i, '').trim();
    })() : null;
    const canCollapse = dateStr && execTimeVal && hasLegs && crHi > crLo + 0.5;
    if (canCollapse) {
      const singleRow =
        `<div class="tl-row tl-key">` +
        `<span class="tl-label" style="color:var(--text)">${escapeHtml(dateStr)}</span>` +
        `<span class="tl-val">` +
          `<span style="color:var(--text-dim)">Execute </span>` +
          `<span style="color:var(--text)">${escapeHtml(execTimeVal)}</span>` +
          `<span style="color:var(--text-dim)"> &nbsp;\u00b7&nbsp; Acceptable Credit </span>` +
          `<strong style="color:var(--ok)">\u20b9${fmt(crLo)}\u2013\u20b9${fmt(crHi)}</strong>` +
          `<span style="color:var(--text-dim);font-size:.75rem"> /unit</span>` +
        `</span>` +
        `</div>`;
      const otherRows = entryItems
        .filter(i => !/^execute/i.test(i))
        .map(item => {
          const clean = stripRupeeAmounts(item);
          const split = splitLabelVal(clean);
          if (!split) return `<div class="tl-row tl-key"><span class="tl-val" style="grid-column:span 2">${escapeHtml(clean)}</span></div>`;
          return `<div class="tl-row tl-key"><span class="tl-label">${escapeHtml(split.label)}</span><span class="tl-val">${escapeHtml(split.val)}</span></div>`;
        }).join('');
      return `<div class="sug-section sug-entry-section"><div class="sug-section-title">Entry</div>` +
        `<div class="sug-timeline">${singleRow}${otherRows}</div></div>`;
    }
    if (entryItems.length) {
      return `<div class="sug-section sug-entry-section"><div class="sug-section-title">Entry</div>` +
        `<div class="sug-timeline">${dateRow}${itemRows}${creditRow}</div></div>`;
    }
    return s.execution_window
      ? `<div class="exec-window-badge">\ud83d\udcc5 Execute: ${escapeHtml(s.execution_window)}</div>`
      : '';
  })();
  const timelineHtml = tlRows
    ? `<div class="sug-section"><div class="sug-section-title">Timeline</div><div class="sug-timeline">${tlRows}</div></div>`
    : '';
  const confHtml = renderConfidenceChecks(s);
  return contextHtml + confHtml + introHtml + entryHtml + timelineHtml + renderExitPlan(s);
}

// Per-transaction (one-sided) charge estimate — each leg = 1 order, no assumed exit.
// Use when you already have a flat list of individual buy/sell transactions
// (e.g. entry_legs + closing_legs combined).
// legs items: { action, fill_price, lots, lot_size }
function estChargesOneSide(legs) {
  if (!legs || !legs.length) return 0;
  const BROKERAGE = 20.0, STT_SELL = 0.0005, EXCHANGE = 0.000530;
  const SEBI = 0.000001, STAMP_BUY = 0.00003, GST = 0.18;
  let brokerage = 0, stt = 0, exchange = 0, sebi = 0, stamp = 0;
  for (const leg of legs) {
    const price   = parseFloat(leg.fill_price || 0);
    const lots    = parseInt(leg.lots || 1);
    const lotSize = parseInt(leg.lot_size || 1);
    const qty     = lots * lotSize;
    if (qty <= 0 || price <= 0) continue;
    const turnover = price * qty;
    brokerage += BROKERAGE;
    exchange  += EXCHANGE * turnover;
    sebi      += SEBI    * turnover;
    if ((leg.action || '').toUpperCase() === 'BUY')  stamp += STAMP_BUY * turnover;
    if ((leg.action || '').toUpperCase() === 'SELL') stt   += STT_SELL  * turnover;
  }
  const gst   = GST * (brokerage + exchange + sebi);
  const total = brokerage + stt + exchange + sebi + stamp + gst;
  return Math.round(total * 100) / 100;
}

// Estimate Zerodha charges from actual executed legs — mirrors engine/charges.py.
// Uses fill_price × (lots_actual || lots) × lot_size for each executed leg.
function estChargesFromLegs(execLegs) {
  if (!execLegs || !execLegs.length) return null;
  const BROKERAGE = 20.0;
  const STT_SELL   = 0.0005;
  const EXCHANGE   = 0.000530;
  const SEBI       = 0.000001;
  const STAMP_BUY  = 0.00003;
  const GST        = 0.18;
  let brokerage = 0, stt = 0, exchange = 0, sebi = 0, stamp = 0;
  for (const leg of execLegs) {
    const price   = parseFloat(leg.fill_price || 0);
    const lots    = parseInt(leg.lots_actual || leg.lots || 1);
    const lotSize = parseInt(leg.lot_size || 1);
    const qty     = lots * lotSize;
    if (qty <= 0 || price <= 0) continue;
    const turnover = price * qty;
    brokerage += 2.0 * BROKERAGE;          // entry + assumed exit
    exchange  += EXCHANGE * turnover * 2.0;
    sebi      += SEBI    * turnover * 2.0;
    if ((leg.action || '').toUpperCase() === 'BUY')  stamp += STAMP_BUY * turnover;
    if ((leg.action || '').toUpperCase() === 'SELL') stt   += STT_SELL  * turnover;
  }
  const gst   = GST * (brokerage + exchange + sebi);
  const total = brokerage + stt + exchange + sebi + stamp + gst;
  return Math.round(total * 100) / 100;
}

// Credit breakdown box — shows per-leg contribution and the net combined credit.
// mode='suggest': uses suggested_price / suggested_price_low / suggested_price_high
// mode='trade':   uses fill_price (actual fills)
function creditBreakdownHtml(legs, mode) {
  if (!legs || !legs.length) return '';
  let netMid = 0, netLow = 0, netHigh = 0;
  let sugNetLow = 0, sugNetHigh = 0;
  const rows = legs.map(l => {
    const price  = mode === 'trade' ? (l.fill_price || 0) : (l.suggested_price || 0);
    const pLow   = mode === 'trade' ? price : (l.suggested_price_low  || price);
    const pHigh  = mode === 'trade' ? price : (l.suggested_price_high || price);
    const sign   = l.action === 'SELL' ? 1 : -1;
    netMid  += sign * price;
    netLow  += sign * (l.action === 'SELL' ? pLow  : pHigh);
    netHigh += sign * (l.action === 'SELL' ? pHigh : pLow);
    // For trade mode: also compute suggested range from suggestion leg data
    if (mode === 'trade') {
      const sLow  = parseFloat(l.suggested_price_low  || l.suggested_price || 0);
      const sHigh = parseFloat(l.suggested_price_high || l.suggested_price || 0);
      sugNetLow  += sign * (l.action === 'SELL' ? sLow  : sHigh);
      sugNetHigh += sign * (l.action === 'SELL' ? sHigh : sLow);
    }
    const color = l.action === 'SELL' ? 'var(--ok)' : 'var(--err)';
    // data-cb-leg / data-cb-action let recalc() update this span live as
    // the user edits leg price inputs — no separate LTP widget needed.
    return `<span class="cb-leg">
      <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'} tag-sm">${escapeHtml(l.action)}</span>
      <span class="cb-leg-name">${escapeHtml(l.option_type||'')} ${l.strike||''}</span>
      <span data-cb-leg="${l.leg_order}" data-cb-action="${escapeHtml(l.action)}" style="color:${color};font-weight:600">${l.action === 'SELL' ? '+' : '\u2212'}\u20b9${fmt(price)}</span>
    </span>`;
  }).join('<span class="cb-sep"> + </span>');
  const rangeText = mode === 'suggest' && Math.abs(netHigh - netLow) > 0.5
    ? ` <span class="cb-range">(acceptable range \u20b9${fmt(Math.min(netLow,netHigh))}\u2013\u20b9${fmt(Math.max(netLow,netHigh))})</span>`
    : '';
  const netColor = netMid >= 0 ? 'var(--ok)' : 'var(--err)';
  const netLabel = netMid >= 0 ? 'Combined credit you receive' : 'Combined debit you pay';
  // Trade mode: show suggested range + whether actual fill was within it
  let tradeCompareHtml = '';
  if (mode === 'trade') {
    const sLo = Math.min(sugNetLow, sugNetHigh);
    const sHi = Math.max(sugNetLow, sugNetHigh);
    const withinRange = netMid >= sLo && netMid <= sHi;
    const aboveRange  = netMid > sHi;
    const rangeLabel  = Math.abs(sHi - sLo) > 0.5
      ? `Suggested range: ₹${fmt(sLo)}–₹${fmt(sHi)}`
      : `Suggested: ₹${fmt(sLo)}`;
    const fillStatus = withinRange
      ? `<span class="cb-fill-status cb-fill-ok">✓ within range</span>`
      : aboveRange
        ? `<span class="cb-fill-status cb-fill-above">↑ above range (favourable)</span>`
        : `<span class="cb-fill-status cb-fill-below">↓ below suggested minimum</span>`;
    tradeCompareHtml = `<div class="cb-trade-compare">${rangeLabel} &nbsp;·&nbsp; ${fillStatus}</div>`;
  }
  return `<div class="credit-breakdown">
    <div class="cb-equation">${rows}
      <span class="cb-sep"> = </span>
      <span class="cb-net" data-cb-net style="color:${netColor}">\u20b9${fmt(Math.abs(netMid))}/unit</span>${rangeText}${mode === 'suggest' ? ' <span class="cb-live-status" data-cb-status></span>' : ''}
    </div>
    <div class="cb-label">${escapeHtml(netLabel)} per unit (1 lot each leg)</div>
    ${tradeCompareHtml}
  </div>`;
}

// Return a coloured spread-group badge when a strategy has both CE and PE legs
// (Iron Condor, Iron Butterfly). Returns '' for one-sided strategies.
function spreadBadge(allLegs, thisLeg) {
  const hasCE = allLegs.some(l => l.option_type === 'CE');
  const hasPE = allLegs.some(l => l.option_type === 'PE');
  if (!hasCE || !hasPE) return '';         // one-sided — no label needed
  if (thisLeg.option_type === 'CE') return '<span class="spread-badge spread-call">Call Spread</span>';
  if (thisLeg.option_type === 'PE') return '<span class="spread-badge spread-put">Put Spread</span>';
  return '';
}

// ── Execution order ─────────────────────────────────────────────────────────
// Compute the safe execution order for a multi-leg strategy.
//
//   ENTRY  rule: BUY hedges (long legs) first, then SELL shorts. This avoids
//                ever holding a naked short between fills.
//   CLOSE  rule: BUY back shorts first (extinguish risk), then SELL longs.
//
//  Strategy-specific overrides (most critical):
//   - JADE_LIZARD has a NAKED short put. On entry build the call spread first
//     (BUY long CE → SELL short CE), then add the naked SELL PE last so the
//     defined-risk side is in place before adding directional risk.
//     On close, BUY-BACK the naked short PE FIRST (highest risk leg).
//
// Returns: Map<leg_order:int, position:int> where position is 1..N execution
// step. For 1-leg strategies returns an empty map (no order needed).
function executionOrder(legs, strategy, mode) {
  const out = new Map();
  if (!legs || legs.length <= 1) return out;
  const isJade = strategy === 'JADE_LIZARD';
  const sorted = [...legs];

  if (mode === 'entry') {
    if (isJade) {
      // BUY CE → SELL CE → SELL PE
      const rank = (l) => {
        if (l.action === 'BUY'  && l.option_type === 'CE') return 0;
        if (l.action === 'SELL' && l.option_type === 'CE') return 1;
        if (l.action === 'SELL' && l.option_type === 'PE') return 2;
        return 3;
      };
      sorted.sort((a, b) => rank(a) - rank(b) || (a.leg_order||0) - (b.leg_order||0));
    } else {
      // BUYs first, SELLs last; stable by leg_order
      sorted.sort((a, b) => {
        const aBuy = a.action === 'BUY' ? 0 : 1;
        const bBuy = b.action === 'BUY' ? 0 : 1;
        if (aBuy !== bBuy) return aBuy - bBuy;
        return (a.leg_order||0) - (b.leg_order||0);
      });
    }
  } else { // 'close'
    if (isJade) {
      // BUY-back naked SELL PE → BUY-back SELL CE → SELL-back BUY CE
      const rank = (l) => {
        if (l.action === 'SELL' && l.option_type === 'PE') return 0;
        if (l.action === 'SELL' && l.option_type === 'CE') return 1;
        if (l.action === 'BUY'  && l.option_type === 'CE') return 2;
        return 3;
      };
      sorted.sort((a, b) => rank(a) - rank(b) || (a.leg_order||0) - (b.leg_order||0));
    } else {
      // SELLs (shorts being bought back) first, BUYs (longs being sold back) last
      sorted.sort((a, b) => {
        const aSell = a.action === 'SELL' ? 0 : 1;
        const bSell = b.action === 'SELL' ? 0 : 1;
        if (aSell !== bSell) return aSell - bSell;
        return (a.leg_order||0) - (b.leg_order||0);
      });
    }
  }
  sorted.forEach((l, i) => out.set(l.leg_order, i + 1));
  return out;
}

// Render the execution-step badge for one leg. mode='entry' or 'close'.
// Returns '' for single-leg strategies (long_call / long_put) where order is moot.
function execStepBadge(legs, leg, strategy, mode) {
  const map = executionOrder(legs, strategy, mode);
  const pos = map.get(leg.leg_order);
  if (!pos) return '';
  const cls = mode === 'close' ? 'exec-step exec-step-close' : 'exec-step exec-step-entry';
  const verb = mode === 'close'
    ? (leg.action === 'SELL' ? 'Buy back' : 'Sell back')
    : leg.action;
  const total = map.size;
  const tip = mode === 'close'
    ? `Close step ${pos} of ${total} \u2014 ${verb} this leg now (close shorts before longs)`
    : `Execution step ${pos} of ${total} \u2014 ${verb} this leg now (acquire hedges before opening shorts)`;
  return `<span class="${cls}" title="${tip}">${pos}</span>`;
}

// Banner shown above the legs list explaining the order rule.
function execOrderBanner(legs, strategy, mode) {
  if (!legs || legs.length <= 1) return '';
  const map = executionOrder(legs, strategy, mode);
  if (!map.size) return '';
  // Build readable sequence: "BUY 23100 PE \u2192 BUY 24900 CE \u2192 SELL 23400 PE \u2192 SELL 24600 CE"
  const ordered = [...legs].sort((a, b) => (map.get(a.leg_order)||99) - (map.get(b.leg_order)||99));
  const seq = ordered.map(l => {
    const verb = mode === 'close'
      ? (l.action === 'SELL' ? 'Buy back' : 'Sell back')
      : l.action;
    const verbClass = (mode === 'close')
      ? (l.action === 'SELL' ? 'tag-ok' : 'tag-err')
      : (l.action === 'SELL' ? 'tag-err' : 'tag-ok');
    const stepCls = mode === 'close' ? 'exec-step exec-step-close' : 'exec-step exec-step-entry';
    return `<span class="exec-seq-item">
      <span class="${stepCls}">${map.get(l.leg_order)}</span>
      <span class="tag ${verbClass} tag-sm">${verb}</span>
      ${l.strike||''} ${escapeHtml(l.option_type||'')}
    </span>`;
  }).join('<span class="exec-seq-arrow">\u2192</span>');
  const heading = mode === 'close'
    ? '\u26a0\ufe0f Close in this order \u2014 buy back short legs FIRST, then sell longs:'
    : '\u26a0\ufe0f Execute in this order \u2014 acquire hedges (BUY) FIRST, then SELL shorts:';
  return `<div class="exec-order-banner exec-order-${mode}">
    <div class="exec-order-heading">${heading}</div>
    <div class="exec-order-seq">${seq}</div>
  </div>`;
}

// ── Strategy rationale ──────────────────────────────────────────────────────
// Small "why this strategy today" + "what makes it better" block.
// Uses actual strikes / BEs / spot from the suggestion object, and parses
// conditions_json for real iv_rank, vix, pcr, trend values from that day.
function parseConditions(s) {
  let raw = s.conditions_json;
  if (!raw) return {};
  if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch { return {}; } }
  // conditions_json for a suggestion = array of {label, passed, detail}
  if (!Array.isArray(raw)) return {};
  const out = {};
  raw.forEach(c => {
    const d = c.detail || '';
    const lbl = (c.label || '').toLowerCase();
    // "IV Rank 72.3 (need >50 or <30)"
    if (lbl.includes('iv rank')) {
      const m = d.match(/IV Rank\s+([\d.]+)/i);
      if (m) out.ivRank = parseFloat(m[1]);
    }
    // "VIX regime: STABLE (close 14.20)"
    if (lbl.includes('vix')) {
      const mR = d.match(/VIX regime:\s*(\w+)/i);
      const mC = d.match(/close\s+([\d.]+)/i);
      if (mR) out.vixRegime = mR[1].toUpperCase();
      if (mC) out.vixClose  = parseFloat(mC[1]);
    }
    // "PCR 0.85 (need 0.5–1.5)"
    if (lbl.includes('pcr')) {
      const m = d.match(/PCR\s+([\d.]+)/i);
      if (m) out.pcr = parseFloat(m[1]);
    }
    // "Trend: SIDEWAYS"
    if (lbl.includes('trend')) {
      const m = d.match(/Trend:\s*(\w+)/i);
      if (m) out.trend = m[1].toUpperCase();
    }
    // "DTE 16 (need 7..21)"
    if (lbl.includes('dte')) {
      const m = d.match(/DTE\s+(\d+)/i);
      if (m) out.dte = parseInt(m[1]);
    }
    // "IV/HV ratio 0.91 (IV 17% vs HV-20 19%) — ..."
    if (lbl.includes('iv premium')) {
      const m = d.match(/IV\/HV ratio\s+([\d.]+)/i);
      if (m) out.ivPremium = parseFloat(m[1]);
    }
  });
  return out;
}

function renderStrategyRationale(s) {
  const legs  = s.legs || [];
  const scLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE');
  const spLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'PE');
  const bcLeg = legs.find(l => l.action === 'BUY'  && l.option_type === 'CE');
  const bpLeg = legs.find(l => l.action === 'BUY'  && l.option_type === 'PE');
  const ub    = s.upper_breakeven  != null ? parseFloat(s.upper_breakeven)  : null;
  const lb    = s.lower_breakeven  != null ? parseFloat(s.lower_breakeven)  : null;
  const spot  = s.spot_at_generation != null ? parseFloat(s.spot_at_generation) : null;
  const np    = s.net_credit != null ? parseFloat(s.net_credit) : null;
  const isDebit = np != null && np < 0;
  const debit = isDebit ? Math.abs(np) : 0;
  const pop   = s.probability_of_profit != null ? Math.round(parseFloat(s.probability_of_profit)) : null;
  const dUB   = (ub && spot) ? ((ub - spot) / spot * 100).toFixed(1) : null;
  const dLB   = (lb && spot) ? ((spot - lb) / spot * 100).toFixed(1) : null;

  // Real market context for this day's suggestion
  const ctx = parseConditions(s);
  const ivRank   = ctx.ivRank   ?? null;
  const vixClose = ctx.vixClose ?? null;
  const vixRegime = ctx.vixRegime ?? null;
  const pcr      = ctx.pcr      ?? null;
  const trend    = ctx.trend    ?? null;
  const dte      = ctx.dte      ?? s.dte ?? null;

  // Helpers for readable context phrases
  const ivDesc = ivRank != null
    ? (ivRank > 70 ? `very high (${ivRank.toFixed(0)})` :
       ivRank > 50 ? `elevated (${ivRank.toFixed(0)})` :
       ivRank < 20 ? `very low (${ivRank.toFixed(0)})` :
                     `low (${ivRank.toFixed(0)})`)
    : 'elevated';
  const ivPremium  = ctx.ivPremium ?? null;
  const ivPremDesc = ivPremium != null
    ? (ivPremium >= 1.40 ? `IV/HV ${ivPremium.toFixed(2)} — options significantly overpriced vs realised vol (strong selling edge)`
     : ivPremium >= 1.0  ? `IV/HV ${ivPremium.toFixed(2)} — options moderately priced vs realised vol`
                         : `IV/HV ${ivPremium.toFixed(2)} — options cheaper than realised vol (weaker selling edge)`)
    : (ivRank != null
      ? (ivRank > 50 ? 'options premiums are rich — a good time to be a seller'
                     : 'options are cheap relative to their recent norm')
      : 'options premiums are in a favourable zone');
  const vixDesc = vixClose != null
    ? `VIX at ${vixClose.toFixed(1)} (${(vixRegime||'stable').toLowerCase()})`
    : 'VIX stable';
  const trendDesc = trend != null ? trend.toLowerCase() : 'sideways';
  const pcrDesc = pcr != null
    ? (pcr < 0.6  ? `PCR ${pcr.toFixed(2)} — strong bullish positioning` :
       pcr > 1.4  ? `PCR ${pcr.toFixed(2)} — strong bearish positioning` :
                    `PCR ${pcr.toFixed(2)} — neutral`)
    : null;
  const dteDesc = dte != null ? `with ${dte} DTE` : '';

  const lookup = {
    IRON_CONDOR: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty's trend is ${trendDesc} with no clear directional bias${pcrDesc ? `, and ${pcrDesc}` : ''}. A range-bound strategy collects premium from both sides without needing to pick a direction.`,
      better: `Nifty stays inside the profit zone — above ₹${fmt(lb)}${dLB ? ` (${dLB}% below spot)` : ''} and below ₹${fmt(ub)}${dUB ? ` (${dUB}% above spot)` : ''}${pop ? ` — a ${pop}% probability` : ''}. Theta earns you money every day the index stays still. ${vixDesc} favours time decay.`,
      ideal:  `Nifty drifts sideways ${dteDesc} and expires anywhere inside the zone.`,
    },
    IRON_BUTTERFLY: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty is ${trendDesc}, and a Butterfly concentrates both short strikes at the ATM level (₹${fmt(scLeg?.strike || spLeg?.strike)}) to collect maximum credit. Higher premium than an Iron Condor, but a narrower profit zone.`,
      better: `Nifty pins close to ₹${fmt(scLeg?.strike || spLeg?.strike)} through expiry. IV crush after any event also accelerates profit. ${vixDesc}. Max credit is captured on expiry-at-the-strike.`,
      ideal:  `Nifty closes exactly at the ATM strike ${dteDesc} on expiry.`,
    },
    BULL_PUT_SPREAD: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty's trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. Selling a put spread collects credit with downside risk capped at the spread width — you only lose if Nifty falls hard below ₹${fmt(spLeg?.strike)}.`,
      better: `Nifty rises or stays flat above ₹${fmt(spLeg?.strike)} (the short put). Even a mild pullback is fine as long as it holds above ₹${fmt(lb)}. ${vixDesc}. A rally above spot earns full credit${pop ? ` (${pop}% PoP)` : ''}.`,
      ideal:  `Nifty rises or stays comfortably above ₹${fmt(spLeg?.strike)} ${dteDesc}.`,
    },
    BEAR_CALL_SPREAD: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty's trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. Selling a call spread collects credit with upside risk capped — you only lose if Nifty rallies hard above ₹${fmt(scLeg?.strike)}.`,
      better: `Nifty falls or stays flat below ₹${fmt(scLeg?.strike)} (the short call). Even a small bounce is fine as long as it stays under ₹${fmt(ub)}. ${vixDesc}. A continued decline earns full credit${pop ? ` (${pop}% PoP)` : ''}.`,
      ideal:  `Nifty falls or remains flat, staying below ₹${fmt(scLeg?.strike)} ${dteDesc}.`,
    },
    JADE_LIZARD: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}${pcrDesc ? `. ${pcrDesc.charAt(0).toUpperCase() + pcrDesc.slice(1)}` : ''}. A Jade Lizard (short OTM call spread + short OTM put) generates premium with zero upside risk — the call spread credit exactly offsets the short put's upside exposure.`,
      better: `Nifty rises or stays sideways. No loss on the upside${pop ? ` (${pop}% PoP)` : ''}. Downside risk only appears below ₹${fmt(spLeg?.strike)} minus net credit. ${vixDesc}.`,
      ideal:  `Nifty stays flat or rallies steadily through expiry.`,
    },
    LONG_STRADDLE: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Buying both ATM CE and PE ${dteDesc} at low cost lets you profit from any large directional move, regardless of which way Nifty goes.`,
      better: `A sharp breakout above ₹${fmt(ub)} or breakdown below ₹${fmt(lb)}. ${vixDesc}. Every day Nifty stays flat, the ₹${fmt(debit)}/unit debit decays — the move should come soon.`,
      ideal:  `A surprise event triggers a large Nifty move in either direction before expiry.`,
    },
    LONG_STRANGLE: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Buying OTM CE (₹${fmt(bcLeg?.strike)}) and OTM PE (₹${fmt(bpLeg?.strike)}) costs less than a Straddle but needs a bigger move to profit.`,
      better: `Nifty breaks sharply above ₹${fmt(ub)} or below ₹${fmt(lb)}. ${vixDesc}. Every day without a move, time decay chips away at the ₹${fmt(debit)}/unit paid.`,
      ideal:  `A large gap-and-go move in either direction shortly after entry.`,
    },
    LONG_CALL: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Long Call gives unlimited upside for a defined ₹${fmt(debit)}/unit debit${dte ? ` with ${dte} DTE` : ''}. High leverage, low capital at risk.`,
      better: `Nifty rallies strongly above ₹${fmt(bcLeg?.strike)}. Delta and gamma accelerate profits as Nifty moves higher. ${vixDesc}. Act early — time decay accelerates toward expiry.`,
      ideal:  `Nifty surges upward quickly, well above the call strike.`,
    },
    LONG_PUT: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Long Put profits from a decline while limiting max loss to ₹${fmt(debit)}/unit${dte ? ` with ${dte} DTE` : ''}.`,
      better: `Nifty falls sharply below ₹${fmt(bpLeg?.strike)}. ${vixDesc}. Avoid holding too close to expiry if the move hasn't materialised — theta decay accelerates.`,
      ideal:  `Nifty breaks down sharply before expiry.`,
    },
    BULL_CALL_SPREAD: {
      why:    `IV Rank is ${ivDesc} — not cheap enough for naked long calls, yet not rich enough for pure credit writing. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Bull Call Spread caps the debit while allowing upside to ₹${fmt(scLeg?.strike)}.`,
      better: `Nifty rises above ₹${fmt(scLeg?.strike)} by expiry — the full spread width is earned. ${vixDesc}. A flat or falling Nifty loses the debit paid.`,
      ideal:  `Nifty climbs steadily to or above ₹${fmt(scLeg?.strike)} ${dteDesc}.`,
    },
    BEAR_PUT_SPREAD: {
      why:    `IV Rank is ${ivDesc} — debit spreads work better than naked longs or pure credit writing here. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Bear Put Spread profits from a decline to ₹${fmt(spLeg?.strike)} while capping the debit.`,
      better: `Nifty falls below ₹${fmt(spLeg?.strike)} by expiry — full spread width is earned. ${vixDesc}. A flat or rising Nifty loses the debit paid.`,
      ideal:  `Nifty drifts or falls to or below ₹${fmt(spLeg?.strike)} ${dteDesc}.`,
    },
  };

  const info = lookup[s.strategy];
  if (!info) return '';

  return `<div class="strategy-rationale">
    <div class="sr-row">
      <span class="sr-label">Why this strategy today</span>
      <span class="sr-text">${info.why}</span>
    </div>
    <div class="sr-row">
      <span class="sr-label">What makes it better</span>
      <span class="sr-text">${info.better}</span>
    </div>
    <div class="sr-row sr-ideal">
      <span class="sr-label">Ideal scenario</span>
      <span class="sr-text">${info.ideal}</span>
    </div>
  </div>`;
}

// Derive a readable leg role description from strategy name + leg action/type.
// This is computed at render time so it's always consistent regardless of what
// text was stored in leg_purpose_note at suggestion creation.
function legRoleNote(strategy, leg) {
  const action = (leg.action || '').toUpperCase();
  const ot     = (leg.option_type || '').toUpperCase();
  const sell = action === 'SELL', buy = action === 'BUY';
  const ce = ot === 'CE', pe = ot === 'PE';
  switch (strategy) {
    case 'IRON_CONDOR':
      if (sell && pe) return 'Iron condor — short put, collects premium below expected move';
      if (buy  && pe) return 'Iron condor — long put hedge, caps downside risk';
      if (sell && ce) return 'Iron condor — short call, collects premium above expected move';
      if (buy  && ce) return 'Iron condor — long call hedge, caps upside risk';
      break;
    case 'IRON_BUTTERFLY':
      if (sell && pe) return 'Iron butterfly — short ATM put (body), maximum premium zone';
      if (buy  && pe) return 'Iron butterfly — long OTM put hedge, caps downside risk';
      if (sell && ce) return 'Iron butterfly — short ATM call (body), maximum premium zone';
      if (buy  && ce) return 'Iron butterfly — long OTM call hedge, caps upside risk';
      break;
    case 'BULL_PUT_SPREAD':
      if (sell && pe) return 'Bull put spread — short put, primary premium leg';
      if (buy  && pe) return 'Bull put spread — long put hedge, defines max loss';
      break;
    case 'BEAR_CALL_SPREAD':
      if (sell && ce) return 'Bear call spread — short call, primary premium leg';
      if (buy  && ce) return 'Bear call spread — long call hedge, defines max loss';
      break;
    case 'BULL_CALL_SPREAD':
      if (buy  && ce) return 'Bull call spread — long call, bullish debit leg';
      if (sell && ce) return 'Bull call spread — short call, caps upside, reduces cost';
      break;
    case 'BEAR_PUT_SPREAD':
      if (buy  && pe) return 'Bear put spread — long put, bearish debit leg';
      if (sell && pe) return 'Bear put spread — short put, caps downside profit, reduces cost';
      break;
    case 'JADE_LIZARD':
      if (sell && pe) return 'Jade lizard — short OTM put, bullish premium';
      if (sell && ce) return 'Jade lizard — short OTM call, premium leg of upside spread';
      if (buy  && ce) return 'Jade lizard — long call hedge, caps upside risk';
      break;
    case 'LONG_STRADDLE':
      if (buy && ce) return 'Long straddle — long ATM call, profits on upside breakout';
      if (buy && pe) return 'Long straddle — long ATM put, profits on downside breakdown';
      break;
    case 'LONG_STRANGLE':
      if (buy && ce) return 'Long strangle — long OTM call, profits on upside breakout';
      if (buy && pe) return 'Long strangle — long OTM put, profits on downside breakdown';
      break;
    case 'LONG_CALL':
      return 'Long call — directional bullish, unlimited upside';
    case 'LONG_PUT':
      return 'Long put — directional bearish, defined max loss = premium';
  }
  return leg.leg_purpose_note || '';
}

// Build the per-card suggestion render output.
// readOnly=true: static view used inside trade cards (no inputs, no action buttons)
function renderSuggestion(s, readOnly = false, allSuggestions = []) {
  const isNoSug = s.strategy === 'NONE' || s.status === 'NO_SUGGESTION';
  if (isNoSug) {
    return `<div class="card">
      <div class="card-head">
        <h3>${escapeHtml(s.underlying)} — No suggestion</h3>
        <span class="tag tag-warn">SKIPPED</span>
      </div>
      <p class="muted">${escapeHtml(s.no_suggestion_reason || '')}</p>
      <p class="muted" style="font-size:.85rem">Confidence: ${s.confidence_score}/7</p>
    </div>`;
  }
  const econ = {
    np: s.net_credit, mp: s.max_profit, ml: s.max_loss,
    pop: s.probability_of_profit,
    ub: s.upper_breakeven, lb: s.lower_breakeven,
    sl: s.stop_loss_level,
    chg: s.estimated_charges_total, npnl: s.estimated_net_pnl,
  };
  // Base quantity (from suggestion) used as denominator when user changes lots.
  // Per-unit numbers (np, breakevens, sl, pop) are independent of lot count;
  // absolute-rupee numbers (mp, ml, chg, npnl, total credit) scale linearly.
  // Position size = one leg's lots × lot_size (all legs in a spread share the same qty).
  // Do NOT sum across all legs — a 4-leg IC has 1 lot worth of exposure, not 4.
  const baseQty = ((s.legs || [])[0]?.lots || 1) * ((s.legs || [])[0]?.lot_size || 1);
  // Spread width (in rupees, summed over baseQty). Stays constant when fill
  // prices move — only the credit/debit allocation between profit & loss
  // shifts. We use this to recompute max-loss live as user edits prices.
  const baseWidthTotal = (econ.mp || 0) + (econ.ml || 0);
  // Suggested credit range used by the live credit monitor widget
  let _sugLo = 0, _sugHi = 0;
  (s.legs || []).forEach(l => {
    const sign = l.action === 'SELL' ? 1 : -1;
    const pLo  = parseFloat(l.suggested_price_low  || l.suggested_price || 0);
    const pHi  = parseFloat(l.suggested_price_high || l.suggested_price || 0);
    _sugLo += sign * (l.action === 'SELL' ? pLo : pHi);
    _sugHi += sign * (l.action === 'SELL' ? pHi : pLo);
  });
  const sugRangeLo = Math.min(_sugLo, _sugHi);
  const sugRangeHi = Math.max(_sugLo, _sugHi);
  // Recompute net_credit from leg midpoints — overrides the stored value which may
  // be stale (e.g. seed data). This keeps Net credit and Acceptable credit consistent.
  if (s.legs && s.legs.length) {
    let _npMid = 0;
    s.legs.forEach(l => {
      _npMid += (l.action === 'SELL' ? 1 : -1) * parseFloat(l.suggested_price || 0);
    });
    econ.np   = Math.round(_npMid * 100) / 100;
    s.net_credit = econ.np;  // keep renderExitPlan in sync
  }
  const baseTotalCredit = (econ.np || 0) * baseQty;
  const legsHtml = (s.legs || []).map(l => {
    const legTotal = (l.lots || 0) * (l.lot_size || 0) * (l.suggested_price || 0);
    // Threshold hint: SELL needs price >= low (to retain credit), BUY needs
    // price <= high (to keep debit small). Target close ≈ 50% of entry =
    // 50% premium capture, which is the standard profit-target heuristic.
    const thresholdHint = l.action === 'SELL'
      ? `<span class="leg-threshold ok">Sell ≥ ₹${fmt(l.suggested_price_low)}</span>`
      : `<span class="leg-threshold warn">Buy ≤ ₹${fmt(l.suggested_price_high)}</span>`;
    const targetClose = (l.suggested_price || 0) * 0.5;
    const closeHint = l.action === 'SELL'
      ? `<span class="leg-target-close">Target close: buy back @ ~₹<span class="target-close-val" data-leg-order="${l.leg_order}">${fmt(targetClose)}</span> (50% capture)</span>`
      : `<span class="leg-target-close">Target close: sell back @ ~₹<span class="target-close-val" data-leg-order="${l.leg_order}">${fmt(targetClose)}</span> (50% capture)</span>`;
    const legMetaHtml = readOnly
      ? `<span class="muted">${l.lots || 1} lot${(l.lots||1)!==1?'s':''} × ${l.lot_size} @ ₹${fmt(l.suggested_price)} = <strong>₹${fmt(legTotal)}</strong></span>
         <span class="leg-price-range muted">(range ₹${fmt(l.suggested_price_low)}–₹${fmt(l.suggested_price_high)})</span>`
      : `<input type="number" class="leg-lots" min="1" value="${l.lots || 1}"
                 data-lot-size="${l.lot_size}" data-leg-order="${l.leg_order}"
                 data-price="${l.suggested_price}"
                 data-orig-lots="${l.lots || 1}">×
          lot ${l.lot_size} @ ₹<span class="leg-price-shown" data-leg-order="${l.leg_order}">${fmt(l.suggested_price)}</span> =
          <strong><span class="leg-total" data-leg-order="${l.leg_order}">₹${fmt(legTotal)}</span></strong>
          <span class="leg-price-range muted">(range ₹${fmt(l.suggested_price_low)}–₹${fmt(l.suggested_price_high)})</span>`;
    const fillColHtml = readOnly ? '' : `
      <label class="leg-fill">
        <input type="checkbox" data-leg="${l.leg_order}" class="leg-exec" checked>
        <input type="number" step="0.05" data-leg-price="${l.leg_order}"
               value="${l.suggested_price}" style="width:90px">
      </label>`;
    return `
    <div class="leg-row action-${l.action}" data-leg-action="${l.action}">
      <div class="leg-action-col">
        ${execStepBadge(s.legs, l, s.strategy, 'entry')}
        <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'}">${l.action}</span>
        ${spreadBadge(s.legs, l)}
      </div>
      <div>
        <div><strong>${escapeHtml(l.symbol)} ${escapeHtml(l.expiry_date || '')} ${l.strike} ${l.option_type}</strong></div>
        <div class="leg-meta">${legMetaHtml}</div>
        <div class="leg-hints">${thresholdHint} · ${closeHint}</div>
        <div class="muted" style="font-size:.8rem">${escapeHtml(legRoleNote(s.strategy, l))}</div>
      </div>${fillColHtml}
    </div>`;
  }).join('');
  // attach live lot-count recalc after DOM insert — see bindSuggestionActions
  const innerHtml = `
    <div class="card-head">
      <h3>${escapeHtml(s.trade_name || s.suggestion_id)}</h3>
      <span class="tag tag-accent">${escapeHtml(s.strategy || '')}</span>
    </div>
    <div class="card-id-row">
      <span class="id-chip" title="Suggestion ID">SID&nbsp;${escapeHtml(s.suggestion_id || '—')}</span>
    </div>
    ${renderStrategyRationale(s)}
    ${renderPlainEnglishStructured(s)}
    <div class="kv-grid">
      ${s.generated_on ? `<div><span class="k">Suggested on</span><br><span class="v">${fmtDate(s.generated_on)}</span></div>` : ''}
      ${s.expiry_date  ? `<div><span class="k">Options expiry</span><br><span class="v">${fmtDate(s.expiry_date)}</span></div>` : ''}
      <div><span class="k">Net credit (per unit)</span><br><span class="v econ-np">₹${fmt(econ.np)}</span></div>
      <div><span class="k">Total credit <span class="econ-qty-hint muted" style="font-size:.75rem">(×${baseQty})</span></span><br><span class="v econ-tot-credit">₹${fmt(baseTotalCredit)}</span></div>
      <div><span class="k">Max profit</span><br><span class="v econ-mp">₹${fmt(econ.mp)}</span></div>
      <div><span class="k">Max loss</span><br><span class="v econ-ml">₹${fmt(econ.ml)}<span class="econ-ml-hint">${pctHint(econ.ml, econ.np, 'credit')}</span></span></div>
      <div><span class="k">PoP</span><br><span class="v">${fmtPct(econ.pop)}</span></div>
      ${econ.ub != null ? `<div><span class="k">Upper BE</span><br><span class="v econ-ub">₹${fmt(econ.ub)}${spotDist(econ.ub, s.spot_at_generation)}</span></div>` : ''}
      ${econ.lb != null ? `<div><span class="k">Lower BE</span><br><span class="v econ-lb">₹${fmt(econ.lb)}${spotDist(econ.lb, s.spot_at_generation)}</span></div>` : ''}
      ${(() => {
        const twoSided = ['IRON_CONDOR', 'IRON_BUTTERFLY'].includes(s.strategy);
        if (!twoSided || econ.sl == null) {
          return `<div><span class="k">Stop loss</span><br><span class="v">₹${fmt(econ.sl)}${spotDist(econ.sl, s.spot_at_generation)}</span></div>`;
        }
        const shortCallLeg = (s.legs || []).find(l => l.action === 'SELL' && l.option_type === 'CE');
        const shortPutLeg  = (s.legs || []).find(l => l.action === 'SELL' && l.option_type === 'PE');
        const upperSl = econ.sl;
        const slBuffer = shortCallLeg ? upperSl - shortCallLeg.strike : 0;
        const lowerSl  = shortPutLeg  ? shortPutLeg.strike - slBuffer : null;
        return `
          <div class="sl-two-sided">
            <span class="k">Stop loss triggers <span class="muted" style="font-size:.7rem">(independent — close only breached spread)</span></span>
            <div class="sl-two-rows">
              <div class="sl-trigger-row">
                <span class="sl-dir-badge sl-dir-up">▲ Nifty rises above</span>
                <span class="v">₹${fmt(upperSl)}${spotDist(upperSl, s.spot_at_generation)}</span>
                <span class="sl-action-hint">→ close call spread (legs ${shortCallLeg ? shortCallLeg.leg_order : '?'}+${shortCallLeg ? shortCallLeg.leg_order + 1 : '?'})</span>
              </div>
              <div class="sl-trigger-row">
                <span class="sl-dir-badge sl-dir-dn">▼ Nifty falls below</span>
                <span class="v">₹${fmt(lowerSl)}${spotDist(lowerSl, s.spot_at_generation)}</span>
                <span class="sl-action-hint">→ close put spread (legs ${shortPutLeg ? shortPutLeg.leg_order : '?'}+${shortPutLeg ? shortPutLeg.leg_order + 1 : '?'})</span>
              </div>
            </div>
          </div>`;
      })()}
      <div><span class="k">Premium SL <span class="muted" style="font-size:.72rem">(1.5× credit)</span></span><br><span class="v econ-psl">₹${fmt((econ.np||0) * baseQty * 1.5)}</span></div>
      <div><span class="k">Est. charges</span><br><span class="v econ-chg">₹${fmt(econ.chg)}</span></div>
      <div><span class="k">Est. net P&amp;L</span><br><span class="v econ-npnl">₹${fmt(econ.npnl)}</span></div>
      <div><span class="k">DTE</span><br><span class="v">${s.dte ?? '—'}</span></div>
    </div>
    ${execOrderBanner(s.legs, s.strategy, 'entry')}
    <div class="legs-grid">${legsHtml}</div>
    ${creditBreakdownHtml(s.legs, 'suggest')}
    ${readOnly ? '' : `
    <div class="exec-spot-bar">
      <div class="sl-monitor-label" style="margin-bottom:6px">Nifty spot at execution</div>
      <div class="exec-spot-row">
        <div class="sl-field">
          <label class="sl-label">Your actual Nifty spot <span class="muted" style="font-size:.7rem">(used ₹${fmt(s.spot_at_generation)})</span></label>
          <input type="number" step="1" class="sl-input exec-spot-input"
                 placeholder="e.g. ${Math.round(s.spot_at_generation || 0)}">
        </div>
        <div class="sl-field">
          <label class="sl-label">Adjusted SL level</label>
          <span class="sl-prem-val exec-adj-sl">₹${fmt(econ.sl)}</span>
          <span class="muted exec-adj-note" style="font-size:.72rem">(suggested, fill spot to adjust)</span>
        </div>
      </div>
    </div>
    <div class="btn-row" style="margin-top:12px">
      <button class="btn btn-accent btn-mark-exec">Mark Executed</button>
      <button class="btn btn-ghost btn-ignore">Ignore</button>
    </div>`}`;

  if (readOnly) {
    return `<details class="orig-sug-details" style="margin-top:10px">
      <summary class="orig-sug-summary">📋 Original suggestion</summary>
      <div class="orig-sug-body">${innerHtml}</div>
    </details>`;
  }
  return `<div class="card"
    data-sug-id="${escapeHtml(s.suggestion_id)}"
    data-base-qty="${baseQty}"
    data-base-np="${econ.np || 0}"
    data-base-mp="${econ.mp || 0}"
    data-base-ml="${econ.ml || 0}"
    data-base-chg="${econ.chg || 0}"
    data-base-npnl="${econ.npnl || 0}"
    data-base-tot-credit="${baseTotalCredit}"
    data-base-width-total="${baseWidthTotal}"
    data-base-sl="${econ.sl || 0}"
    data-spot-at-gen="${s.spot_at_generation || 0}"
    data-sug-range-lo="${sugRangeLo}"
    data-sug-range-hi="${sugRangeHi}"
    data-short-call-strike="${((s.legs||[]).find(l=>l.action==='SELL'&&l.option_type==='CE')||{}).strike||''}"
    data-short-put-strike="${((s.legs||[]).find(l=>l.action==='SELL'&&l.option_type==='PE')||{}).strike||''}"
  >${innerHtml}</div>`;
}

function bindSuggestionActions() {
  // Live recalc on every card. Triggers on:
  //   * leg-lots input  → quantity changes (scales rupee totals)
  //   * data-leg-price  → actual fill price changes (shifts net credit;
  //                       width is constant so max-loss moves opposite)
  $$('.card[data-sug-id]').forEach(card => {
    const recalc = () => {
      const baseQty   = parseFloat(card.dataset.baseQty)        || 1;
      const baseNp    = parseFloat(card.dataset.baseNp)         || 0;
      const baseMp    = parseFloat(card.dataset.baseMp)         || 0;
      const baseChg   = parseFloat(card.dataset.baseChg)        || 0;
      const baseWidth = parseFloat(card.dataset.baseWidthTotal) || 0;

      // 1. Compute live per-unit net credit from leg actions + price inputs
      let liveCreditPerUnit = 0;
      let curQty = 0;
      card.querySelectorAll('.leg-row').forEach(row => {
        const action  = row.dataset.legAction;
        const lotsIn  = row.querySelector('.leg-lots');
        const priceIn = row.querySelector('input[data-leg-price]');
        const lots    = parseInt(lotsIn?.value)    || 0;
        const lotSize = parseFloat(lotsIn?.dataset.lotSize) || 0;
        const price   = parseFloat(priceIn?.value) || 0;
        const lo      = lotsIn?.dataset.legOrder;
        const qty     = lots * lotSize;
        curQty += qty;
        // SELL collects premium (+), BUY pays premium (−)
        liveCreditPerUnit += (action === 'SELL' ? 1 : -1) * price;
        // Update per-leg row total + price echo + target close (50% capture)
        const tot = card.querySelector(`.leg-total[data-leg-order="${lo}"]`);
        if (tot) tot.textContent = `₹${fmt(qty * price)}`;
        const shown = card.querySelector(`.leg-price-shown[data-leg-order="${lo}"]`);
        if (shown) shown.textContent = fmt(price);
        const tc = card.querySelector(`.target-close-val[data-leg-order="${lo}"]`);
        if (tc) tc.textContent = fmt(price * 0.5);
      });
      // curQty was summed across all N legs; divide by leg count to get
      // the position quantity (1 lot × lot_size), not N × position quantity.
      const numLegs = card.querySelectorAll('.leg-row').length || 1;
      curQty = Math.round(curQty / numLegs);
      const ratioQty = curQty / baseQty;
      const liveTotalCredit = liveCreditPerUnit * curQty;
      // For credit spreads max profit ≈ total credit. For non-credit strats
      // we fall back to scaling the original max profit by qty only.
      const isCreditStrat = baseWidth > 0 && baseNp !== 0;
      const liveMp = isCreditStrat ? liveTotalCredit
                                   : baseMp * ratioQty;
      // Width stays constant in rupees per unit qty, so total scales by qty.
      const liveMl = isCreditStrat
        ? Math.max(0, baseWidth * ratioQty - liveMp)
        : baseMp * 0;  // not applicable for non-credit; leave as base*ratio below
      const liveChg  = baseChg * ratioQty;
      const liveNpnl = liveMp - liveChg;

      const setText = (sel, txt) => {
        const el = card.querySelector(sel);
        if (el) el.textContent = txt;
      };
      setText('.econ-np',         `₹${fmt(liveCreditPerUnit)}`);
      setText('.econ-tot-credit', `₹${fmt(liveTotalCredit)}`);
      setText('.econ-mp',         `₹${fmt(liveMp)}`);
      if (isCreditStrat) setText('.econ-ml', `₹${fmt(liveMl)}`);
      setText('.econ-chg',        `₹${fmt(liveChg)}`);
      setText('.econ-npnl',       `₹${fmt(liveNpnl)}`);
      setText('.econ-psl',        `\u20b9${fmt(liveTotalCredit * 1.5)}`);
      const qtyHint = card.querySelector('.econ-qty-hint');
      if (qtyHint) qtyHint.textContent = `(\u00d7${curQty})`;
      // Update credit breakdown equation spans live
      card.querySelectorAll('[data-cb-leg]').forEach(span => {
        const lo     = span.dataset.cbLeg;
        const action = span.dataset.cbAction;
        const priceIn = card.querySelector(`input[data-leg-price="${lo}"]`);
        const p = parseFloat(priceIn?.value) || 0;
        span.textContent = `${action === 'SELL' ? '+' : '\u2212'}\u20b9${fmt(p)}`;
        span.style.color = action === 'SELL' ? 'var(--ok)' : 'var(--err)';
      });
      const cbNet = card.querySelector('[data-cb-net]');
      if (cbNet) {
        cbNet.textContent = `\u20b9${fmt(Math.abs(liveCreditPerUnit))}/unit`;
        cbNet.style.color = liveCreditPerUnit >= 0 ? 'var(--ok)' : 'var(--err)';
      }
      const cbStatus = card.querySelector('[data-cb-status]');
      if (cbStatus) {
        const rangeLo = parseFloat(card.dataset.sugRangeLo) || 0;
        const rangeHi = parseFloat(card.dataset.sugRangeHi) || 0;
        if (rangeHi > rangeLo + 0.5) {
          const within = liveCreditPerUnit >= rangeLo && liveCreditPerUnit <= rangeHi;
          const above  = liveCreditPerUnit > rangeHi;
          cbStatus.textContent = within || above ? '\u2713 good to execute' : '\u2193 below minimum \u2014 wait';
          cbStatus.className = 'cb-live-status ' + (within || above ? 'cb-status-ok' : 'cb-status-warn');
        }
      }
      // Update Upper/Lower BE live from short strikes + live credit
      const scStrike = parseFloat(card.dataset.shortCallStrike);
      const spStrike = parseFloat(card.dataset.shortPutStrike);
      const ubEl = card.querySelector('.econ-ub');
      const lbEl = card.querySelector('.econ-lb');
      if (ubEl && !isNaN(scStrike)) ubEl.textContent = '\u20b9' + fmt(scStrike + liveCreditPerUnit);
      if (lbEl && !isNaN(spStrike)) lbEl.textContent = '\u20b9' + fmt(spStrike - liveCreditPerUnit);
    };
    card.addEventListener('input', e => {
      const inp = e.target;
      if (inp.classList.contains('leg-lots') ||
          inp.hasAttribute('data-leg-price')) {
        recalc();
      }
      if (inp.classList.contains('exec-spot-input')) {
        const spot = parseFloat(inp.value);
        const sugSl   = parseFloat(card.dataset.baseSl)    || 0;
        const sugSpot = parseFloat(card.dataset.spotAtGen) || 0;
        const adjSlEl  = card.querySelector('.exec-adj-sl');
        const noteEl   = card.querySelector('.exec-adj-note');
        if (!isNaN(spot) && spot > 0 && sugSl > 0) {
          const delta = spot - sugSpot;
          adjSlEl.textContent = `\u20b9${fmt(sugSl + delta)}`;
          noteEl.textContent = delta === 0
            ? '(no change)'
            : `(${delta > 0 ? '+' : ''}${fmt(delta)} from AI level)`;
        } else {
          adjSlEl.textContent = `\u20b9${fmt(sugSl)}`;
          noteEl.textContent = '(suggested, fill spot to adjust)';
        }
      }
    });
  });

  $$('.btn-mark-exec').forEach(b => b.addEventListener('click', async (e) => {
    const btn  = e.currentTarget;
    const card = btn.closest('.card');
    const sid  = card.dataset.sugId;

    // ── Lot-count parity validation ──────────────────────────────────────────
    const execLots = $$('.leg-row', card)
      .filter(row => row.querySelector('.leg-exec')?.checked)
      .map(row => parseInt(row.querySelector('.leg-lots')?.value || 1))
      .filter(n => !isNaN(n));
    const uniqueLots = [...new Set(execLots)];
    if (uniqueLots.length > 1) {
      toast(`All legs must use the same lot count — found ${uniqueLots.join(' & ')} lots. Fix before proceeding.`, 'err');
      return;
    }
    const numLots = uniqueLots[0] || 1;

    // ── Nifty spot at execution — required ───────────────────────────────────
    const spotInput = card.querySelector('.exec-spot-input');
    const spotRaw   = spotInput?.value.trim();
    const spotVal   = spotRaw ? parseFloat(spotRaw) : null;
    if (!spotVal || isNaN(spotVal) || spotVal <= 0) {
      if (spotInput) { spotInput.classList.add('input-error'); spotInput.focus(); }
      toast('Enter the Nifty spot price at execution before proceeding.', 'err');
      return;
    }
    if (spotInput) spotInput.classList.remove('input-error');

    // ── 2-step confirm ────────────────────────────────────────────────────────
    if (!btn.dataset.confirmed) {
      btn.dataset.confirmed = '1';
      btn.textContent = `Confirm execution · ${numLots} lot${numLots !== 1 ? 's' : ''}?`;
      btn.classList.add('btn-confirm-pending');
      const cancelBtn = document.createElement('button');
      cancelBtn.className = 'btn btn-ghost btn-confirm-cancel';
      cancelBtn.textContent = 'Cancel';
      cancelBtn.addEventListener('click', () => {
        btn.dataset.confirmed = '';
        btn.textContent = 'Mark Executed';
        btn.classList.remove('btn-confirm-pending');
        cancelBtn.remove();
      });
      btn.insertAdjacentElement('afterend', cancelBtn);
      return;
    }
    // Clear confirm state before submitting
    btn.dataset.confirmed = '';
    btn.textContent = 'Mark Executed';
    btn.classList.remove('btn-confirm-pending');
    btn.nextElementSibling?.classList.contains('btn-confirm-cancel') && btn.nextElementSibling.remove();

    const fills = $$('.leg-row', card).map(row => {
      const lotsInput = row.querySelector('.leg-lots');
      const lo = row.querySelector('.leg-exec').dataset.leg;
      const exec = row.querySelector('.leg-exec').checked;
      const price = parseFloat(row.querySelector('input[type="number"][data-leg-price]').value);
      const lotsOverride = lotsInput ? parseInt(lotsInput.value) : null;
      return {leg_order: parseInt(lo), executed: exec,
              fill_price: exec ? price : null,
              fill_time: new Date().toISOString(),
              lots_override: lotsOverride};
    });
    const sugSl   = parseFloat(card.dataset.baseSl)    || 0;
    const sugSpot = parseFloat(card.dataset.spotAtGen) || 0;
    const adjSl = (spotVal != null && !isNaN(spotVal) && spotVal > 0 && sugSl > 0)
      ? sugSl + (spotVal - sugSpot) : null;
    try {
      const r = await API(`/api/suggestion/${sid}/mark-executed`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          fills,
          spot_at_execution: spotVal,
          actual_stop_loss_level: adjSl,
        }),
      });
      toast(r.trade_id ? `Trade created: ${r.trade_id}` : 'Suggestion ignored', 'info');
      loadSuggestion(); loadTrades();
    } catch (err) { toast(err.message, 'err'); }
  }));
  $$('.btn-ignore').forEach(b => b.addEventListener('click', async e => {
    const sid = e.target.closest('.card').dataset.sugId;
    try {
      await API(`/api/suggestion/${sid}/mark-executed`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({fills: []}),
      });
      toast('Suggestion ignored', 'warn');
      loadSuggestion();
    } catch (err) { toast(err.message, 'err'); }
  }));

  // Confidence chip click → toggle breakdown panel
  bindConfChips();
}

function bindConfChips() {
  $$('.conf-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const sid = chip.dataset.sugId;
      // Scope lookup to nearest card/details container to avoid duplicate-id
      // collisions when both the Suggestion tab and Trades tab are in the DOM.
      const container = chip.closest('.card, .orig-sug-details') || document;
      const panel = container.querySelector(`[id="conf-${CSS.escape(sid)}"]`)
                 || document.getElementById(`conf-${sid}`);
      if (!panel) return;
      const hidden = panel.hidden;
      panel.hidden = !hidden;
      const arrow = chip.querySelector('span');
      if (arrow) arrow.textContent = hidden ? '\u25b2' : '\u25bc';
    });
  });
}

// ---------------- Tab 2: My Trades ----------------
async function loadTrades() {
  const c = $('#trades-container');
  c.className = 'loading'; c.textContent = 'Loading…';
  try {
    const data = await API('/api/trades/open');
    if (!data.trades.length) {
      c.className=''; c.innerHTML = '<div class="empty">No open trades.</div>';
      return;
    }
    c.className=''; c.innerHTML = data.trades.map(renderTrade).join('');
    bindConfChips();
    $$('.btn-resuggest').forEach(b => b.addEventListener('click', async e => {
      const id = e.target.dataset.tradeId;
      try {
        const r = await API(`/api/trades/${id}/resuggest`, {method:'POST'});
        toast(r.inserted ? 'Resuggestion generated' : 'Already exists', 'info');
      } catch (err) { toast(err.message, 'err'); }
    }));
    $$('.btn-complete-trade').forEach(b => b.addEventListener('click', e => {
      openSupplementForm(e.target.dataset.tradeId);
    }));
    $$('.btn-close-trade').forEach(b => b.addEventListener('click', e => {
      openCloseForm(e.target.dataset.tradeId, parseFloat(e.target.dataset.netCredit) || 0);
    }));
    $$('.btn-void-trade').forEach(b => b.addEventListener('click', async e => {
      const id = e.target.dataset.tradeId;
      const card = e.target.closest('.card');
      const name = card?.querySelector('h3')?.textContent?.trim() || id;
      if (!confirm(`Void trade "${name}"?\n\nThis marks the trade as VOID and removes it from your active trades. The record is kept for audit purposes.`)) return;
      try {
        await API(`/api/trades/${id}`, {method: 'DELETE'});
        toast(`Trade "${name}" voided`, 'warn');
        loadTrades();
      } catch (err) { toast(err.message, 'err'); }
    }));
  } catch (e) {
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

async function openSupplementForm(tradeId) {
  const panel = document.getElementById(`supp-${tradeId}`);
  if (!panel) return;
  panel.hidden = false;
  panel.innerHTML = '<div class="muted">Loading remaining legs…</div>';
  try {
    const data = await API(`/api/trades/${tradeId}/remaining-legs`);
    if (!data.legs.length) {
      panel.innerHTML = '<div class="muted">All legs already filled.</div>'; return;
    }
    const suppStrategy = (data.legs[0] && data.legs[0].strategy) || '';
    const legsHtml = data.legs.map(l => `
      <div class="leg-row action-${escapeHtml(l.action)}" data-leg-order="${l.leg_order}">
        ${execStepBadge(data.legs, l, suppStrategy, 'entry')}
        <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'}">${escapeHtml(l.action)}</span>
        <div>
          <div><strong>${escapeHtml(l.symbol)} ${l.strike} ${escapeHtml(l.option_type)}</strong></div>
          <div class="leg-meta">
            <input type="number" class="leg-lots" min="1" value="${l.lots || 1}"
                   data-lot-size="${l.lot_size}" data-leg-order="${l.leg_order}"
                   data-price="${l.suggested_price}">×
            lot ${l.lot_size} @ ₹${fmt(l.suggested_price)}
          </div>
          <div class="muted" style="font-size:.8rem">${escapeHtml(legRoleNote(l.strategy, l))}</div>
        </div>
        <label class="leg-fill">
          <input type="checkbox" class="supp-exec" data-leg="${l.leg_order}" checked>
          <input type="number" step="0.05" class="supp-price" data-leg-price="${l.leg_order}"
                 value="${l.suggested_price}" style="width:90px">
        </label>
      </div>`).join('');
    panel.innerHTML = `
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid #2a3744">
        <div class="muted" style="font-size:.8rem;margin-bottom:8px">Fill remaining legs:</div>
        ${execOrderBanner(data.legs, suppStrategy, 'entry')}
        ${legsHtml}
        <div class="btn-row" style="margin-top:8px">
          <button class="btn btn-accent btn-supp-submit" data-trade-id="${escapeHtml(tradeId)}">Confirm fills</button>
          <button class="btn btn-ghost btn-supp-cancel">Cancel</button>
        </div>
      </div>`;
    panel.querySelector('.btn-supp-submit').addEventListener('click', () =>
      submitSupplement(tradeId, panel));
    panel.querySelector('.btn-supp-cancel').addEventListener('click', () => {
      panel.hidden = true;
    });
  } catch (err) {
    panel.innerHTML = `<div class="muted">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function submitSupplement(tradeId, panel) {
  const btn = panel.querySelector('.btn-supp-submit');

  // ── Lot-count parity validation ──────────────────────────────────────────
  const execLots = $$('.leg-row[data-leg-order]', panel)
    .filter(row => row.querySelector('.supp-exec')?.checked)
    .map(row => parseInt(row.querySelector('.leg-lots')?.value || 1))
    .filter(n => !isNaN(n));
  const uniqueLots = [...new Set(execLots)];
  if (uniqueLots.length > 1) {
    toast(`All legs must use the same lot count — found ${uniqueLots.join(' & ')} lots. Fix before proceeding.`, 'err');
    return;
  }
  const numLots = uniqueLots[0] || 1;

  // ── 2-step confirm ────────────────────────────────────────────────────────
  if (!btn.dataset.confirmed) {
    btn.dataset.confirmed = '1';
    btn.textContent = `Confirm fills · ${numLots} lot${numLots !== 1 ? 's' : ''}?`;
    btn.classList.add('btn-confirm-pending');
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost btn-confirm-cancel';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => {
      btn.dataset.confirmed = '';
      btn.textContent = 'Confirm fills';
      btn.classList.remove('btn-confirm-pending');
      cancelBtn.remove();
    });
    btn.insertAdjacentElement('afterend', cancelBtn);
    return;
  }
  // Clear confirm state before submitting
  btn.dataset.confirmed = '';
  btn.textContent = 'Confirm fills';
  btn.classList.remove('btn-confirm-pending');
  btn.nextElementSibling?.classList.contains('btn-confirm-cancel') && btn.nextElementSibling.remove();

  const fills = $$('.leg-row[data-leg-order]', panel).map(row => {
    const lo = parseInt(row.dataset.legOrder);
    const exec = row.querySelector('.supp-exec').checked;
    const price = parseFloat(row.querySelector('.supp-price').value);
    const lotsInput = row.querySelector('.leg-lots');
    const lotsOverride = lotsInput ? parseInt(lotsInput.value) : null;
    return {leg_order: lo, executed: exec,
            fill_price: exec ? price : null,
            fill_time: new Date().toISOString(),
            lots_override: lotsOverride};
  });
  try {
    await API(`/api/trades/${tradeId}/supplement`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({fills}),
    });
    toast('Trade supplemented successfully', 'info');
    loadTrades();
  } catch (err) { toast(err.message, 'err'); }
}
async function openCloseForm(tradeId, netCreditActual = 0) {
  const panel = document.getElementById(`close-${tradeId}`);
  if (!panel) return;
  panel.hidden = false;
  panel.innerHTML = '<div class="muted">Loading legs…</div>';
  try {
    const [data, sugg] = await Promise.all([
      API(`/api/trades/${tradeId}/executed-legs`),
      API(`/api/trades/${tradeId}/close-suggestion`).catch(() => ({legs: [], est_gross_pnl: 0})),
    ]);
    if (!data.legs.length) {
      panel.innerHTML = '<div class="muted">No executed legs found.</div>'; return;
    }
    const suggMap = {};
    (sugg.legs || []).forEach(s => { suggMap[s.leg_order] = s.suggested_close; });
    const closeStrategy = (data.legs[0] && data.legs[0].strategy) || '';
    const legsHtml = data.legs.map(l => {
      const closeAction = l.action === 'SELL' ? 'Buy back' : 'Sell back';
      const lotsUsed = l.lots_actual || l.lots || 1;
      const lotSize = l.lot_size || 1;
      const sx = suggMap[l.leg_order];
      const prefill = (l.exit_price != null) ? l.exit_price
                    : (sx != null && sx > 0 ? sx : '');
      const hint = (sx != null && sx > 0)
        ? `<span class="muted" style="font-size:.72rem">Suggested \u20b9${fmt(sx)}</span>`
        : '';
      return `
        <div class="leg-exit-row" data-leg-order="${l.leg_order}"
             data-action="${escapeHtml(l.action)}"
             data-fill-price="${l.fill_price || 0}"
             data-lots="${lotsUsed}"
             data-lot-size="${lotSize}">
          <div class="leg-exit-head">
            ${execStepBadge(data.legs, l, closeStrategy, 'close')}
            <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'}">${escapeHtml(l.action)}</span>
            <strong>${escapeHtml(l.symbol)} ${l.strike} ${escapeHtml(l.option_type)}</strong>
            <span class="muted" style="font-size:.8rem">Entry \u20b9${fmt(l.fill_price)} \u00d7 ${lotsUsed} lots</span>
          </div>
          <div class="leg-exit-input">
            <span class="muted" style="font-size:.8rem">${escapeHtml(closeAction)} @ \u20b9</span>
            <input type="number" step="0.05" class="close-price" data-leg="${l.leg_order}"
                   value="${prefill}" placeholder="0.00">
            ${hint}
          </div>
        </div>`;
    }).join('');
    panel.innerHTML = `
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid #2a3744">
        <div class="close-step-banner">
          <span class="step-badge step-1">Step 1</span> Adjust prices below &rarr; see live P&amp;L &rarr; go to broker
          &nbsp;&nbsp;
          <span class="step-badge step-2">Step 2</span> Return &rarr; enter actual fills &rarr; <strong>Confirm</strong>
        </div>
        ${execOrderBanner(data.legs, closeStrategy, 'close')}
        <div class="leg-exit-grid">${legsHtml}</div>
        
        <div class="live-pnl-preview" id="live-pnl-${escapeHtml(tradeId)}">
          <div>Gross P&amp;L: <strong class="live-pnl-gross">—</strong></div>
          <div class="muted" style="font-size:.85rem">Est. charges (entry+exit): <strong class="live-pnl-charges">—</strong></div>
          <div>Net P&amp;L: <strong class="live-pnl-value">—</strong><span class="live-pnl-pct muted"></span></div>
        </div>
        <div class="btn-row" style="margin-top:8px">
          <button class="btn btn-close-trade btn-close-submit" data-trade-id="${escapeHtml(tradeId)}">Confirm close &amp; record fills</button>
          <button class="btn btn-ghost btn-close-cancel">Cancel</button>
        </div>
      </div>`;
    function recalcClosePnl() {
      let grossPnl = 0; let allFilled = true;
      const entryTxns = [], exitTxns = [];
      panel.querySelectorAll('.leg-exit-row').forEach(row => {
        const action = row.dataset.action;
        const entryPrice = parseFloat(row.dataset.fillPrice) || 0;
        const lots = parseInt(row.dataset.lots) || 1;
        const lotSize = parseInt(row.dataset.lotSize) || 1;
        const closeInput = row.querySelector('.close-price');
        const closePrice = parseFloat(closeInput?.value);
        if (isNaN(closePrice) || closePrice <= 0) { allFilled = false; return; }
        const legPnl = action === 'SELL'
          ? (entryPrice - closePrice) * lots * lotSize
          : (closePrice - entryPrice) * lots * lotSize;
        grossPnl += legPnl;
        entryTxns.push({ action, fill_price: entryPrice, lots, lot_size: lotSize });
        exitTxns.push({ action: action === 'SELL' ? 'BUY' : 'SELL', fill_price: closePrice, lots, lot_size: lotSize });
      });
      const grossEl   = panel.querySelector('.live-pnl-gross');
      const chargesEl = panel.querySelector('.live-pnl-charges');
      const el        = panel.querySelector('.live-pnl-value');
      const pctEl     = panel.querySelector('.live-pnl-pct');
      if (!el) return;
      if (!allFilled) {
        if (grossEl)   { grossEl.textContent = '—';   grossEl.className   = 'live-pnl-gross'; }
        if (chargesEl) { chargesEl.textContent = '—'; chargesEl.className = 'live-pnl-charges'; }
        el.textContent = '—'; el.className = 'live-pnl-value';
        if (pctEl) { pctEl.textContent = ''; }
        return;
      }
      const charges = estChargesOneSide([...entryTxns, ...exitTxns]);
      const netPnl  = grossPnl - charges;
      if (grossEl) {
        grossEl.textContent = `₹${fmt(grossPnl)}`;
        grossEl.className   = 'live-pnl-gross ' + (grossPnl >= 0 ? 'pnl-pos' : 'pnl-neg');
      }
      if (chargesEl) {
        chargesEl.textContent = `₹${fmt(charges)}`;
        chargesEl.className   = 'live-pnl-charges';
      }
      // net_credit_actual is already total ₹ (price × lots × lot_size from DB)
      // — do NOT multiply by qty again
      const totalCredit = netCreditActual;
      const pctStr = totalCredit > 0
        ? ` (${netPnl >= 0 ? '+' : ''}${(netPnl / totalCredit * 100).toFixed(0)}% of credit)`
        : '';
      el.textContent = `₹${fmt(netPnl)}`;
      el.className = 'live-pnl-value ' + (netPnl >= 0 ? 'pnl-pos' : 'pnl-neg');
      if (pctEl) { pctEl.textContent = pctStr; pctEl.className = 'live-pnl-pct ' + (netPnl >= 0 ? 'pnl-pos' : 'pnl-neg'); }
    }
    panel.querySelectorAll('.close-price').forEach(inp => inp.addEventListener('input', recalcClosePnl));
    recalcClosePnl(); // init with prefilled values
    panel.querySelector('.btn-close-submit').addEventListener('click', () =>
      submitClose(tradeId, panel));
    panel.querySelector('.btn-close-cancel').addEventListener('click', () => {
      panel.hidden = true;
    });
  } catch (err) {
    panel.innerHTML = `<div class="muted">Error: ${escapeHtml(err.message)}</div>`;
  }
}
async function submitClose(tradeId, panel) {
  const exits = $$('.leg-exit-row[data-leg-order]', panel).map(row => {
    const lo = parseInt(row.dataset.legOrder);
    const price = row.querySelector('.close-price').value;
    return {
      leg_order: lo,
      exit_price: price !== '' ? parseFloat(price) : null,
      exit_time: new Date().toISOString(),
    };
  }).filter(e => e.exit_price != null);
  if (!exits.length) {
    toast('Enter at least one exit price', 'warn'); return;
  }

  // ── 2-step confirm ────────────────────────────────────────────────────────
  const btn = panel.querySelector('.btn-close-submit');
  if (!btn.dataset.confirmed) {
    const pnlEl = panel.querySelector('.live-pnl-value');
    const pnlText = pnlEl && pnlEl.textContent !== '—' ? ` · Net P&L ${pnlEl.textContent}` : '';
    btn.dataset.confirmed = '1';
    btn.textContent = `Really close${pnlText}?`;
    btn.classList.add('btn-confirm-pending');
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost btn-confirm-cancel';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => {
      btn.dataset.confirmed = '';
      btn.textContent = 'Confirm close & record fills';
      btn.classList.remove('btn-confirm-pending');
      cancelBtn.remove();
    });
    btn.insertAdjacentElement('afterend', cancelBtn);
    return;
  }
  // Clear confirm state before submitting
  btn.dataset.confirmed = '';
  btn.textContent = 'Confirm close & record fills';
  btn.classList.remove('btn-confirm-pending');
  btn.nextElementSibling?.classList.contains('btn-confirm-cancel') && btn.nextElementSibling.remove();

  try {
    await API(`/api/trades/${tradeId}/close`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({exits}),
    });
    toast('Trade closed \u2014 P&L recorded', 'info');
    loadTrades();
  } catch (err) { toast(err.message, 'err'); }
}
// Derive a contextual next-action note for an unexecuted leg
// based on the leg's role and the overall execution state.
function legNextAction(leg, allLegs) {
  const action   = (leg.action || '').toUpperCase();
  const optType  = (leg.option_type || '').toUpperCase();
  const purpose  = leg.leg_purpose_note || '';
  const execCount = allLegs.filter(l => l.executed).length;
  const totalCount = allLegs.length;

  // Is any matching short already in the position?
  const hasExecutedSell = allLegs.some(l => l.executed && (l.action || '').toUpperCase() === 'SELL');

  let action_note = '';
  if (action === 'SELL') {
    action_note = 'Sell to collect premium';
  } else if (action === 'BUY' && hasExecutedSell) {
    // Hedge for an already-open short — more urgent
    action_note = 'Buy hedge urgently — open short is unprotected';
  } else {
    action_note = 'Buy to complete the spread';
  }

  // Append the purpose note if it adds context beyond the action note
  const extra = purpose && !action_note.toLowerCase().includes(purpose.toLowerCase().slice(0, 10))
    ? ` · ${purpose}` : '';
  return `${action_note}${extra}`;
}

// Collapsible original-suggestion panel shown inside each open trade card
function renderOriginalSuggestion(s) {
  if (!s) return '';
  return renderSuggestion(s, true);
}

function renderTrade(t) {
  const broken = t.broken_state_json ? JSON.parse(t.broken_state_json) : null;
  const brokenHtml = broken && broken.options && broken.options.length ? `
    <div style="margin-top:10px">
      <div class="muted" style="font-size:.85rem;margin-bottom:6px">
        Broken state: <strong>${escapeHtml(broken.state)}</strong></div>
      ${broken.options.map(o => `
        <div class="leg-row" style="grid-template-columns:auto 1fr">
          <span class="tag ${o.recommended ? 'tag-accent' : 'tag-info'}">#${o.rank}</span>
          <div>
            <strong>${escapeHtml(o.label)}</strong>
            <div class="muted" style="font-size:.85rem">${escapeHtml(o.when_to_use)}</div>
            <div class="muted" style="font-size:.8rem">${escapeHtml(o.zerodha_steps)}</div>
          </div>
        </div>`).join('')}
    </div>` : '';

  const legs = t.legs || [];
  const isPartial = t.position_type && t.position_type !== 'FULL_VALID';
  const hasLegDetails = legs.length > 0 && legs[0].symbol != null;
  const executedLegs = legs.filter(l => l.executed);
  const hasExecutedLegs = executedLegs.length > 0;
  const hasPendingClose = hasExecutedLegs && executedLegs.every(l => !l.exit_price);

  let legsHtml = '';
  if (hasLegDetails && legs.length) {
    // Build target-exit summary for open executed legs
    const openExecLegs = legs.filter(l => l.executed && !l.exit_price);
    let targetSummaryHtml = '';
    // Strategy-specific profit target %: Iron Butterfly exits earlier (25%) due to narrow wings
    const _tradeStrategy = (t.suggestion && t.suggestion.strategy) || '';
    const tradePct      = _tradeStrategy === 'IRON_BUTTERFLY' ? 0.25 : 0.50;
    const tradePctLabel = _tradeStrategy === 'IRON_BUTTERFLY' ? '25%' : '50%';
    if (openExecLegs.length > 0) {
      const netCreditActual = t.net_credit_actual || 0;
      const totalQty = openExecLegs.reduce((a, l) => a + ((l.lots_actual || l.lots || 1) * (l.lot_size || 1)), 0) || 1;
      // Per-unit net credit = sum of (SELL fills) - sum of (BUY fills), averaged over legs
      let perUnitCredit = 0;
      openExecLegs.forEach(l => {
        perUnitCredit += (l.action === 'SELL' ? 1 : -1) * (l.fill_price || 0);
      });
      const targetPct = perUnitCredit * tradePct * totalQty;
      const targetRows = openExecLegs.map(l => {
        const tc = (l.fill_price || 0) * tradePct;
        const lotsUsed = l.lots_actual || l.lots || 1;
        const lotSize = l.lot_size || 1;
        const qty = lotsUsed * lotSize;
        const closeVerb = l.action === 'SELL' ? 'Buy back' : 'Sell back';
        const sign = l.action === 'SELL' ? '\u2264' : '\u2265';
        return `<div class="target-row">
          <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'} tag-sm">${escapeHtml(l.action||'')}</span>
          <span><strong>${escapeHtml(l.symbol||'')} ${l.strike||''} ${escapeHtml(l.option_type||'')}</strong></span>
          <span>${closeVerb} ${sign} <strong>\u20b9${fmt(tc)}</strong> <span class="muted">(${tradePctLabel} of \u20b9${fmt(l.fill_price)} entry \u00d7 ${qty}u)</span></span>
        </div>`;
      }).join('');
      targetSummaryHtml = `<div class="target-exit-box">
        <div class="target-exit-title">\u{1F3AF} Target exit (${tradePctLabel} profit capture)</div>
        ${targetRows}
        <div class="target-exit-keep">Keep ~\u20b9${fmt(targetPct)} of the \u20b9${fmt(netCreditActual * totalQty)} total credit received</div>
      </div>`;
    }
    legsHtml = `<div class="trade-legs-section">
      ${targetSummaryHtml}
      ${(() => {
        // Close-order banner shown above legs when there are open executed legs.
        const openExec = legs.filter(l => l.executed && !l.exit_price);
        const tradeStrategy2 = (t.suggestion && t.suggestion.strategy) || '';
        return openExec.length > 1 ? execOrderBanner(openExec, tradeStrategy2, 'close') : '';
      })()}
      ${(() => {
        // Entry-order banner when there are still pending (un-executed) legs to fill.
        const pending = legs.filter(l => !l.executed);
        const tradeStrategy3 = (t.suggestion && t.suggestion.strategy) || '';
        return pending.length > 1 ? execOrderBanner(pending, tradeStrategy3, 'entry') : '';
      })()}
      <div class="trade-legs-grid">${(() => {
        const tradeStrategy = (t.suggestion && t.suggestion.strategy) || '';
        const openExec = legs.filter(l => l.executed && !l.exit_price);
        const pending  = legs.filter(l => !l.executed);
        return legs.map(l => {
        const done = !!l.executed;
        const lotsUsed = l.lots_actual || l.lots || 0;
        const tag = `<span class="tag ${(l.action||'') === 'SELL' ? 'tag-err' : 'tag-ok'}">${escapeHtml(l.action||'')}</span>`;
        const instrument = `${escapeHtml(l.symbol||'')} ${l.strike||''} ${escapeHtml(l.option_type||'')}`;
        if (done && l.exit_price != null) {
          const pnlClass = l.leg_pnl != null ? (l.leg_pnl >= 0 ? 'pnl-profit' : 'pnl-loss') : '';
          return `<div class="trade-leg-row leg-done leg-exited action-${l.action}">
            <div class="leg-action-col">${tag}${spreadBadge(legs, l)}</div>
            <div class="tl-info">
              <span class="tl-instrument">${instrument}</span>
              <div class="muted" style="font-size:.8rem">${escapeHtml(legRoleNote(l.strategy, l))}</div>
              <span class="leg-status-done">\u2713 Filled @ \u20b9${fmt(l.fill_price)} \u00b7 ${lotsUsed} lot${lotsUsed !== 1 ? 's' : ''}</span>
              <span class="leg-exit-info">\u21b3 Closed @ \u20b9${fmt(l.exit_price)}${l.leg_pnl != null ? (() => { const legBase = (l.fill_price||0)*(l.lots_actual||l.lots||1)*(l.lot_size||1); const legPct = legBase > 0 ? ` (${l.leg_pnl >= 0 ? '+' : ''}${(l.leg_pnl/legBase*100).toFixed(0)}%)` : ''; return ` &nbsp;<span class="${pnlClass}">P&L: \u20b9${fmt(l.leg_pnl)}${legPct}</span>`; })() : ''}</span>
            </div>
          </div>`;
        } else if (done) {
          const targetClose = (l.fill_price || 0) * tradePct;
          const closeHint = l.action === 'SELL'
            ? `<span class="leg-target-close">Target buy back \u2264 \u20b9${fmt(targetClose)} <span class="muted" style="font-size:.72rem">(${tradePctLabel} of \u20b9${fmt(l.fill_price)} entry)</span></span>`
            : `<span class="leg-target-close">Target sell back \u2265 \u20b9${fmt(targetClose)} <span class="muted" style="font-size:.72rem">(${tradePctLabel} of \u20b9${fmt(l.fill_price)} entry)</span></span>`;
          return `<div class="trade-leg-row leg-done action-${l.action}">
            <div class="leg-action-col">${execStepBadge(openExec, l, tradeStrategy, 'close')}${tag}${spreadBadge(legs, l)}</div>
            <div class="tl-info">
              <span class="tl-instrument">${instrument}</span>
              <div class="muted" style="font-size:.8rem">${escapeHtml(legRoleNote(l.strategy, l))}</div>
              <span class="leg-status-done">\u2713 Filled</span>
              <span class="tl-fill">@ \u20b9${fmt(l.fill_price)} \u00b7 ${lotsUsed} lot${lotsUsed !== 1 ? 's' : ''}</span>
              ${closeHint}
            </div>
          </div>`;
        } else {
          const note = legNextAction(l, legs);
          return `<div class="trade-leg-row leg-pending">
            <div class="leg-action-col">${execStepBadge(pending, l, tradeStrategy, 'entry')}${tag}${spreadBadge(legs, l)}</div>
            <div class="tl-info">
              <span class="tl-instrument">${instrument}</span>
              <span class="leg-status-pending">\u23f3 Pending</span>
              <span class="leg-next-action">${escapeHtml(note)}</span>
            </div>
          </div>`;
        }
      }).join('');
      })()}</div>
      ${creditBreakdownHtml(legs.filter(l => l.executed), 'trade')}
    </div>`;
  }

  return `<div class="card">
    <div class="card-head">
      <h3>${escapeHtml(t.trade_name || t.trade_id)}</h3>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        ${hasPendingClose ? `<span class="tag tag-warn" title="Record exit prices to compute P&L">CLOSE PENDING</span>` : ''}
        <span class="tag tag-${t.daily_status === 'EXIT_AT_OPEN' ? 'warn' : 'ok'}">
          ${escapeHtml(t.daily_status || t.status)}</span>
      </div>
    </div>
    <div class="card-id-row">
      <span class="id-chip" title="Trade ID">TID&nbsp;${escapeHtml(t.trade_id || '—')}</span>
      ${t.suggestion_id ? `<span class="id-chip" title="Suggestion ID">SID&nbsp;${escapeHtml(t.suggestion_id)}</span>` : ''}
    </div>
    <div class="kv-grid">
      ${(() => {
        // Compute BEs from actual fill prices — more accurate than DB-stored values
        // which were copied from the suggestion at execution time.
        const scLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE' && l.fill_price != null);
        const spLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'PE' && l.fill_price != null);
        let fillNetCredit = 0;
        legs.filter(l => l.executed && l.fill_price != null).forEach(l => {
          fillNetCredit += (l.action === 'SELL' ? 1 : -1) * parseFloat(l.fill_price);
        });
        const realUBE = scLeg ? parseFloat(scLeg.strike) + fillNetCredit : null;
        const realLBE = spLeg ? parseFloat(spLeg.strike) - fillNetCredit : null;
        // Resolve max profit/loss — prefer trade-stored actuals, fall back to suggestion
        const estMp = t.actual_max_profit != null ? t.actual_max_profit
                    : (t.suggestion && t.suggestion.max_profit != null ? t.suggestion.max_profit : null);
        const estMl = t.actual_max_loss   != null ? t.actual_max_loss
                    : (t.suggestion && t.suggestion.max_loss   != null ? t.suggestion.max_loss   : null);
        const estPop = t.suggestion && t.suggestion.probability_of_profit != null
                     ? t.suggestion.probability_of_profit : null;
        const estDte = t.suggestion && t.suggestion.dte != null ? t.suggestion.dte : null;

        // Grid positions mirror suggestion tab exactly (3-col on wide screen):
        // Pos  1: Entry date       ≈ Suggested on        (col 1)
        // Pos  2: Options expiry   = Options expiry       (col 2) ✓
        // Pos  3: Net credit       ≈ Net credit/unit      (col 3) ✓
        // Pos  4: Type             ≈ Total credit         (col 1, row 2)
        // Pos  5: Est. max profit  = Max profit           (col 2) ✓
        // Pos  6: Est. max loss    = Max loss             (col 3) ✓
        // Pos  7: Est. PoP         = PoP                  (col 1) ✓
        // Pos  8: Upper BE (fills) ≈ Upper BE             (col 2) ✓
        // Pos  9: Lower BE (fills) ≈ Stop loss / Lower BE (col 3) ✓
        // Pos 10: P&L              ≈ Premium SL           (col 1, row 4)
        // Pos 11: Est. charges     = Est. charges         (col 2) ✓
        // Pos 12: Est. net P&L     = Est. net P&L         (col 3) ✓
        // Pos 13: DTE at entry     = DTE                  (col 1) ✓
        // Pos 14: Status           (col 2)
        // Pos 15: Exit date (if closed, col 3)
        const execWithFills = legs.filter(l => l.executed && l.fill_price != null);
        const estChg = execWithFills.length > 0
          ? estChargesFromLegs(execWithFills)
          : (t.suggestion && t.suggestion.estimated_charges_total != null ? t.suggestion.estimated_charges_total : null);
        const estNetPnl = (estMp != null && estChg != null) ? (estMp - estChg) : null;
        return `
      <div><span class="k">Entry date</span><br><span class="v">${fmtDt(t.executed_on)}</span></div>
      ${t.suggestion && t.suggestion.expiry_date ? `<div><span class="k">Options expiry</span><br><span class="v">${fmtDate(t.suggestion.expiry_date)}</span></div>` : '<div></div>'}
      <div><span class="k">Net credit (actual)</span><br><span class="v">₹${fmt(t.net_credit_actual)}</span></div>
      <div><span class="k">Type</span><br><span class="v">${escapeHtml(t.position_type)}</span></div>
      ${estMp != null ? `<div><span class="k">Est. max profit</span><br><span class="v pnl-profit">₹${fmt(estMp)}</span></div>` : '<div></div>'}
      ${estMl != null ? `<div><span class="k">Est. max loss</span><br><span class="v pnl-loss">₹${fmt(estMl)}<span class="econ-ml-hint">${pctHint(estMl, t.net_credit_actual, 'credit')}</span></span></div>` : '<div></div>'}
      ${estPop != null ? `<div><span class="k">Est. PoP</span><br><span class="v">${fmtPct(estPop)}</span></div>` : '<div></div>'}
      ${realUBE != null ? `<div><span class="k">Upper BE <span class="muted" style="font-size:.7rem">(from fills)</span></span><br><span class="v">₹${fmt(realUBE)}</span></div>` : '<div></div>'}
      ${realLBE != null ? `<div><span class="k">Lower BE <span class="muted" style="font-size:.7rem">(from fills)</span></span><br><span class="v">₹${fmt(realLBE)}</span></div>` : '<div></div>'}
      <div><span class="k">P&amp;L</span><br><span class="v">₹${fmt(t.net_pnl)}${pctHint(t.net_pnl, t.net_credit_actual, 'credit')}</span></div>
      ${estChg    != null ? `<div><span class="k">Est. charges <span class="muted" style="font-size:.7rem">(from fills)</span></span><br><span class="v">₹${fmt(estChg)}</span></div>` : '<div></div>'}
      ${estNetPnl != null ? `<div><span class="k">Est. net P&amp;L</span><br><span class="v ${estNetPnl >= 0 ? 'pnl-profit' : 'pnl-loss'}">₹${fmt(estNetPnl)}</span></div>` : '<div></div>'}
      ${estDte != null ? `<div><span class="k">DTE at entry</span><br><span class="v">${estDte}</span></div>` : '<div></div>'}
      <div><span class="k">Status</span><br><span class="v">${escapeHtml(t.status)}</span></div>
      ${t.closed_on ? `<div><span class="k">Exit date</span><br><span class="v">${fmtDt(t.closed_on)}</span></div>` : ''}`;
      })()}
    </div>
    ${(() => {
      const shortCallLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE');
      const shortPutLeg  = legs.find(l => l.action === 'SELL' && l.option_type === 'PE');
      const ul = (t.suggestion && t.suggestion.underlying) || '';
      const spot = t.spot_at_execution != null ? parseFloat(t.spot_at_execution) : null;
      // Compute actual net credit per unit from fill prices
      const execLegs = legs.filter(l => l.executed && l.fill_price != null);
      let actualNetCredit = 0;
      execLegs.forEach(l => { actualNetCredit += (l.action === 'SELL' ? 1 : -1) * parseFloat(l.fill_price || 0); });
      // Compute real BEs from fills
      const realUpperBE = shortCallLeg ? parseFloat(shortCallLeg.strike) + actualNetCredit : null;
      const realLowerBE = shortPutLeg  ? parseFloat(shortPutLeg.strike)  - actualNetCredit : null;
      const beHtml = (realUpperBE != null || realLowerBE != null) ? (() => {
        const parts = [];
        if (realLowerBE != null) parts.push(`Lower BE <strong>\u20b9${fmt(realLowerBE)}</strong>`);
        if (realUpperBE != null) parts.push(`Upper BE <strong>\u20b9${fmt(realUpperBE)}</strong>`);
        const spotBelowUpperBE = spot != null && realUpperBE != null && spot < realUpperBE;
        const spotAboveLowerBE = spot != null && realLowerBE != null && spot > realLowerBE;
        const safeAtEntry = (!realLowerBE || spotAboveLowerBE) && (!realUpperBE || spotBelowUpperBE);
        const beStatus = spot != null
          ? `<span class="pz-spot ${safeAtEntry ? 'pz-inside' : 'pz-outside'}">${safeAtEntry ? '\u2713 spot inside BEs at entry' : '\u26a0 spot outside BEs at entry'}</span>`
          : '';
        return `<div class="pz-be-row">\u{1F4CF} Actual BEs (from fills): ${parts.join(' \u00b7 ')}${beStatus ? ' &nbsp;\u00b7&nbsp; ' + beStatus : ''}</div>`;
      })() : '';
      if (shortCallLeg && shortPutLeg) {
        const pzLow  = parseFloat(shortPutLeg.strike);
        const pzHigh = parseFloat(shortCallLeg.strike);
        const inside = spot != null && spot >= pzLow && spot <= pzHigh;
        const spotTag = spot != null
          ? `<span class="pz-spot ${inside ? 'pz-inside' : 'pz-outside'}">Spot at entry \u20b9${fmt(spot)} ${inside ? '\u2713 inside zone' : '\u26a0 outside zone'}</span>` : '';
        return `<div class="profit-zone-bar">\u{1F3AF} Max profit if ${escapeHtml(ul)} stays <strong>\u20b9${fmt(pzLow)} \u2013 \u20b9${fmt(pzHigh)}</strong>${spotTag ? ' &nbsp;\u00b7&nbsp; ' + spotTag : ''}${beHtml}</div>`;
      } else if (shortCallLeg) {
        const pzHigh = parseFloat(shortCallLeg.strike);
        const inside = spot != null && spot <= pzHigh;
        const spotTag = spot != null ? `<span class="pz-spot ${inside ? 'pz-inside' : 'pz-outside'}">Spot \u20b9${fmt(spot)} ${inside ? '\u2713 below strike' : '\u26a0 above strike'}</span>` : '';
        return `<div class="profit-zone-bar">\u{1F3AF} Max profit if ${escapeHtml(ul)} stays below <strong>\u20b9${fmt(pzHigh)}</strong>${spotTag ? ' &nbsp;\u00b7&nbsp; ' + spotTag : ''}${beHtml}</div>`;
      } else if (shortPutLeg) {
        const pzLow = parseFloat(shortPutLeg.strike);
        const inside = spot != null && spot >= pzLow;
        const spotTag = spot != null ? `<span class="pz-spot ${inside ? 'pz-inside' : 'pz-outside'}">Spot \u20b9${fmt(spot)} ${inside ? '\u2713 above strike' : '\u26a0 below strike'}</span>` : '';
        return `<div class="profit-zone-bar">\u{1F3AF} Max profit if ${escapeHtml(ul)} stays above <strong>\u20b9${fmt(pzLow)}</strong>${spotTag ? ' &nbsp;\u00b7&nbsp; ' + spotTag : ''}${beHtml}</div>`;
      }
      return '';
    })()}
    <div class="sl-monitor-section">
      <div class="sl-monitor-label">Stop-loss monitor</div>
      <div class="sl-monitor-grid">
        ${(() => {
          const twoSided = t.suggestion && ['IRON_CONDOR', 'IRON_BUTTERFLY'].includes(t.suggestion.strategy);
          if (twoSided && t.actual_stop_loss_level != null) {
            const legs = (t.suggestion.legs || []);
            const shortCallLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE');
            const shortPutLeg  = legs.find(l => l.action === 'SELL' && l.option_type === 'PE');
            const upperSl = t.actual_stop_loss_level;
            const slBuffer = shortCallLeg ? upperSl - shortCallLeg.strike : 0;
            const lowerSl  = shortPutLeg  ? shortPutLeg.strike - slBuffer : null;
            return `<div class="sl-field sl-two-sided" style="grid-column:1/-1">
              <label class="sl-label">SL triggers <span class="muted" style="font-size:.7rem">(independent — close only the breached spread)</span></label>
              <div class="sl-two-rows" style="margin-top:6px">
                <div class="sl-trigger-row">
                  <span class="sl-dir-badge sl-dir-up">▲ rises above</span>
                  <span class="sl-prem-val">₹${fmt(upperSl)}</span>
                  <span class="sl-action-hint">→ close call spread (legs ${shortCallLeg ? shortCallLeg.leg_order : '?'}+${shortCallLeg ? shortCallLeg.leg_order + 1 : '?'})</span>
                </div>
                <div class="sl-trigger-row">
                  <span class="sl-dir-badge sl-dir-dn">▼ falls below</span>
                  <span class="sl-prem-val">₹${fmt(lowerSl)}</span>
                  <span class="sl-action-hint">→ close put spread (legs ${shortPutLeg ? shortPutLeg.leg_order : '?'}+${shortPutLeg ? shortPutLeg.leg_order + 1 : '?'})</span>
                </div>
              </div>
            </div>`;
          }
          return `<div class="sl-field">
            <label class="sl-label">Nifty SL level</label>
            <span class="sl-prem-val">${t.actual_stop_loss_level != null ? `\u20b9${fmt(t.actual_stop_loss_level)}` : '\u2014 not set'}</span>
          </div>`;
        })()}
        <div class="sl-field">
          <label class="sl-label">Spot at entry</label>
          <span class="sl-prem-val">${t.spot_at_execution != null ? `\u20b9${fmt(t.spot_at_execution)}` : '\u2014 not set'}</span>
        </div>
        <div class="sl-field">
          <label class="sl-label">Premium SL <span class="muted" style="font-size:.72rem">(1.5\u00d7\u00a0credit)</span></label>
          <span class="sl-prem-val">\u20b9${fmt((t.net_credit_actual || 0) * 1.5)}</span>
          <span class="muted sl-prem-note">exit if MTM loss exceeds this</span>
        </div>
      </div>
      <div class="sl-action-note">
        <strong>MTM loss</strong> = (current buy-back cost of all short legs) \u2212 net credit received<br>
        Exit if <strong>MTM loss \u2265 Premium SL</strong> or Nifty spot hits the SL level \u2014 use <strong>Close Trade</strong> below.
      </div>
    </div>
    ${hasPendingClose ? `<div class="pending-close-alert">\u26a0 Exit fills not recorded \u2014 use Close Trade below to compute P&amp;L</div>` : ''}
    ${legsHtml}
    ${t.exit_instruction ? `<p class="muted" style="margin:8px 0 0">Exit: ${escapeHtml(t.exit_instruction)}</p>` : ''}
    ${brokenHtml}
    ${renderOriginalSuggestion(t.suggestion)}
    <div class="btn-row" style="margin-top:10px">
      <button class="btn btn-ghost btn-resuggest" data-trade-id="${escapeHtml(t.trade_id)}">
        Generate resuggestion</button>
      ${isPartial ? `<button class="btn btn-warn btn-complete-trade" data-trade-id="${escapeHtml(t.trade_id)}">
        Complete Trade</button>` : ''}
      ${hasExecutedLegs ? `<button class="btn btn-close-trade" data-trade-id="${escapeHtml(t.trade_id)}" data-net-credit="${t.net_credit_actual || 0}">
        Close Trade</button>` : ''}
      <button class="btn btn-danger btn-void-trade" data-trade-id="${escapeHtml(t.trade_id)}"
              style="margin-left:auto">Void Trade</button>
    </div>
    ${isPartial ? `<div class="supplement-panel" id="supp-${escapeHtml(t.trade_id)}" hidden></div>` : ''}
    ${hasExecutedLegs ? `<div class="close-trade-panel" id="close-${escapeHtml(t.trade_id)}" hidden></div>` : ''}
  </div>`;
}

// ---------------- Tab 3: History ----------------

// ---- Sub-tab switcher ----
let _histActiveSubtab = 'trades';
document.querySelectorAll('.hist-subtab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.hist-subtab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _histActiveSubtab = btn.dataset.htab;
    $('#hist-pane-trades').hidden      = (_histActiveSubtab !== 'trades');
    $('#hist-pane-suggestions').hidden = (_histActiveSubtab !== 'suggestions');
    if (_histActiveSubtab === 'trades')       loadHistory();
    if (_histActiveSubtab === 'suggestions')  loadHistorySuggestions();
  });
});

function loadHistory() {
  if (_histActiveSubtab !== 'trades') return;
  const c = $('#history-container');
  c.className='loading'; c.textContent='Loading…';

  // Default dates: today and 30 days ago
  const fromEl = $('#hist-from'), toEl = $('#hist-to'), instrEl = $('#hist-instrument');
  if (!fromEl.value) { const d = new Date(); d.setDate(d.getDate()-30); fromEl.value = d.toISOString().slice(0,10); }
  if (!toEl.value)   { toEl.value = new Date().toISOString().slice(0,10); }

  const params = new URLSearchParams();
  params.set('from_date', fromEl.value);
  params.set('to_date', toEl.value);
  if (instrEl.value) params.set('underlying', instrEl.value);

  API('/api/history/closed-trades?' + params).then(data => {
    // Populate instrument dropdown (preserve selection)
    const cur = instrEl.value;
    instrEl.innerHTML = '<option value="">All instruments</option>';
    (data.underlyings || []).forEach(u => {
      const o = document.createElement('option'); o.value = u; o.textContent = u;
      if (u === cur) o.selected = true;
      instrEl.appendChild(o);
    });

    if (!data.trades.length) {
      c.className=''; c.innerHTML='<div class="empty">No closed trades in the selected period.</div>'; return;
    }
    c.className='';
    c.innerHTML = data.trades.map(renderHistoryTrade).join('');
  }).catch(e => {
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  });
}

async function loadHistorySuggestions() {
  if (_histActiveSubtab !== 'suggestions') return;
  const c = $('#hsug-container');
  c.className='loading'; c.textContent='Loading…';

  const fromEl = $('#hsug-from'), toEl = $('#hsug-to');
  const instrEl = $('#hsug-instrument'), statusEl = $('#hsug-status');
  if (!fromEl.value) { const d = new Date(); d.setDate(d.getDate()-30); fromEl.value = d.toISOString().slice(0,10); }
  if (!toEl.value)   { toEl.value = new Date().toISOString().slice(0,10); }

  const params = new URLSearchParams();
  params.set('from_date', fromEl.value);
  params.set('to_date',   toEl.value);
  if (instrEl.value)  params.set('underlying', instrEl.value);
  if (statusEl.value) params.set('status',     statusEl.value);

  try {
    const data = await API('/api/history/suggestions?' + params);

    // Populate instrument dropdown
    const cur = instrEl.value;
    instrEl.innerHTML = '<option value="">All instruments</option>';
    (data.underlyings || []).forEach(u => {
      const o = document.createElement('option'); o.value = u; o.textContent = u;
      if (u === cur) o.selected = true;
      instrEl.appendChild(o);
    });

    if (!data.suggestions.length) {
      c.className=''; c.innerHTML='<div class="empty">No suggestions in the selected period.</div>'; return;
    }
    c.className='';
    c.innerHTML = data.suggestions.map(renderHistorySuggestion).join('');
  } catch (e) {
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderHistorySuggestion(s) {
  const statusCls = s.status === 'EXECUTED' ? 'tag-ok'
                  : s.status === 'IGNORED'  ? 'tag-warn'
                  : s.status === 'PENDING'  ? 'tag-acc'
                  : 'tag-muted';
  return `
  <div class="hist-card" style="border-left-color: var(--accent)">
    <div class="hist-card-head">
      <div class="hist-card-title">
        <strong class="hist-instr">${escapeHtml(s.underlying || '')}</strong>
        <span class="tag tag-accent">${escapeHtml(s.strategy || '')}</span>
        <span class="tag ${statusCls}">${escapeHtml(s.status || '')}</span>
        ${s.expiry_type ? `<span class="muted" style="font-size:.78rem">${escapeHtml(s.expiry_type)}</span>` : ''}
      </div>
      <div class="hist-card-pnl">
        ${s.confidence_score != null ? `<span class="muted" style="font-size:.85rem">Confidence ${s.confidence_score}/7</span>` : ''}
      </div>
    </div>
    <div class="hist-card-meta muted">
      ${escapeHtml(s.trade_name || s.suggestion_id)}
      &nbsp;·&nbsp;Generated: ${fmtDt(s.generated_on)}
      ${s.expiry_date ? '&nbsp;·&nbsp;Expiry: '+fmtDt(s.expiry_date) : ''}
      ${s.dte != null ? '&nbsp;·&nbsp;DTE: '+s.dte : ''}
      ${s.entry_date ? '&nbsp;·&nbsp;Entry day: '+s.entry_date : ''}
    </div>
    ${s.net_credit_suggested != null ? `
    <div class="hcmp-grid">
      <div class="hcmp-header"><span>Economics (Suggested)</span></div>
      <div class="hcmp-row"><span class="hcmp-key">Net credit</span><span class="hcmp-sug">₹${fmt(s.net_credit_suggested)}</span></div>
      ${s.max_profit != null ? `<div class="hcmp-row"><span class="hcmp-key">Max profit</span><span class="hcmp-sug">₹${fmt(s.max_profit)}</span></div>` : ''}
      ${s.max_loss != null ? `<div class="hcmp-row"><span class="hcmp-key">Max loss</span><span class="hcmp-sug">₹${fmt(s.max_loss)}</span></div>` : ''}
      ${s.stop_loss_level != null ? `<div class="hcmp-row"><span class="hcmp-key">Stop loss</span><span class="hcmp-sug">${fmt(s.stop_loss_level)}</span></div>` : ''}
    </div>` : ''}
    ${s.plain_english ? `<div class="hist-card-meta muted" style="margin-top:8px;white-space:pre-line">${escapeHtml(s.plain_english)}</div>` : ''}
  </div>`;
}

function renderHistoryTrade(t) {
  const s = t.suggestion || {};
  const pnl = t.net_pnl;
  const pnlClass = pnl != null ? (pnl >= 0 ? 'pnl-profit' : 'pnl-loss') : '';
  const pnlSign  = pnl != null && pnl > 0 ? '+' : '';
  const statusCls = t.status === 'CLOSED' ? 'tag-ok' : t.status === 'EXPIRED' ? 'tag-warn' : 'tag-acc';

  // Comparison row helper: key | suggested value | actual value
  const cmp = (key, sv, av) => `
    <div class="hcmp-row">
      <span class="hcmp-key">${key}</span>
      <span class="hcmp-sug">${sv}</span>
      <span class="hcmp-act">${av}</span>
    </div>`;

  const r = (v, prefix='₹') => v != null ? prefix + fmt(v) : '—';

  const legRows = (t.legs || []).map(l => {
    const lpc = l.leg_pnl != null ? (l.leg_pnl >= 0 ? 'pnl-profit' : 'pnl-loss') : '';
    const sugRange = l.suggested_price_low != null
      ? `₹${fmt(l.suggested_price_low)}–${fmt(l.suggested_price_high)}`
      : (l.suggested_price != null ? '₹'+fmt(l.suggested_price) : '—');
    return `<tr>
      <td class="num">${l.leg_order}</td>
      <td><span class="tag tag-${l.action==='SELL'?'err':'ok'} tag-sm">${escapeHtml(l.action||'')}</span></td>
      <td>${escapeHtml(l.symbol||'')} <strong>${l.strike||''}</strong> ${escapeHtml(l.option_type||'')}</td>
      <td class="muted">${escapeHtml(fmtDt(l.fill_time))}</td>
      <td class="muted">${escapeHtml(fmtDt(l.exit_time))}</td>
      <td class="muted">${escapeHtml(legRoleNote(s.strategy, l))}</td>
      <td class="num muted">${sugRange}</td>
      <td class="num">${l.fill_price != null ? '₹'+fmt(l.fill_price) : '—'}</td>
      <td class="num">${l.exit_price != null ? '₹'+fmt(l.exit_price) : '—'}</td>
      <td class="num ${lpc}">${l.leg_pnl != null ? '₹'+fmt(l.leg_pnl) : '—'}</td>
    </tr>`;
  }).join('');

  return `
  <div class="hist-card">
    <div class="hist-card-head">
      <div class="hist-card-title">
        <strong class="hist-instr">${escapeHtml(s.underlying || t.trade_id)}</strong>
        <span class="tag tag-accent">${escapeHtml(s.strategy || '')}</span>
        <span class="tag ${statusCls}">${escapeHtml(t.status || '')}</span>
        ${t.position_type ? `<span class="muted" style="font-size:.78rem">${escapeHtml(t.position_type)}</span>` : ''}
      </div>
      <div class="hist-card-pnl ${pnlClass}">${pnl != null ? pnlSign+'₹'+fmt(pnl) : '—'}${pnl != null && t.net_credit_actual ? `<span class="hist-pnl-pct"> (${pnl >= 0 ? '+' : ''}${(pnl / Math.abs(t.net_credit_actual) * 100).toFixed(0)}% of credit)</span>` : ''}</div>
    </div>
    <div class="hist-card-meta muted">
      ${escapeHtml(t.trade_name || t.trade_id)}
      &nbsp;·&nbsp;Executed: ${fmtDt(t.executed_on)}
      ${t.closed_on ? '&nbsp;·&nbsp;Closed: '+fmtDt(t.closed_on) : ''}
      ${s.expiry ? '&nbsp;·&nbsp;Expiry: '+fmtDt(s.expiry)+(s.dte != null ? ' ('+s.dte+'d)' : '') : ''}
    </div>

    <div class="hcmp-grid">
      <div class="hcmp-header">
        <span>Parameter</span>
        <span class="hcmp-col-head">Suggested</span>
        <span class="hcmp-col-head">Actual</span>
      </div>
      ${cmp('Credit received',   r(s.net_credit),      r(t.net_credit_actual))}
      ${cmp('Max profit',        r(s.max_profit),      r(t.actual_max_profit))}
      ${cmp('Max loss',          r(s.max_loss),        r(t.actual_max_loss))}
      ${cmp('Gross P&amp;L',     '—',                  r(t.gross_pnl))}
      ${cmp('Charges / tax',     r(s.est_charges),     r(t.total_charges))}
      ${cmp('Net P&amp;L',       r(s.est_net_pnl),     `<span class="${pnlClass} hcmp-bold">${pnl != null ? pnlSign+'₹'+fmt(pnl) : '—'}</span>`)}
      ${cmp('Spot at entry',     s.spot != null ? fmt(s.spot) : '—',  t.spot_at_execution != null ? fmt(t.spot_at_execution) : '—')}
      ${cmp('Upper breakeven',   s.upper_be != null ? fmt(s.upper_be) : '—',  t.actual_upper_be != null ? fmt(t.actual_upper_be) : '—')}
      ${cmp('Lower breakeven',   s.lower_be != null ? fmt(s.lower_be) : '—',  t.actual_lower_be != null ? fmt(t.actual_lower_be) : '—')}
      ${cmp('Stop loss level',   s.stop_loss != null ? fmt(s.stop_loss) : '—',  t.actual_stop_loss != null ? fmt(t.actual_stop_loss) : '—')}
      ${s.pop != null ? cmp('Prob. of profit', fmtPct(s.pop), '—') : ''}
      ${s.confidence != null ? cmp('Confidence', s.confidence+'/7', '—') : ''}
      ${t.exit_instruction ? cmp('Exit reason', '—', `<span class="muted">${escapeHtml(t.exit_instruction)}</span>`) : ''}
    </div>

    ${legRows ? `
    <div class="hist-legs-wrap">
      <div class="hist-legs-title">Legs — Suggested vs Executed</div>
      <div class="hist-legs-scroll">
        <table class="hist-legs-tbl">
          <thead><tr>
            <th class="num">#</th><th>Dir</th><th>Contract</th><th>Executed</th><th>Exited</th><th>Purpose</th>
            <th class="num">Suggested range</th><th class="num">Fill price</th><th class="num">Exit price</th><th class="num">Leg P&amp;L</th>
          </tr></thead>
          <tbody>${legRows}</tbody>
        </table>
      </div>
    </div>` : ''}
  </div>`;
}

// ---------------- Tab 4: Logs ----------------
async function loadLogs() {
  const c = $('#logs-container');
  c.className='loading'; c.textContent='Loading…';
  const params = new URLSearchParams();
  const lvl = $('#log-level').value;
  const q   = $('#log-search').value;
  if (lvl) params.set('level', lvl);
  if (q)   params.set('search', q);
  try {
    const [logs, jobs] = await Promise.all([
      API('/api/logs?' + params),
      API('/api/jobs/latest'),
    ]);
    $('#jobs-strip').innerHTML = jobs.jobs.map(j => `
      <div class="job-pill ${escapeHtml(j.status)}">
        <strong>${escapeHtml(j.job_name)}</strong><br>
        <span class="muted" style="font-size:.7rem">${escapeHtml(j.finished_at || j.started_at || '')}</span>
      </div>`).join('') || '<div class="muted">No job runs yet.</div>';
    if (!logs.logs.length) {
      c.className=''; c.innerHTML = '<div class="empty">No logs.</div>'; return;
    }
    c.className='';
    c.innerHTML = `<table class="dt"><thead><tr>
      <th>Time</th><th>Level</th><th>Module</th><th>Message</th></tr></thead>
      <tbody>${logs.logs.map(l => `<tr>
        <td>${escapeHtml(l.logged_at)}</td>
        <td><span class="tag tag-${levelClass(l.level)}">${escapeHtml(l.level)}</span></td>
        <td>${escapeHtml(l.module || '')}</td>
        <td>${escapeHtml(l.message)}</td></tr>`).join('')}
      </tbody></table>`;
  } catch (e) {
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}
function levelClass(lvl) {
  if (lvl === 'CRITICAL' || lvl === 'ERROR') return 'err';
  if (lvl === 'WARNING') return 'warn';
  if (lvl === 'INFO') return 'info';
  return '';
}
$('#log-refresh').addEventListener('click', loadLogs);
$('#log-level').addEventListener('change', loadLogs);
$('#log-search').addEventListener('keydown', e => { if (e.key === 'Enter') loadLogs(); });

// History filter bindings — Trades sub-tab
$('#hist-refresh').addEventListener('click', loadHistory);
$('#hist-instrument').addEventListener('change', loadHistory);
$('#hist-from').addEventListener('change', loadHistory);
$('#hist-to').addEventListener('change', loadHistory);

// History filter bindings — Suggestions sub-tab
$('#hsug-refresh').addEventListener('click', loadHistorySuggestions);
$('#hsug-instrument').addEventListener('change', loadHistorySuggestions);
$('#hsug-status').addEventListener('change', loadHistorySuggestions);
$('#hsug-from').addEventListener('change', loadHistorySuggestions);
$('#hsug-to').addEventListener('change', loadHistorySuggestions);

// ---------------- Tab 5: Config ----------------
async function loadConfig() {
  const c = $('#config-container');
  c.className='loading'; c.textContent='Loading…';
  try {
    const data = await API('/api/config');
    if (!data.config.length) {
      c.className=''; c.innerHTML = '<div class="empty">No runtime overrides set.</div>';
      return;
    }
    c.className='';
    c.innerHTML = `<table class="dt"><thead><tr>
      <th>Key</th><th>Value</th><th>Default</th><th>Modified</th></tr></thead>
      <tbody>${data.config.map(r => `<tr>
        <td><code>${escapeHtml(r.config_key)}</code></td>
        <td><code>${escapeHtml(r.config_value)}</code></td>
        <td><code class="muted">${escapeHtml(r.default_value || '')}</code></td>
        <td class="muted" style="font-size:.8rem">${escapeHtml(r.modified_at || '')}</td>
        </tr>`).join('')}
      </tbody></table>`;
  } catch (e) {
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

// ---------------- Utils ----------------
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// ---------------- Tab 6: Jobs (scheduler monitor + manual trigger) ----------------
const JOB_STATUS_META = {
  RUNNING: { label: 'Running',   cls: 'js-running' },
  SUCCESS: { label: 'Success',   cls: 'js-success' },
  FAILED:  { label: 'Failed',    cls: 'js-failed'  },
  SKIPPED: { label: 'Skipped',   cls: 'js-skipped' },
  NEVER:   { label: 'Never run', cls: 'js-never'   },
};

let _jobsTimer = null;

function stopJobsAutoRefresh() {
  if (_jobsTimer) { clearInterval(_jobsTimer); _jobsTimer = null; }
}
function startJobsAutoRefresh() {
  stopJobsAutoRefresh();
  _jobsTimer = setInterval(() => {
    if (document.getElementById('panel-jobs')?.classList.contains('active')) {
      loadJobs(true);
    }
  }, 5000);
}

function _jobRelTime(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  const diff = (Date.now() - t) / 1000;
  if (Math.abs(diff) < 60) return diff >= 0 ? `${Math.round(diff)}s ago` : `in ${Math.round(-diff)}s`;
  if (Math.abs(diff) < 3600) return diff >= 0 ? `${Math.round(diff/60)}m ago` : `in ${Math.round(-diff/60)}m`;
  if (Math.abs(diff) < 86400) return diff >= 0 ? `${(diff/3600).toFixed(1)}h ago` : `in ${(-diff/3600).toFixed(1)}h`;
  return diff >= 0 ? `${Math.round(diff/86400)}d ago` : `in ${Math.round(-diff/86400)}d`;
}

function _jobDuration(startIso, endIso) {
  if (!startIso) return '';
  const start = new Date(startIso).getTime();
  const end   = endIso ? new Date(endIso).getTime() : Date.now();
  if (isNaN(start) || isNaN(end)) return '';
  const sec = Math.max(0, (end - start) / 1000);
  if (sec < 60)    return `${sec.toFixed(1)}s`;
  if (sec < 3600)  return `${(sec/60).toFixed(1)}m`;
  return `${(sec/3600).toFixed(1)}h`;
}

async function loadJobs(silent = false) {
  const c = $('#jobs-container');
  if (!c) return;
  if (!silent) { c.className = 'loading'; c.textContent = 'Loading…'; }
  try {
    const data = await API('/api/jobs/list');
    const updated = $('#jobs-updated');
    if (updated) updated.textContent = `Updated: ${fmtDt(data.generated_at)}` + (data.scheduler_running ? '' : '  •  scheduler not running');

    if (!data.jobs.length) {
      c.className = ''; c.innerHTML = '<div class="empty">No jobs configured.</div>';
      return;
    }
    c.className = '';
    c.innerHTML = `<div class="jobs-grid">${data.jobs.map(renderJobCard).join('')}</div>`;
  } catch (e) {
    c.className = ''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderJobCard(j) {
  const sm = JOB_STATUS_META[j.status] || JOB_STATUS_META.NEVER;
  const isRunning = j.status === 'RUNNING';
  const isFailed  = j.status === 'FAILED';
  const cardCls = `job-card${isRunning ? ' job-card--running' : isFailed ? ' job-card--failed' : ''}`;

  const dur = _jobDuration(j.started_at, j.finished_at);
  const lastRunIso = j.finished_at || j.started_at;
  const lastRunRel = _jobRelTime(lastRunIso);
  const nextRunRel = _jobRelTime(j.next_run);

  const errLine = (isFailed && j.error_message)
    ? `<div class="job-error">⚠ ${escapeHtml(String(j.error_message).slice(0, 200))}</div>`
    : '';

  const rowsLine = (j.rows_processed != null)
    ? `<div class="job-meta-row"><span>Rows</span><span>${escapeHtml(String(j.rows_processed))}</span></div>`
    : '';

  const triggerBtn = `<button class="btn job-trigger-btn" data-job="${escapeHtml(j.job_name)}"
      ${isRunning || !j.enabled ? 'disabled' : ''}>
      ${isRunning ? '⏳ Running…' : '▶ Run now'}
    </button>`;

  return `<div class="${cardCls}">
    <div class="job-card-head">
      <span class="job-icon">${escapeHtml(j.icon)}</span>
      <div class="job-title-block">
        <div class="job-name">${escapeHtml(j.display_name)}</div>
        <div class="job-schedule">${escapeHtml(j.schedule)}${j.enabled ? '' : ' • disabled'}</div>
      </div>
      <span class="job-status ${sm.cls}">${sm.label}</span>
    </div>
    <div class="job-desc">${escapeHtml(j.description)}</div>
    ${errLine}
    <div class="job-meta">
      <div class="job-meta-row"><span>Last run</span><span>${escapeHtml(fmtDt(lastRunIso))}${lastRunRel ? ` <em class="muted">(${lastRunRel})</em>` : ''}</span></div>
      <div class="job-meta-row"><span>Duration</span><span>${escapeHtml(dur || '—')}</span></div>
      ${rowsLine}
      <div class="job-meta-row"><span>Next run</span><span>${escapeHtml(fmtDt(j.next_run))}${nextRunRel ? ` <em class="muted">(${nextRunRel})</em>` : ''}</span></div>
    </div>
    <div class="job-card-foot">${triggerBtn}</div>
  </div>`;
}

// Jobs that support an explicit data date override
const DATE_OVERRIDE_JOBS = new Set([
  'fo_bhav_download','spot_bhav_download','vix_download','fii_download',
  'iv_calculation','suggestion_engine','exit_engine',
]);

async function triggerJob(jobName) {
  if (!jobName) return;

  let tradeDate = '';
  if (DATE_OVERRIDE_JOBS.has(jobName)) {
    const input = prompt(
      `Run "${jobName}"\n\nEnter data date (YYYY-MM-DD) to use specific day's data,\nor leave blank to auto-detect the latest available date:`,
      ''
    );
    if (input === null) return;   // user cancelled
    if (input.trim() !== '') {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(input.trim())) {
        toast('Invalid date format — use YYYY-MM-DD', 'err');
        return;
      }
      tradeDate = input.trim();
    }
  } else {
    if (!confirm(`Trigger "${jobName}" now?`)) return;
  }

  try {
    const body = tradeDate ? JSON.stringify({ trade_date: tradeDate }) : undefined;
    const headers = tradeDate ? { 'Content-Type': 'application/json' } : {};
    await API(`/api/jobs/${encodeURIComponent(jobName)}/trigger`, { method: 'POST', headers, body });
    toast(`Job queued: ${jobName}${tradeDate ? ' (' + tradeDate + ')' : ' (auto-date)'}`, 'ok');
    setTimeout(() => loadJobs(true), 600);
  } catch (e) {
    toast(`Trigger failed: ${e.message}`, 'err');
  }
}

// Delegated click + auto-refresh wiring (idempotent)
document.addEventListener('click', e => {
  const btn = e.target.closest('.job-trigger-btn');
  if (btn && !btn.disabled) {
    e.preventDefault();
    triggerJob(btn.dataset.job);
  }
});
document.addEventListener('DOMContentLoaded', () => {
  const refreshBtn = $('#jobs-refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => loadJobs());
  const auto = $('#jobs-auto');
  if (auto) {
    auto.addEventListener('change', () => {
      if (auto.checked) startJobsAutoRefresh();
      else stopJobsAutoRefresh();
    });
    if (auto.checked) startJobsAutoRefresh();
  }
});

// ---------------- WS Monitor ----------------
let _wsmonTimer = null;
const WSMON_INTERVAL_MS = 1000;

function stopWsMonitorAutoRefresh() {
  if (_wsmonTimer) {
    clearInterval(_wsmonTimer);
    _wsmonTimer = null;
  }
}
function startWsMonitorAutoRefresh() {
  stopWsMonitorAutoRefresh();
  _wsmonTimer = setInterval(() => {
    if (document.getElementById('panel-wsmon')?.classList.contains('active')) {
      loadWsMonitor({ silent: true });
    }
  }, WSMON_INTERVAL_MS);
}

function _fmtAge(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (!isFinite(t)) return '—';
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60)   return sec + 's ago';
  if (sec < 3600) return Math.round(sec / 60) + 'm ago';
  return Math.round(sec / 3600) + 'h ago';
}
function _wsmonStateClass(state) {
  const s = (state || '').toLowerCase();
  if (s === 'connected')                       return 'wsmon-state-ok';
  if (s === 'connecting' || s === 'unknown')   return 'wsmon-state-warn';
  if (s === 'degraded')                        return 'wsmon-state-warn';
  if (s === 'token_expired' || s === 'disconnected' || s === 'stopped') return 'wsmon-state-err';
  return 'wsmon-state-warn';
}

async function loadWsMonitor({ silent = false } = {}) {
  const summary = $('#wsmon-summary');
  const eventsEl = $('#wsmon-events');
  if (!silent && summary) summary.classList.add('loading');

  const topic  = $('#wsmon-topic')?.value || '';
  const symbol = ($('#wsmon-symbol')?.value || '').trim();
  const qs = new URLSearchParams();
  if (topic)  qs.set('topic', topic);
  if (symbol) qs.set('symbol', symbol);
  qs.set('limit', '200');

  let snap;
  try {
    snap = await API('/api/ws/monitor?' + qs.toString());
  } catch (err) {
    if (summary) {
      summary.classList.remove('loading');
      summary.innerHTML = `<div class="empty">Failed to load WS telemetry: ${escapeHtml(String(err))}</div>`;
    }
    return;
  }

  if (!snap || snap.available === false) {
    if (summary) {
      summary.classList.remove('loading');
      summary.innerHTML = `<div class="empty">WS telemetry unavailable.<br><span class="muted">${escapeHtml(snap?.reason || 'no snapshot')}</span></div>`;
    }
    if (eventsEl) eventsEl.innerHTML = '';
    return;
  }

  if (summary) {
    summary.classList.remove('loading');
    const stateClass = _wsmonStateClass(snap.connection_state);
    const runnerState = snap.runner_state || snap.connection_state || 'unknown';
    const topSyms = (snap.top_symbols || []).slice(0, 8)
      .map(t => `<span class="wsmon-pill">${escapeHtml(t.symbol)}<small>${t.ticks}</small></span>`)
      .join('');
    summary.innerHTML = `
      <div class="wsmon-cards">
        <div class="wsmon-card">
          <div class="wsmon-card-label">Provider</div>
          <div class="wsmon-card-value">${escapeHtml(snap.provider || '—')}</div>
        </div>
        <div class="wsmon-card">
          <div class="wsmon-card-label">Runner state</div>
          <div class="wsmon-card-value ${stateClass}">${escapeHtml(runnerState)}</div>
        </div>
        <div class="wsmon-card">
          <div class="wsmon-card-label">Connection state</div>
          <div class="wsmon-card-value ${_wsmonStateClass(snap.connection_state)}">${escapeHtml(snap.connection_state || 'unknown')}</div>
        </div>
        <div class="wsmon-card">
          <div class="wsmon-card-label">Last tick</div>
          <div class="wsmon-card-value">${escapeHtml(_fmtAge(snap.last_tick_at))}</div>
        </div>
        <div class="wsmon-card">
          <div class="wsmon-card-label">Tick rate (${Math.round(snap.rate_window_seconds || 60)}s avg)</div>
          <div class="wsmon-card-value">${(snap.tick_rate_per_sec ?? 0).toFixed(2)} /s</div>
        </div>
        <div class="wsmon-card">
          <div class="wsmon-card-label">Total ticks</div>
          <div class="wsmon-card-value">${(snap.tick_count_total || 0).toLocaleString()}</div>
        </div>
        <div class="wsmon-card">
          <div class="wsmon-card-label">Subscribed tokens</div>
          <div class="wsmon-card-value">${snap.subscribed_tokens ?? '—'}</div>
        </div>
        <div class="wsmon-card">
          <div class="wsmon-card-label">Reconnect attempts</div>
          <div class="wsmon-card-value">${snap.reconnect_attempts ?? 0}</div>
        </div>
      </div>
      ${snap.last_error ? `<div class="wsmon-error">Last error: <code>${escapeHtml(snap.last_error)}</code></div>` : ''}
      ${topSyms ? `<div class="wsmon-tops"><span class="muted">Top symbols:</span> ${topSyms}</div>` : ''}
      <div class="muted" style="font-size:.74rem;margin-top:.4rem;">
        Snapshot @ ${escapeHtml(snap.generated_at || '')} &middot; uptime ${Math.round(snap.uptime_seconds || 0)}s
      </div>
    `;
  }

  if (eventsEl) {
    const evs = snap.recent_events || [];
    if (!evs.length) {
      eventsEl.classList.remove('loading');
      eventsEl.innerHTML = '<div class="empty">No events match the current filter.</div>';
      return;
    }
    const rows = evs.map(e => {
      const tag = String(e.topic || '').toLowerCase();
      let detail = '';
      if (tag === 'tick') {
        const px = e.last_price != null ? Number(e.last_price).toFixed(2) : '—';
        const strike = e.strike != null ? `${e.strike}${e.option_type || ''}` : '';
        detail = `<span class="wsmon-ev-sym">${escapeHtml(e.symbol || '?')}</span> ${escapeHtml(strike)} <span class="wsmon-ev-px">@ ${px}</span>`;
      } else if (tag === 'connection_state') {
        detail = `<span class="wsmon-ev-state ${_wsmonStateClass(e.state)}">${escapeHtml(e.state || '?')}</span>${e.detail ? ` <span class="muted">${escapeHtml(String(e.detail))}</span>` : ''}`;
      } else if (tag === 'token_expired') {
        detail = `<span class="wsmon-ev-state wsmon-state-err">token_expired</span>`;
      } else {
        detail = escapeHtml(JSON.stringify(e));
      }
      return `<div class="wsmon-ev wsmon-ev-${escapeHtml(tag)}">
        <span class="wsmon-ev-ts">${escapeHtml((e.ts || '').replace('T',' ').slice(0,19))}</span>
        <span class="wsmon-ev-tag">${escapeHtml(tag)}</span>
        ${detail}
      </div>`;
    }).join('');
    eventsEl.classList.remove('loading');
    eventsEl.innerHTML = `<div class="wsmon-ev-list">${rows}</div>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const panel = document.getElementById('panel-wsmon');
  if (!panel) return;
  $('#wsmon-refresh')?.addEventListener('click', () => loadWsMonitor());
  $('#wsmon-topic')?.addEventListener('change', () => loadWsMonitor());
  $('#wsmon-symbol')?.addEventListener('input', _debounce(() => loadWsMonitor(), 300));
  const auto = $('#wsmon-auto');
  if (auto) {
    auto.addEventListener('change', () => {
      if (auto.checked) startWsMonitorAutoRefresh();
      else stopWsMonitorAutoRefresh();
    });
    if (auto.checked) startWsMonitorAutoRefresh();
  }
});

function _debounce(fn, ms) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// ---------------- Boot ----------------
loadSuggestion();
refreshNotifBadge();
setInterval(refreshNotifBadge, 60000);
