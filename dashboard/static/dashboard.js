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
const fmtDt  = s => { if (!s) return '—'; try { const d = new Date(s); return d.toLocaleString('en-IN', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:false}); } catch(e) { return String(s); } };

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
const TABS = ['suggestion', 'trades', 'history', 'logs', 'jobs', 'config'];
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
  if (name === 'config')     loadConfig();
  // Stop jobs auto-refresh when leaving the tab
  if (name !== 'jobs') stopJobsAutoRefresh();
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
    const data = await API('/api/suggestion/today');
    const list = data.suggestions || [];
    if (!list.length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No suggestion yet.</div>';
      return;
    }
    c.className = '';
    c.innerHTML = list.map(renderSuggestion).join('');
    bindSuggestionActions();
  } catch (e) {
    c.className = ''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
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
  const introLines = [], timelineItems = [];
  let confLine = '', mode = 'intro';
  for (const line of rawLines) {
    if (/^ENTRY THRESHOLDS/i.test(line)) { mode = 'entry'; continue; }
    if (/^TARGET CLOSE/i.test(line))     { mode = 'target'; continue; }
    if (/^TIMELINE/i.test(line))         { mode = 'timeline'; continue; }
    if (/All \d+ confidence/i.test(line)) { confLine = line; continue; }
    if (line.startsWith('\u2022') || line.startsWith('-')) {
      if (mode === 'timeline') timelineItems.push(line.replace(/^[\u2022\-]\s*/, '').trim());
      // entry/target bullets already shown on leg chips — skip
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
  if (spot)               chips.push(`<span class="ctx-chip">Spot ₹${escapeHtml(spot)}</span>`);
  if (ivRank)             chips.push(`<span class="ctx-chip ctx-iv">IV Rank ${escapeHtml(ivRank)}%</span>`);
  if (s.confidence_score) chips.push(`<span class="ctx-chip ctx-pass">${s.confidence_score}/7 checks ✓</span>`);
  const tlRows = timelineItems.map(item => {
    const ci = item.indexOf(':');
    if (ci < 0) return `<div class="tl-row"><span class="tl-val" style="grid-column:span 2">${escapeHtml(item)}</span></div>`;
    const label = item.slice(0, ci).trim();
    const val   = item.slice(ci + 1).trim();
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
  const timelineHtml = tlRows
    ? `<div class="sug-section"><div class="sug-section-title">Timeline</div><div class="sug-timeline">${tlRows}</div></div>`
    : (s.execution_window ? `<div class="exec-window-badge">📅 Execute: ${escapeHtml(s.execution_window)}</div>` : '');
  const confHtml = confLine
    ? `<div class="sug-conf">${escapeHtml(confLine)}</div>`
    : '';
  return contextHtml + introHtml + timelineHtml + confHtml;
}

// Build the per-card suggestion render output.
function renderSuggestion(s) {
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
  const baseQty = (s.legs || []).reduce(
    (a, l) => a + (l.lots || 0) * (l.lot_size || 0), 0) || 1;
  const baseTotalCredit = (econ.np || 0) * baseQty;
  // Spread width (in rupees, summed over baseQty). Stays constant when fill
  // prices move — only the credit/debit allocation between profit & loss
  // shifts. We use this to recompute max-loss live as user edits prices.
  const baseWidthTotal = (econ.mp || 0) + (econ.ml || 0);
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
    return `
    <div class="leg-row action-${l.action}" data-leg-action="${l.action}">
      <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'}">${l.action}</span>
      <div>
        <div><strong>${escapeHtml(l.symbol)} ${escapeHtml(l.expiry_date || '')} ${l.strike} ${l.option_type}</strong></div>
        <div class="leg-meta">
          <input type="number" class="leg-lots" min="1" value="${l.lots || 1}"
                 data-lot-size="${l.lot_size}" data-leg-order="${l.leg_order}"
                 data-price="${l.suggested_price}"
                 data-orig-lots="${l.lots || 1}">×
          lot ${l.lot_size} @ ₹<span class="leg-price-shown" data-leg-order="${l.leg_order}">${fmt(l.suggested_price)}</span> =
          <strong><span class="leg-total" data-leg-order="${l.leg_order}">₹${fmt(legTotal)}</span></strong>
          <span class="leg-price-range muted">(range ₹${fmt(l.suggested_price_low)}–₹${fmt(l.suggested_price_high)})</span>
        </div>
        <div class="leg-hints">${thresholdHint} · ${closeHint}</div>
        <div class="muted" style="font-size:.8rem">${escapeHtml(l.leg_purpose_note || '')}</div>
      </div>
      <label class="leg-fill">
        <input type="checkbox" data-leg="${l.leg_order}" class="leg-exec" checked>
        <input type="number" step="0.05" data-leg-price="${l.leg_order}"
               value="${l.suggested_price}" style="width:90px">
      </label>
    </div>`;
  }).join('');
  // attach live lot-count recalc after DOM insert (delegate via event on card)
  // we do it after the card is inserted — see bindSuggestionActions

  return `<div class="card" data-sug-id="${escapeHtml(s.suggestion_id)}"
              data-base-qty="${baseQty}"
              data-base-np="${econ.np || 0}"
              data-base-mp="${econ.mp || 0}"
              data-base-ml="${econ.ml || 0}"
              data-base-chg="${econ.chg || 0}"
              data-base-npnl="${econ.npnl || 0}"
              data-base-tot-credit="${baseTotalCredit}"
              data-base-width-total="${baseWidthTotal}"
              data-base-sl="${econ.sl || 0}"
              data-spot-at-gen="${s.spot_at_generation || 0}">
    <div class="card-head">
      <h3>${escapeHtml(s.trade_name || s.suggestion_id)}</h3>
      <span class="tag tag-accent">${escapeHtml(s.strategy || '')}</span>
    </div>
    ${renderPlainEnglishStructured(s)}
    <div class="kv-grid">
      <div><span class="k">Net credit (per unit)</span><br><span class="v econ-np">₹${fmt(econ.np)}</span></div>
      <div><span class="k">Total credit <span class="econ-qty-hint muted" style="font-size:.75rem">(×${baseQty})</span></span><br><span class="v econ-tot-credit">₹${fmt(baseTotalCredit)}</span></div>
      <div><span class="k">Max profit</span><br><span class="v econ-mp">₹${fmt(econ.mp)}</span></div>
      <div><span class="k">Max loss</span><br><span class="v econ-ml">₹${fmt(econ.ml)}<span class="econ-ml-hint">${pctHint(econ.ml, econ.np, 'credit')}</span></span></div>
      <div><span class="k">PoP</span><br><span class="v">${fmtPct(econ.pop)}</span></div>
      <div><span class="k">Upper BE</span><br><span class="v">₹${fmt(econ.ub)}${spotDist(econ.ub, s.spot_at_generation)}</span></div>
      <div><span class="k">Lower BE</span><br><span class="v">₹${fmt(econ.lb)}${spotDist(econ.lb, s.spot_at_generation)}</span></div>
      <div><span class="k">Stop loss</span><br><span class="v">₹${fmt(econ.sl)}${spotDist(econ.sl, s.spot_at_generation)}</span></div>
      <div><span class="k">Premium SL <span class="muted" style="font-size:.72rem">(1.5× credit)</span></span><br><span class="v econ-psl">₹${fmt((econ.np||0) * baseQty * 1.5)}</span></div>
      <div><span class="k">Est. charges</span><br><span class="v econ-chg">₹${fmt(econ.chg)}</span></div>
      <div><span class="k">Est. net P&amp;L</span><br><span class="v econ-npnl">₹${fmt(econ.npnl)}</span></div>
      <div><span class="k">DTE</span><br><span class="v">${s.dte ?? '—'}</span></div>
    </div>
    <div class="legs-grid">${legsHtml}</div>
    <div class="exec-spot-bar">
      <div class="sl-monitor-label" style="margin-bottom:6px">Nifty spot at execution</div>
      <div class="exec-spot-row">
        <div class="sl-field">
          <label class="sl-label">Your actual Nifty spot <span class="muted" style="font-size:.7rem">(AI used ₹${fmt(s.spot_at_generation)})</span></label>
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
    </div>
  </div>`;
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
      // Note: the per-unit credit summed above is in fact per single unit
      // (one quantity), not per baseQty. The base 'np' field is also per
      // single unit, so they are directly comparable.
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
      setText('.econ-psl',        `₹${fmt(liveTotalCredit * 1.5)}`);
      const qtyHint = card.querySelector('.econ-qty-hint');
      if (qtyHint) qtyHint.textContent = `(×${curQty})`;
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
    const card = e.target.closest('.card');
    const sid  = card.dataset.sugId;
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
    const spotInput = card.querySelector('.exec-spot-input');
    const spotRaw = spotInput?.value.trim();
    const spotVal = spotRaw ? parseFloat(spotRaw) : null;
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
      openCloseForm(e.target.dataset.tradeId);
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
    const legsHtml = data.legs.map(l => `
      <div class="leg-row action-${escapeHtml(l.action)}" data-leg-order="${l.leg_order}">
        <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'}">${escapeHtml(l.action)}</span>
        <div>
          <div><strong>${escapeHtml(l.symbol)} ${l.strike} ${escapeHtml(l.option_type)}</strong></div>
          <div class="leg-meta">
            <input type="number" class="leg-lots" min="1" value="${l.lots || 1}"
                   data-lot-size="${l.lot_size}" data-leg-order="${l.leg_order}"
                   data-price="${l.suggested_price}">×
            lot ${l.lot_size} @ ₹${fmt(l.suggested_price)}
          </div>
          <div class="muted" style="font-size:.8rem">${escapeHtml(l.leg_purpose_note || '')}</div>
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
async function openCloseForm(tradeId) {
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
        ${legsHtml}
        <div class="live-pnl-preview" id="live-pnl-${escapeHtml(tradeId)}">
          P&amp;L preview: <strong class="live-pnl-value">—</strong>
        </div>
        <div class="btn-row" style="margin-top:8px">
          <button class="btn btn-close-trade btn-close-submit" data-trade-id="${escapeHtml(tradeId)}">Confirm close &amp; record fills</button>
          <button class="btn btn-ghost btn-close-cancel">Cancel</button>
        </div>
      </div>`;
    // live P&L calc
    function recalcClosePnl() {
      let total = 0; let allFilled = true;
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
        total += legPnl;
      });
      const el = panel.querySelector('.live-pnl-value');
      if (!el) return;
      if (!allFilled) { el.textContent = '—'; el.className = 'live-pnl-value'; return; }
      el.textContent = `₹${fmt(total)}`;
      el.className = 'live-pnl-value ' + (total >= 0 ? 'pnl-pos' : 'pnl-neg');
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
    if (openExecLegs.length > 0) {
      const netCreditActual = t.net_credit_actual || 0;
      const totalQty = openExecLegs.reduce((a, l) => a + ((l.lots_actual || l.lots || 1) * (l.lot_size || 1)), 0) || 1;
      // Per-unit net credit = sum of (SELL fills) - sum of (BUY fills), averaged over legs
      let perUnitCredit = 0;
      openExecLegs.forEach(l => {
        perUnitCredit += (l.action === 'SELL' ? 1 : -1) * (l.fill_price || 0);
      });
      const target50pct = perUnitCredit * 0.5 * totalQty;
      const targetRows = openExecLegs.map(l => {
        const tc = (l.fill_price || 0) * 0.5;
        const lotsUsed = l.lots_actual || l.lots || 1;
        const lotSize = l.lot_size || 1;
        const qty = lotsUsed * lotSize;
        const closeVerb = l.action === 'SELL' ? 'Buy back' : 'Sell back';
        const sign = l.action === 'SELL' ? '\u2264' : '\u2265';
        return `<div class="target-row">
          <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'} tag-sm">${escapeHtml(l.action||'')}</span>
          <span><strong>${escapeHtml(l.symbol||'')} ${l.strike||''} ${escapeHtml(l.option_type||'')}</strong></span>
          <span>${closeVerb} ${sign} <strong>\u20b9${fmt(tc)}</strong> <span class="muted">(50% of \u20b9${fmt(l.fill_price)} entry \u00d7 ${qty}u)</span></span>
        </div>`;
      }).join('');
      targetSummaryHtml = `<div class="target-exit-box">
        <div class="target-exit-title">\u{1F3AF} Target exit (50% profit capture)</div>
        ${targetRows}
        <div class="target-exit-keep">Keep ~\u20b9${fmt(target50pct)} of the \u20b9${fmt(netCreditActual * totalQty)} total credit received</div>
      </div>`;
    }
    legsHtml = `<div class="trade-legs-section">
      ${targetSummaryHtml}
      ${legs.map(l => {
        const done = !!l.executed;
        const lotsUsed = l.lots_actual || l.lots || 0;
        const tag = `<span class="tag ${(l.action||'') === 'SELL' ? 'tag-err' : 'tag-ok'}">${escapeHtml(l.action||'')}</span>`;
        const instrument = `${escapeHtml(l.symbol||'')} ${l.strike||''} ${escapeHtml(l.option_type||'')}`;
        if (done && l.exit_price != null) {
          const pnlClass = l.leg_pnl != null ? (l.leg_pnl >= 0 ? 'pnl-profit' : 'pnl-loss') : '';
          return `<div class="trade-leg-row leg-done leg-exited">
            ${tag}
            <div class="tl-info">
              <span class="tl-instrument">${instrument}</span>
              <span class="leg-status-done">\u2713 Filled @ \u20b9${fmt(l.fill_price)} \u00b7 ${lotsUsed} lot${lotsUsed !== 1 ? 's' : ''}</span>
              <span class="leg-exit-info">\u21b3 Closed @ \u20b9${fmt(l.exit_price)}${l.leg_pnl != null ? ` &nbsp;<span class="${pnlClass}">P&L: \u20b9${fmt(l.leg_pnl)}</span>` : ''}</span>
            </div>
          </div>`;
        } else if (done) {
          const targetClose = (l.fill_price || 0) * 0.5;
          const closeHint = l.action === 'SELL'
            ? `<span class="leg-target-close">Target buy back \u2264 \u20b9${fmt(targetClose)} <span class="muted" style="font-size:.72rem">(50% of \u20b9${fmt(l.fill_price)} entry)</span></span>`
            : `<span class="leg-target-close">Target sell back \u2265 \u20b9${fmt(targetClose)} <span class="muted" style="font-size:.72rem">(50% of \u20b9${fmt(l.fill_price)} entry)</span></span>`;
          return `<div class="trade-leg-row leg-done">
            ${tag}
            <div class="tl-info">
              <span class="tl-instrument">${instrument}</span>
              <span class="leg-status-done">\u2713 Filled</span>
              <span class="tl-fill">@ \u20b9${fmt(l.fill_price)} \u00b7 ${lotsUsed} lot${lotsUsed !== 1 ? 's' : ''}</span>
              ${closeHint}
            </div>
          </div>`;
        } else {
          const note = legNextAction(l, legs);
          return `<div class="trade-leg-row leg-pending">
            ${tag}
            <div class="tl-info">
              <span class="tl-instrument">${instrument}</span>
              <span class="leg-status-pending">\u23f3 Pending</span>
              <span class="leg-next-action">${escapeHtml(note)}</span>
            </div>
          </div>`;
        }
      }).join('')}
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
    <div class="kv-grid">
      <div><span class="k">Type</span><br><span class="v">${escapeHtml(t.position_type)}</span></div>
      <div><span class="k">Net credit</span><br><span class="v">\u20b9${fmt(t.net_credit_actual)}</span></div>
      <div><span class="k">P&amp;L</span><br><span class="v">\u20b9${fmt(t.net_pnl)}${pctHint(t.net_pnl, t.net_credit_actual, 'credit')}</span></div>
      <div><span class="k">Status</span><br><span class="v">${escapeHtml(t.status)}</span></div>
    </div>
    <div class="sl-monitor-section">
      <div class="sl-monitor-label">Stop-loss monitor</div>
      <div class="sl-monitor-grid">
        <div class="sl-field">
          <label class="sl-label">Nifty SL level</label>
          <span class="sl-prem-val">${t.actual_stop_loss_level != null ? `\u20b9${fmt(t.actual_stop_loss_level)}` : '\u2014 not set'}</span>
        </div>
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
    <div class="btn-row" style="margin-top:10px">
      <button class="btn btn-ghost btn-resuggest" data-trade-id="${escapeHtml(t.trade_id)}">
        Generate resuggestion</button>
      ${isPartial ? `<button class="btn btn-warn btn-complete-trade" data-trade-id="${escapeHtml(t.trade_id)}">
        Complete Trade</button>` : ''}
      ${hasExecutedLegs ? `<button class="btn btn-close-trade" data-trade-id="${escapeHtml(t.trade_id)}">
        Close Trade</button>` : ''}
      <button class="btn btn-danger btn-void-trade" data-trade-id="${escapeHtml(t.trade_id)}"
              style="margin-left:auto">Void Trade</button>
    </div>
    ${isPartial ? `<div class="supplement-panel" id="supp-${escapeHtml(t.trade_id)}" hidden></div>` : ''}
    ${hasExecutedLegs ? `<div class="close-trade-panel" id="close-${escapeHtml(t.trade_id)}" hidden></div>` : ''}
  </div>`;
}

// ---------------- Tab 3: History ----------------
async function loadHistory() {
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

  try {
    const data = await API('/api/history/closed-trades?' + params);

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
  } catch (e) {
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
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
      <td class="muted">${escapeHtml(l.leg_purpose_note||'')}</td>
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
      <div class="hist-card-pnl ${pnlClass}">${pnl != null ? pnlSign+'₹'+fmt(pnl) : '—'}</div>
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

// History filter bindings
$('#hist-refresh').addEventListener('click', loadHistory);
$('#hist-instrument').addEventListener('change', loadHistory);
$('#hist-from').addEventListener('change', loadHistory);
$('#hist-to').addEventListener('change', loadHistory);

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

async function triggerJob(jobName) {
  if (!jobName) return;
  if (!confirm(`Trigger "${jobName}" now?`)) return;
  try {
    await API(`/api/jobs/${encodeURIComponent(jobName)}/trigger`, { method: 'POST' });
    toast(`Job queued: ${jobName}`, 'ok');
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

// ---------------- Boot ----------------
loadSuggestion();
refreshNotifBadge();
setInterval(refreshNotifBadge, 60000);
