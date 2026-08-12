// =====================================================================
// Options Advisor — Dashboard JS
// Vanilla JS, no framework. Mobile-first.
// =====================================================================
'use strict';

const API = (path, opts={}) => {
  // Auto-set Content-Type: application/json whenever the caller passed a
  // JSON-shaped string body but forgot the header. Without this, Flask's
  // request.get_json() returns None and the endpoint replies 400. This used
  // to silently break the Daily P&L circuit breaker Reset button and any
  // other POST that JSON.stringify()'d its body without an explicit header.
  const headers = { ...(opts.headers || {}) };
  if (opts.body && typeof opts.body === 'string') {
    const hasCt = Object.keys(headers).some(k => k.toLowerCase() === 'content-type');
    const looksJson = /^\s*[{[]/.test(opts.body);
    if (!hasCt && looksJson) headers['Content-Type'] = 'application/json';
  }
  return fetch(path, { ...opts, headers }).then(async r => {
    if (!r.ok) throw new Error((await r.json().catch(()=>({}))).error || r.statusText);
    return r.json();
  });
};

// Normalize leg keys so SSE/poll prices match DOM spans regardless of
// strike formatting (26000 vs 26000.0 vs 26000.0000).
function _normLegKey(sym, strike, ot) {
  return `${String(sym || '').toUpperCase()}|${parseFloat(strike)}|${String(ot || '').toUpperCase()}`;
}

function _lookupLegLtp(legLtps, symbol, strike, optionType) {
  if (!legLtps || typeof legLtps !== 'object') return null;
  const want = _normLegKey(symbol, strike, optionType);
  for (const [k, v] of Object.entries(legLtps)) {
    const parts = k.split('|');
    if (parts.length === 3 && _normLegKey(parts[0], parts[1], parts[2]) === want) return v;
  }
  return null;
}

function _inMarketHours() {
  const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  const dow = now.getDay();
  const hhmm = now.getHours() * 100 + now.getMinutes();
  return dow >= 1 && dow <= 5 && hhmm >= 915 && hhmm <= 1530;
}

const LIVE_FEED_STALE_SEC = 45;
let _zerodhaHasSession = false;
let _zerodhaValid = false;
let _lastMtmByTrade = {};  // tradeId → { mtm, as_of, receivedAt }

function _parseMtmAsOf(asOfStr) {
  if (!asOfStr) return null;
  try {
    const s = String(asOfStr).includes('T') ? asOfStr : String(asOfStr).replace(' ', 'T');
    const t = new Date(s).getTime();
    return Number.isNaN(t) ? null : t;
  } catch { return null; }
}

function _mtmAgeSec(tradeId) {
  const rec = _lastMtmByTrade[tradeId];
  if (!rec || !rec.receivedAt) return Infinity;
  return (Date.now() - rec.receivedAt) / 1000;
}

/** Describe live-price / P&L feed state for UI labels and banners. */
function _liveFeedInfo(tradeId, opts = {}) {
  const { forCloseForm = false, hasLivePrices = false, usingEod = false } = opts;
  const inMarket = _inMarketHours();
  const rec = _lastMtmByTrade[tradeId];
  const fresh = _mtmAgeSec(tradeId) <= LIVE_FEED_STALE_SEC;

  if (inMarket) {
    if (!_zerodhaHasSession || !_zerodhaValid) {
      return {
        mode: 'login',
        label: 'Login required',
        tip: 'Zerodha session missing or expired — log in to receive live prices and P&L.',
        cls: 'tag-err',
        banner: 'No live feed — Zerodha login required',
      };
    }
    if (fresh || (forCloseForm && hasLivePrices)) {
      const asOf = rec?.as_of ? ` Updated ${rec.as_of} IST.` : '';
      return {
        mode: 'live',
        label: 'Live',
        tip: `Live Zerodha feed active.${asOf}`,
        cls: 'tag-ok',
        banner: null,
      };
    }
    if (rec) {
      return {
        mode: 'stale',
        label: 'Stale feed',
        tip: `No fresh ticks — last P&L${rec.as_of ? ' at ' + rec.as_of + ' IST' : ''}.`,
        cls: 'tag-warn',
        banner: 'No live feed — showing last known prices (may be outdated)',
      };
    }
    return {
      mode: 'none',
      label: 'No live feed',
      tip: 'Market is open but no live ticks received yet.',
      cls: 'tag-warn',
      banner: 'No live feed — waiting for Zerodha ticks',
    };
  }
  if (forCloseForm && usingEod) {
    return {
      mode: 'eod',
      label: 'Last EOD',
      tip: 'Market closed — prices from last available bhavcopy.',
      cls: 'tag-warn',
      banner: 'Off market — showing last EOD prices',
    };
  }
  if (rec) {
    return {
      mode: 'stale',
      label: 'Last session',
      tip: `Off market — P&L from last live session${rec.as_of ? ' (' + rec.as_of + ' IST)' : ''}.`,
      cls: 'tag-warn',
      banner: 'Off market — P&L from last live session (not current)',
    };
  }
  return {
    mode: 'off',
    label: 'Off market',
    tip: 'Market is closed — no live prices.',
    cls: 'tag-warn',
    banner: 'Off market — no live prices available',
  };
}

function _updateFeedTag(tradeId, opts = {}) {
  const feed = _liveFeedInfo(tradeId, opts);
  document.querySelectorAll(`.live-feed-tag[data-trade-id="${CSS.escape(tradeId)}"]`).forEach(el => {
    el.textContent = feed.label;
    el.className = `tag live-feed-tag ${feed.cls}`;
    el.title = feed.tip;
  });
  const banner = document.getElementById(`live-feed-banner-${tradeId}`);
  if (banner) {
    if (feed.banner && feed.mode !== 'live') {
      banner.hidden = false;
      banner.textContent = feed.banner;
      banner.className = `live-feed-banner live-feed-banner--${feed.mode}`;
    } else {
      banner.hidden = true;
    }
  }
}

function _buildMtmPayload(trade, snapTrade) {
  const sug = trade.suggestion || {};
  const st = snapTrade || {};
  return {
    ...st,
    trade_id: trade.trade_id,
    max_profit: st.max_profit ?? trade.actual_max_profit ?? sug.max_profit,
    max_loss: st.max_loss ?? trade.actual_max_loss ?? sug.max_loss,
    trailing_pnl_floor: st.trailing_pnl_floor ?? trade.trailing_pnl_floor,
    mtm: st.mtm,
    dte: st.dte,
    as_of: st.as_of,
  };
}

function _bootstrapLiveLevelsForTrades(trades, snap) {
  const byId = (snap && snap.trades) || {};
  (trades || []).forEach(t => {
    const payload = _buildMtmPayload(t, byId[t.trade_id]);
    if (payload.mtm != null) {
      _updateCurrentPnlBadge(t.trade_id, payload.mtm, payload.as_of, false);
    }
    _updateLiveProfitLevels(t.trade_id, payload);
  });
  _refreshAllFeedTags();
}

function _updateLiveRiskStrip(tradeId, state) {
  document.querySelectorAll(`.live-risk-live[data-trade-id="${CSS.escape(tradeId)}"]`).forEach(el => {
    const card = el.closest('.card');
    const staticRa = card && card.querySelector('.risk-alert-static');
    if (!state || state.liveMtm == null || isNaN(state.liveMtm)) {
      el.hidden = true;
      if (staticRa) staticRa.hidden = false;
      return;
    }
    if (state.lossHit) {
      el.className = 'tag tag-err live-risk-live';
      el.textContent = '\ud83d\uded1 LOSS LIMIT HIT';
      el.title = 'Live MTM has reached the loss limit — consider closing';
      el.hidden = false;
      if (staticRa) staticRa.hidden = true;
      return;
    }
    if (state.floorBreach) {
      el.className = 'tag tag-warn live-risk-live';
      el.textContent = '\u26a0\ufe0f PROFIT FLOOR BREACHED';
      el.title = 'Live MTM fell below the armed profit floor';
      el.hidden = false;
      if (staticRa) staticRa.hidden = true;
      return;
    }
    if (state.targetHit) {
      el.className = 'tag tag-ok live-risk-live';
      el.textContent = '\u2705 TARGET HIT';
      el.title = 'Live MTM reached the profit target';
      el.hidden = false;
      if (staticRa) staticRa.hidden = true;
      return;
    }
    el.hidden = true;
    if (staticRa) staticRa.hidden = false;
  });
}

function _resolveLiveLevelThresholds(section, payload) {
  const mp = parseFloat(payload.max_profit ?? section.dataset.maxProfit);
  const mlRaw = payload.max_loss ?? section.dataset.maxLoss;
  const ml = mlRaw != null && mlRaw !== '' ? parseFloat(mlRaw) : null;
  const strat = section.dataset.strategy || '';
  const dte = payload.dte != null ? parseInt(payload.dte, 10) : dteFromExpiry(section.dataset.expiry);
  if (!mp || mp <= 0 || isNaN(mp)) return null;
  const tgtFrac = liveTargetFraction(dte);
  const targetRs = Math.round(mp * tgtFrac);
  const lossRs = effectiveSlRs(strat, ml);
  let floor = payload.trailing_pnl_floor;
  if (floor == null && section.dataset.trailingFloor != null && section.dataset.trailingFloor !== '') {
    floor = parseFloat(section.dataset.trailingFloor);
  }
  if (floor != null && isNaN(floor)) floor = null;
  return { targetRs, lossRs, floor };
}

function _setLiveLevelRowStatus(row, statusEl, active, label, variant) {
  if (!row || !statusEl) return;
  row.classList.toggle('lpl-row-active', !!active);
  row.classList.toggle('lpl-row-hit', !!active && variant === 'hit');
  row.classList.toggle('lpl-row-breach', !!active && variant === 'breach');
  if (active && label) {
    statusEl.textContent = label;
    statusEl.hidden = false;
    statusEl.className = `lpl-status lpl-status-${variant}`;
  } else {
    statusEl.textContent = '';
    statusEl.hidden = true;
    statusEl.className = 'lpl-status';
  }
}

function _clearLiveLevelLabels(section) {
  if (!section) return;
  _setLiveLevelRowStatus(
    section.querySelector('.lpl-target-row'),
    section.querySelector('.lpl-status-target'),
    false, '', 'hit',
  );
  _setLiveLevelRowStatus(
    section.querySelector('.lpl-floor-row'),
    section.querySelector('.lpl-status-floor'),
    false, '', 'breach',
  );
  _setLiveLevelRowStatus(
    section.querySelector('.lpl-loss-row'),
    section.querySelector('.lpl-status-loss'),
    false, '', 'breach',
  );
}

function _updateLiveProfitLevels(tradeId, payload) {
  if (!payload) return;
  const mtm = payload.mtm != null ? parseFloat(payload.mtm) : null;
  const floorIn = payload.trailing_pnl_floor;
  if (floorIn != null) {
    _lastMtmByTrade[tradeId] = _lastMtmByTrade[tradeId] || {};
    _lastMtmByTrade[tradeId].trailing_pnl_floor = floorIn;
  }
  const liveMtm = mtm != null && !isNaN(mtm)
    ? mtm
    : (_lastMtmByTrade[tradeId] && _lastMtmByTrade[tradeId].mtm);

  let stripState = null;
  document.querySelectorAll(`.live-profit-levels[data-trade-id="${CSS.escape(tradeId)}"]`).forEach(section => {
    const floorVal = section.querySelector('.live-profit-floor-val');
    const floorNote = section.querySelector('.live-profit-floor-note');
    const floor = floorIn != null ? parseFloat(floorIn) : null;
    if (floor != null && !isNaN(floor) && floorVal) {
      section.dataset.trailingFloor = String(floor);
      floorVal.textContent = '\u20b9' + fmt(floor);
      floorVal.classList.remove('muted');
      if (floorNote) {
        floorNote.textContent = 'Trailing lock — alert if MTM falls below (always below target)';
      }
    }

    const thresholds = _resolveLiveLevelThresholds(section, payload);
    if (!thresholds || liveMtm == null || isNaN(liveMtm)) {
      _clearLiveLevelLabels(section);
      return;
    }

    const { targetRs, lossRs, floor: armedFloor } = thresholds;
    const targetRow = section.querySelector('.lpl-target-row');
    const floorRow = section.querySelector('.lpl-floor-row');
    const lossRow = section.querySelector('.lpl-loss-row');

    _setLiveLevelRowStatus(
      targetRow,
      section.querySelector('.lpl-status-target'),
      liveMtm >= targetRs,
      'HIT',
      'hit',
    );
    _setLiveLevelRowStatus(
      floorRow,
      section.querySelector('.lpl-status-floor'),
      armedFloor != null && liveMtm < armedFloor,
      'BREACHED',
      'breach',
    );
    _setLiveLevelRowStatus(
      lossRow,
      section.querySelector('.lpl-status-loss'),
      lossRs != null && liveMtm <= -lossRs,
      'HIT',
      'breach',
    );

    stripState = {
      liveMtm,
      lossHit: lossRs != null && liveMtm <= -lossRs,
      floorBreach: armedFloor != null && liveMtm < armedFloor,
      targetHit: liveMtm >= targetRs,
    };
  });
  _updateLiveRiskStrip(tradeId, stripState);
  _updateTradeActionPanel(tradeId, payload, stripState);
}

function _updateCurrentPnlBadge(tradeId, mtm, asOf, liveTick = false) {
  if (mtm != null) {
    _lastMtmByTrade[tradeId] = {
      ...(_lastMtmByTrade[tradeId] || {}),
      mtm,
      as_of: asOf,
      receivedAt: liveTick ? Date.now() : (_parseMtmAsOf(asOf) || Date.now()),
    };
  }
  document.querySelectorAll(`.live-mtm[data-trade-id="${CSS.escape(tradeId)}"]`).forEach(el => {
    const valEl = el.querySelector('.cpnl-val') || el.querySelector('.lpl-current-val');
    const pctEl = el.querySelector('.cpnl-pct-bracket') || el.querySelector('.lpl-current-pct');
    const premRs = parseFloat(el.dataset.premiumRs);
    const premInfo = premRs > 0
      ? { rs: premRs, kind: el.dataset.premiumKind || 'paid' }
      : null;
    const txt = mtm != null
      ? (mtm >= 0 ? '+' : '\u2212') + '\u20b9' + fmt(Math.abs(mtm))
      : '\u2014';
    if (valEl) valEl.textContent = txt;
    if (pctEl) pctEl.innerHTML = (mtm != null && premInfo) ? pnlPctBracket(mtm, premInfo) : '';
    else if (valEl && mtm != null && premInfo) {
      valEl.insertAdjacentHTML('afterend', pnlPctBracket(mtm, premInfo));
    }
    el.classList.toggle('mtm-pos', mtm > 0);
    el.classList.toggle('mtm-neg', mtm < 0);
    const feed = _liveFeedInfo(tradeId);
    const premTip = premInfo
      ? ` · ${premInfo.kind === 'received' ? 'Premium received' : 'Premium paid'} \u20b9${fmt(premInfo.rs)}`
      : '';
    el.title = 'Current profit/loss' + premTip + (asOf ? ' as of ' + asOf + ' IST' : '') + '. ' + feed.tip;
  });
  _updateFeedTag(tradeId);
}

function _refreshAllFeedTags() {
  document.querySelectorAll('.live-feed-tag[data-trade-id]').forEach(el => {
    const tid = el.dataset.tradeId;
    const panel = document.getElementById(`close-${tid}`);
    const opts = {};
    if (panel) {
      const content = panel.querySelector('.close-trade-content');
      if (content && content.querySelector('.cf-live-leg')) {
        let hasPrices = false;
        content.querySelectorAll('.cf-live-price[data-ltp]').forEach(span => {
          const ltp = parseFloat(span.dataset.ltp);
          if (!isNaN(ltp) && ltp > 0) hasPrices = true;
        });
        opts.forCloseForm = true;
        opts.hasLivePrices = hasPrices && _inMarketHours();
        opts.usingEod = hasPrices && !_inMarketHours();
      }
    }
    _updateFeedTag(tid, opts);
  });
}

function _applyMtmSnapshot(snap) {
  Object.entries((snap && snap.trades) || {}).forEach(([tid, t]) => {
    if (t && t.mtm != null) _updateCurrentPnlBadge(tid, t.mtm, t.as_of, false);
    _updateLiveProfitLevels(tid, t);
  });
  _refreshAllFeedTags();
}

function _applyLegLtpsToClosePanel(tradeId, legLtps) {
  const closePanel = document.getElementById(`close-${tradeId}`);
  if (!closePanel || !legLtps) return false;
  let anyUpdated = false;
  closePanel.querySelectorAll('.cf-live-price[data-leg-key]').forEach(span => {
    const parts = span.dataset.legKey.split('|');
    if (parts.length !== 3) return;
    const ltp = _lookupLegLtp(legLtps, parts[0], parts[1], parts[2]);
    if (ltp == null || ltp <= 0) return;
    span.textContent = `\u20b9${fmt(ltp)}`;
    span.dataset.ltp = ltp;
    span.classList.add('ltp-flash');
    setTimeout(() => span.classList.remove('ltp-flash'), 600);
    anyUpdated = true;
  });
  if (!anyUpdated) return false;
  const rows = [...closePanel.querySelectorAll('.cf-live-leg')];
  let gross = 0; let allFilled = true;
  const entryTxns = [], exitTxns = [];
  rows.forEach(row => {
    const action = row.dataset.action;
    const ep = parseFloat(row.dataset.fillPrice) || 0;
    const lots = parseInt(row.dataset.lots) || 1;
    const ls = parseInt(row.dataset.lotSize) || 1;
    const cp = parseFloat(row.querySelector('.cf-live-price')?.dataset.ltp);
    if (isNaN(cp) || cp <= 0) { allFilled = false; return; }
    gross += action === 'SELL' ? (ep - cp) * lots * ls : (cp - ep) * lots * ls;
    entryTxns.push({ action, fill_price: ep, lots, lot_size: ls });
    exitTxns.push({ action: action === 'SELL' ? 'BUY' : 'SELL', fill_price: cp, lots, lot_size: ls });
  });
  const preview = closePanel.querySelector('.live-pnl-preview');
  if (preview && allFilled) {
    const charges = estChargesOneSide([...entryTxns, ...exitTxns]);
    const net = gross - charges;
    const prem = _premiumFromDataset(preview);
    _setPnlLine(preview.querySelector('.live-pnl-gross'), preview.querySelector('.live-pnl-gross-pct'), gross, prem);
    const c = preview.querySelector('.live-pnl-charges');
    if (c) c.textContent = `\u20b9${fmt(charges)}`;
    _setPnlLine(preview.querySelector('.live-pnl-value'), preview.querySelector('.live-pnl-pct'), net, prem);
  }
  return anyUpdated;
}

const $ = sel => document.querySelector(sel);
const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));
const fmt = n => (n == null ? '—' : Number(n).toLocaleString('en-IN', {maximumFractionDigits: 2}));
const fmtPct = n => (n == null ? '—' : Number(n).toFixed(1) + '%');
const fmtDt   = s => { if (!s) return '—'; try { const d = new Date(s); return d.toLocaleString('en-IN', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:false}); } catch(e) { return String(s); } };
const fmtDate = s => { if (!s) return '—'; try { const d = new Date(s); return d.toLocaleDateString('en-IN', {day:'2-digit', month:'short', year:'numeric'}); } catch(e) { return String(s); } };
// Context-chip datetime (matches Execute chip style, includes time)
const fmtChipDt = s => {
  if (!s) return null;
  try {
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleString('en-IN', {
      weekday: 'short', day: '2-digit', month: 'short', year: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
  } catch { return null; }
};

// "₹500 (67% of credit ₹750)" — shows a derived value as % of its base
const pctHint = (val, base, label = '') => {
  if (val == null || base == null || base === 0) return '';
  const p = (Math.abs(val) / Math.abs(base) * 100).toFixed(0);
  const lbl = label ? `${label} ` : '';
  return `<span class="pct-hint"> (${p}% of ${lbl}₹${fmt(base)})</span>`;
};

/** Total premium received (credit) or paid (debit) in ₹ from executed leg fills. */
function tradePremiumFromLegs(legs) {
  const exec = (legs || []).filter(l => l.executed && l.fill_price != null);
  if (!exec.length) return null;
  let total = 0;
  exec.forEach(l => {
    const lots = l.lots_actual || l.lots || 0;
    const qty = lots * (l.lot_size || 1);
    const sign = l.action === 'SELL' ? 1 : -1;
    total += sign * parseFloat(l.fill_price) * qty;
  });
  if (!total) return null;
  return {
    rs: Math.abs(total),
    kind: total > 0 ? 'received' : 'paid',
  };
}

/** Per-leg premium basis for leg-level P&L %. */
function legPremiumFromLeg(l) {
  if (l.fill_price == null) return null;
  const lots = l.lots_actual || l.lots || 0;
  const qty = lots * (l.lot_size || 1);
  if (!qty) return null;
  const sign = l.action === 'SELL' ? 1 : -1;
  const total = sign * parseFloat(l.fill_price) * qty;
  if (!total) return null;
  return { rs: Math.abs(total), kind: total > 0 ? 'received' : 'paid' };
}

function premiumInfoFromTrade(t) {
  if (!t) return null;
  if (t.premium_rs > 0) {
    return { rs: parseFloat(t.premium_rs), kind: t.premium_kind || 'paid' };
  }
  return tradePremiumFromLegs(t.legs || []);
}

/** Premium basis from signed total credit/debit (positive = received, negative = paid). */
function premiumFromCreditTotal(totalCredit) {
  const n = parseFloat(totalCredit);
  if (!n || isNaN(n)) return null;
  return { rs: Math.abs(n), kind: n > 0 ? 'received' : 'paid' };
}

function premiumFromSuggestion(s) {
  if (!s) return null;
  const legs = s.legs || [];
  if (legs.length) {
    let total = 0;
    legs.forEach(l => {
      const qty = (l.lots || 1) * (l.lot_size || 1);
      const price = parseFloat(l.suggested_price || l.suggested_price_low || 0);
      total += (l.action === 'SELL' ? 1 : -1) * price * qty;
    });
    if (total) return premiumFromCreditTotal(total);
  }
  if (s.net_credit != null) {
    const qty = legs[0] ? (legs[0].lots || 1) * (legs[0].lot_size || 1) : 1;
    return premiumFromCreditTotal(parseFloat(s.net_credit) * qty);
  }
  return null;
}

function _premiumLabel(kind, aggregate = false) {
  if (aggregate) return 'premium deployed';
  return kind === 'received' ? 'premium received' : 'premium paid';
}

/** Bracket beside P&L: "(+26.3% · premium paid ₹18,450)" */
function pnlPctBracket(pnl, premiumInfo, { aggregate = false } = {}) {
  if (pnl == null || isNaN(pnl) || !premiumInfo || !premiumInfo.rs) return '';
  const pct = (pnl / premiumInfo.rs) * 100;
  const sign = pct >= 0 ? '+' : '';
  const kindLabel = _premiumLabel(premiumInfo.kind, aggregate);
  return `<span class="pnl-pct-bracket"> (${sign}${pct.toFixed(1)}% · ${kindLabel} \u20b9${fmt(premiumInfo.rs)})</span>`;
}

function formatPnlPctText(pnl, premiumInfo, { aggregate = false } = {}) {
  if (pnl == null || isNaN(pnl) || !premiumInfo || !premiumInfo.rs) return '';
  const pct = (pnl / premiumInfo.rs) * 100;
  const sign = pct >= 0 ? '+' : '';
  return ` (${sign}${pct.toFixed(1)}% · ${_premiumLabel(premiumInfo.kind, aggregate)} \u20b9${fmt(premiumInfo.rs)})`;
}

function formatPnlWithPct(pnl, premiumInfo, { useGrossSign = true, aggregate = false } = {}) {
  if (pnl == null || isNaN(pnl)) return '—';
  const prefix = useGrossSign && pnl > 0 ? '+' : (useGrossSign && pnl < 0 ? '\u2212' : '');
  const absTxt = '\u20b9' + fmt(Math.abs(pnl));
  return `${prefix}${absTxt}${pnlPctBracket(pnl, premiumInfo, { aggregate })}`;
}

function _setPnlLine(valueEl, pctEl, amount, premiumInfo) {
  if (!valueEl) return;
  if (amount == null || isNaN(amount)) {
    valueEl.textContent = '\u2014';
    if (pctEl) pctEl.innerHTML = '';
    return;
  }
  const prefix = amount >= 0 ? '' : '\u2212';
  valueEl.textContent = prefix + '\u20b9' + fmt(Math.abs(amount));
  const posNeg = amount >= 0 ? ' pnl-pos' : ' pnl-neg';
  valueEl.className = valueEl.className.replace(/pnl-pos|pnl-neg/g, '').trim() + posNeg;
  if (pctEl) {
    pctEl.innerHTML = premiumInfo ? pnlPctBracket(amount, premiumInfo) : '';
    pctEl.className = 'pnl-pct-bracket muted' + posNeg;
  }
}

function _premiumFromDataset(el) {
  if (!el) return null;
  const rs = parseFloat(el.dataset.premiumRs);
  if (!rs || isNaN(rs)) return null;
  return { rs, kind: el.dataset.premiumKind || 'paid' };
}

function formatLegPnlHtml(l) {
  if (l.leg_pnl == null) return '';
  const prem = legPremiumFromLeg(l);
  const cls = l.leg_pnl >= 0 ? 'pnl-profit' : 'pnl-loss';
  return `<span class="${cls}">P&amp;L: ${formatPnlWithPct(l.leg_pnl, prem)}</span>`;
}

function _perfPnlHtml(pnl, premiumRs, premiumKind, { aggregate = false } = {}) {
  if (pnl == null) return '—';
  const prem = premiumRs > 0 ? { rs: premiumRs, kind: premiumKind || 'paid' } : null;
  return formatPnlWithPct(pnl, prem, { aggregate });
}
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
window.toast = toast;

// ---------------- Tab switching ----------------
const TABS = ['suggestion', 'trades', 'history', 'logs', 'jobs', 'wsmon', 'notifications', 'config'];
const TAB_LOADERS = {};
const TAB_LEAVE = {};

/** Extension point for separate modules (e.g. Intraday Scout). */
function registerDashboardTab(name, onEnter, onLeave) {
  if (!TABS.includes(name)) TABS.push(name);
  if (onEnter) TAB_LOADERS[name] = onEnter;
  if (onLeave) TAB_LEAVE[name] = onLeave;
}
window.registerDashboardTab = registerDashboardTab;

function switchTab(name) {
  const prev = TABS.find(t => {
    const panel = document.getElementById(`panel-${t}`);
    return panel && panel.classList.contains('active');
  });
  if (prev && TAB_LEAVE[prev]) TAB_LEAVE[prev]();
  TABS.forEach(t => {
    const panel = document.getElementById(`panel-${t}`);
    if (!panel) return;
    panel.classList.toggle('active', t === name);
    panel.setAttribute('aria-hidden', t !== name);
  });
  $$('.nav-item, .bnav-item').forEach(b => {
    const tab = b.dataset.tab;
    const isBottomNav = b.classList.contains('bnav-item');
    const active = tab === name
      || (isBottomNav && tab === 'scout-signals' && name.startsWith('scout-'));
    b.classList.toggle('active', active);
  });
  if (name === 'suggestion')    loadSuggestion();
  if (name === 'trades')        loadTrades();
  if (name === 'history')       loadHistory();
  if (name === 'logs')          loadLogs();
  if (name === 'jobs')          loadJobs();
  if (name === 'wsmon')         loadWsMonitor();
  if (name === 'notifications') loadNotifications();
  if (name === 'config')        loadConfig();
  if (TAB_LOADERS[name])        TAB_LOADERS[name]();
  if (name.startsWith('scout-')) {
    const sec = document.getElementById('nav-section-scout');
    if (sec) sec.open = true;
  } else if (TABS.includes(name)) {
    const sec = document.getElementById('nav-section-options');
    if (sec) sec.open = true;
  }
  // Stop jobs auto-refresh when leaving the tab
  if (name !== 'jobs')  stopJobsAutoRefresh();
  if (name !== 'wsmon') stopWsMonitorAutoRefresh();
  try { localStorage.setItem('activeTab', name); } catch (_) {}
  try {
    if (window.location.hash !== '#' + name) {
      history.replaceState(null, '', '#' + name);
    }
  } catch (_) {}
}
window.switchTab = switchTab;

$$('.nav-item, .bnav-item').forEach(b =>
  b.addEventListener('click', () => switchTab(b.dataset.tab))
);
// Restore last active tab on page load (URL hash takes precedence over localStorage).
// MUST defer until after the whole script parses, otherwise switchTab() touches
// `let` bindings (_histActiveSubtab, _jobsTimer, _wsmonTimer) declared further
// down → TDZ ReferenceError that breaks Jobs/Logs/Wsmon tabs.
function _restoreActiveTab() {
  let initial = null;
  const hash = (window.location.hash || '').replace(/^#/, '');
  if (hash === 'scout') initial = 'scout-signals';
  else if (hash && TABS.includes(hash)) initial = hash;
  if (!initial) {
    try {
      const saved = localStorage.getItem('activeTab');
      if (saved === 'scout') initial = 'scout-signals';
      else if (saved && TABS.includes(saved)) initial = saved;
    } catch (_) {}
  }
  if (initial) switchTab(initial);
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _restoreActiveTab);
} else {
  // Script already parsed (e.g. defer/module): run on next tick so all top-level
  // `let`/`const` declarations below this point have been initialized.
  setTimeout(_restoreActiveTab, 0);
}

// ---------------- Notifications (badge only — full panel is the Alerts tab) ----------------
async function refreshNotifBadge() {
  // Kept for compatibility with refreshGlobalBanners calls; actual badge is
  // now on the sidebar Alerts tab nav item via _nfRefreshStats().
  try {
    const data = await API('/api/notifications?unread=1');
    return data.notifications || [];
  } catch { return []; }
}

// ---------------- Header index spot strip ----------------
function _fmtIndexPrice(sym, price) {
  if (price == null || isNaN(price)) return '—';
  if (sym === 'VIX') return Number(price).toFixed(2);
  return Number(price).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function _indexSpotTitle(item) {
  if (!item) return '';
  if (item.source === 'live') {
    return `${item.label} live${item.as_of ? ` · ${item.as_of} IST` : ''}`;
  }
  if (item.source === 'eod' && item.trade_date) {
    return `${item.label} EOD close · ${item.trade_date}`;
  }
  return `${item.label} — no price data`;
}

async function refreshIndexSpotStrip() {
  const host = document.getElementById('index-spot-strip');
  if (!host) return;
  try {
    const data = await API('/api/indices/spot');
    const items = data.indices || [];
    if (!items.length) {
      host.innerHTML = '';
      return;
    }
    host.innerHTML = items.map(item => {
      const src = (item.source || '').toLowerCase();
      const cls = src === 'live' ? 'idx-chip-live'
        : src === 'eod' ? 'idx-chip-eod'
        : 'idx-chip-unavailable';
      const srcLabel = src === 'live' ? 'Live'
        : src === 'eod' ? 'EOD'
        : '—';
      const tip = _indexSpotTitle(item);
      return `<div class="idx-chip ${cls}" title="${escapeHtml(tip)}">`
        + `<span class="idx-chip-label">${escapeHtml(item.label || item.symbol || '')}</span>`
        + `<span class="idx-chip-price">${escapeHtml(_fmtIndexPrice(item.symbol, item.price))}</span>`
        + `<span class="idx-chip-src">${srcLabel}</span>`
        + `</div>`;
    }).join('');
  } catch {
    /* keep last rendered values on transient errors */
  }
}

// ---------------- Global banners (header strip) ----------------
// System flags (kill switch / circuit breaker / trade execution) and unread
// CRITICAL/WARNING notification summaries live in a single #global-banners
// container immediately under the page header so they are visible on EVERY
// tab — not just the Suggestion tab. Called on page load and after any
// action that could change banner state (notification mark-as-read, flag
// toggle, etc.).
async function refreshGlobalBanners() {
  const host = document.getElementById('global-banners');
  if (!host) return;
  const banners = [];
  // Compact pill format: each banner is a single line with a short label
  // + actionable button. The `title` attribute carries the full explanation
  // so hover/tap-and-hold still surfaces the long form.
  try {
    const st = await API('/api/system-status');
    if (st.circuit_breaker_active) {
      banners.push(`<div class="sys-banner sys-banner-err" title="Daily P&L circuit breaker is ACTIVE — new executions are blocked until reset. This flag re-triggers tonight at 20:50 IST if aggregate open-trade losses still breach the limit.">\ud83d\udea8 P&amp;L breaker <strong>ACTIVE</strong> <button type="button" class="btn btn-ghost btn-flag-reset" data-flag-key="circuit_breaker_active" data-flag-value="false">Reset</button></div>`);
    }
    if (st.kill_switch) {
      banners.push(`<div class="sys-banner sys-banner-err" title="Kill switch is ON — all alerts and execution are paused.">\ud83d\uded1 Kill switch <strong>ON</strong> <button type="button" class="btn btn-ghost btn-flag-reset" data-flag-key="kill_switch" data-flag-value="false">Disable</button></div>`);
    }
    if (st.trade_execution_enabled === false) {
      banners.push(`<div class="sys-banner sys-banner-warn" title="Trade execution disabled by runtime flag. Suggestions are still generated but cannot auto-execute until you re-enable.">\u26a0\ufe0f Execution <strong>OFF</strong> <button type="button" class="btn btn-ghost btn-flag-reset" data-flag-key="trade_execution_enabled" data-flag-value="true">Enable</button></div>`);
    }
  } catch {}
  try {
    const nd = await API('/api/notifications?unread=1');
    const unread = nd.notifications || [];
    const crit = unread.filter(n => (n.severity || '').toUpperCase() === 'CRITICAL');
    const warn = unread.filter(n => (n.severity || '').toUpperCase() === 'WARNING');
    if (crit.length) {
      const titles = crit.slice(0, 3).map(n => (n.title || '')).join(' • ');
      const more = crit.length > 3 ? ` (+${crit.length - 3} more)` : '';
      const tt = escapeHtml(`${crit.length} CRITICAL alert(s): ${titles}${more}`);
      banners.push(`<div class="sys-banner sys-banner-err" title="${tt}">\ud83d\udea8 <strong>${crit.length}</strong> critical <button type="button" class="btn btn-ghost" id="open-notif-from-banner">Open</button></div>`);
    } else if (warn.length) {
      const titles = warn.slice(0, 3).map(n => (n.title || '')).join(' • ');
      const more = warn.length > 3 ? ` (+${warn.length - 3} more)` : '';
      const tt = escapeHtml(`${warn.length} WARNING(s): ${titles}${more}`);
      banners.push(`<div class="sys-banner sys-banner-warn" title="${tt}">\u26a0\ufe0f <strong>${warn.length}</strong> warning <button type="button" class="btn btn-ghost" id="open-notif-from-banner">Open</button></div>`);
    }
  } catch {}
  host.innerHTML = banners.join('');
  bindFlagResetButtons();
  // Wire the "Open" button in critical/warning banners to switch to the Alerts tab.
  const openBtn = document.getElementById('open-notif-from-banner');
  if (openBtn) openBtn.addEventListener('click', () => switchTab('notifications'));
}

// ---------------- Tab 1: Suggestion ----------------
function renderMarketSitOutSummary(summary) {
  if (!summary) return '';
  return `<div class="sit-out-summary" role="status">
    <div class="sit-out-summary-head">
      <span class="tag tag-info">CAPITAL PRESERVATION</span>
      <strong>${escapeHtml(summary.title)}</strong>
    </div>
    <p class="sit-out-summary-body">${escapeHtml(summary.summary)}</p>
    <p class="sit-out-summary-note muted">${escapeHtml(summary.profit_note)}</p>
  </div>`;
}

function renderSitOutCard(s) {
  const regime = s.market_regime || {};
  const regimeTitle = regime.title ? escapeHtml(regime.title) : 'Sitting out';
  const reason = s.no_suggestion_reason || s.reason || '';
  const confLabel = s.confidence_display || formatConfidence(s) || `${s.confidence_score || '—'} passed`;
  return `<div class="card sit-out-card">
    <div class="card-head">
      <h3>${escapeHtml(s.underlying)} — No trade today</h3>
      <span class="tag tag-warn">SITTING OUT</span>
    </div>
    <p class="sit-out-regime"><strong>${regimeTitle}</strong></p>
    <p class="muted sit-out-reason">${escapeHtml(reason)}</p>
    <p class="muted sit-out-conf" style="font-size:.85rem">Confidence: ${escapeHtml(confLabel)}</p>
    <p class="muted sit-out-hint" style="font-size:.82rem">Gates unchanged — the engine waits for a high-edge setup rather than forcing a marginal trade.</p>
  </div>`;
}

function regimePairTitle(s) {
  if (s.regime_pair_type === 'range') {
    return 'If market stays in range';
  }
  if (s.regime_pair_type === 'breakout') {
    return 'If market moves sharply up or down';
  }
  return '';
}

function groupRegimePairSuggestions(list) {
  const groups = new Map();
  const singles = [];
  for (const s of list || []) {
    const gid = s.regime_pair_group;
    if (!gid) {
      singles.push({ type: 'single', item: s });
      continue;
    }
    if (!groups.has(gid)) {
      groups.set(gid, { type: 'pair', group: gid, items: [] });
    }
    groups.get(gid).items.push(s);
  }
  const out = [...groups.values(), ...singles];
  out.sort((a, b) => {
    const au = (a.items && a.items[0]?.underlying) || a.item?.underlying || '';
    const bu = (b.items && b.items[0]?.underlying) || b.item?.underlying || '';
    return au.localeCompare(bu);
  });
  return out;
}

function wrapCollapsibleCard(summaryHtml, bodyHtml, { open = false, className = '', attrs = '' } = {}) {
  const openAttr = open ? ' open' : '';
  const cls = className ? ` ${className}` : '';
  return `<details class="card collapsible-card${cls}"${openAttr}${attrs ? ` ${attrs}` : ''}>
    <summary class="collapsible-summary">${summaryHtml}</summary>
    <div class="collapsible-body">${bodyHtml}</div>
  </details>`;
}

function bindCollapsibleCardInteractions(root) {
  const scope = root || document;
  scope.querySelectorAll('.collapsible-card summary button, .collapsible-card summary a, .collapsible-card summary input, .collapsible-card summary select, .collapsible-card summary textarea, .collapsible-card summary label').forEach(el => {
    if (el.dataset.collapseBound === '1') return;
    el.dataset.collapseBound = '1';
    el.addEventListener('click', e => e.stopPropagation());
  });
  scope.querySelectorAll('.collapsible-card summary .btn, .collapsible-card summary .card-head-btn').forEach(el => {
    if (el.dataset.collapseBound === '2') return;
    el.dataset.collapseBound = '2';
    el.addEventListener('mousedown', e => e.stopPropagation());
  });
}

function renderRegimePairGroup(group, startCardIdx = 0) {
  const items = (group.items || []).slice().sort((a, b) => {
    if (a.regime_pair_type === 'range') return -1;
    if (b.regime_pair_type === 'range') return 1;
    return 0;
  });
  const preferred = items.find(x => x.regime_pair_preferred);
  const reason = preferred?.regime_pair_preference_reason
    || items[0]?.regime_pair_preference_reason
    || '';
  const underlying = items[0]?.underlying || '';
  const header = `<div class="regime-pair-header">
    <div class="regime-pair-headline">
      <span class="tag tag-info">TWO SCENARIOS</span>
      <strong>${escapeHtml(underlying)} — pick the scenario you believe in</strong>
    </div>
    ${reason ? `<p class="regime-pair-pref muted">${escapeHtml(reason)}</p>` : ''}
    <p class="regime-pair-hint muted">Take only one of these — they bet on opposite outcomes.</p>
  </div>`;
  const cards = items.map((s, i) => {
    const prefBadge = s.regime_pair_preferred
      ? '<span class="tag tag-ok regime-pair-pref-badge">System preferred</span>'
      : '';
    const scenario = regimePairTitle(s);
    return `<div class="regime-pair-card${s.regime_pair_preferred ? ' regime-pair-preferred' : ''}">
      <div class="regime-pair-scenario">
        <strong>${escapeHtml(scenario)}</strong>
        ${prefBadge}
      </div>
      ${renderSuggestion(s, false, items, false, startCardIdx + i === 0)}
    </div>`;
  }).join('');
  return `<div class="regime-pair-group">${header}<div class="regime-pair-cards">${cards}</div></div>`;
}

function renderSuggestionList(list) {
  let cardIdx = 0;
  return groupRegimePairSuggestions(list).map(entry => {
    if (entry.type === 'pair') {
      const html = renderRegimePairGroup(entry, cardIdx);
      cardIdx += (entry.items || []).length;
      return html;
    }
    const html = renderSuggestion(entry.item, false, list, false, cardIdx === 0);
    cardIdx += 1;
    return html;
  }).join('');
}

async function loadSuggestion() {
  const c = $('#suggestion-container');
  c.className = 'loading'; c.textContent = 'Loading…';
  // Refresh global banners on every tab visit (catches new alerts the user
  // might have triggered elsewhere). Fire-and-forget — never blocks.
  refreshGlobalBanners();
  try {
    const data = await API('/api/suggestion/today');
    const list = data.suggestions || [];
    const sitOut = data.sit_out || [];
    const actionable = list.filter(s => {
      const st = (s.status || '').toUpperCase();
      return st === 'PENDING' && s.execution_gate && s.execution_gate.ok;
    });
    const parts = [];
    if (data.market_summary && (sitOut.length || !actionable.length)) {
      parts.push(renderMarketSitOutSummary(data.market_summary));
    }
    if (actionable.length) {
      parts.push(renderSuggestionList(actionable));
    }
    if (sitOut.length) {
      parts.push(sitOut.map(s => renderSitOutCard(s)).join(''));
    }
    if (!parts.length) {
      c.className = '';
      c.innerHTML = '<div class="empty">No actionable suggestion right now. Run <strong>Live Suggestion Engine</strong> from the Jobs tab during market hours — ignored, expired, and blocked signals are hidden.</div>';
      return;
    }
    c.className = '';
    c.innerHTML = parts.join('');
    bindSuggestionActions();
    bindCollapsibleCardInteractions(c);
  } catch (e) {
    c.className = ''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

// ── Per-strategy MTM SL (mirrors config.py strategy_sl_limits) ──
const STRATEGY_SL_DEFAULTS = { loss_fraction: 0.50, absolute_cap_rs: 10000, cap_min_max_loss_rs: null };
const STRATEGY_SL_LIMITS = {
  LONG_STRADDLE:    { loss_fraction: 0.30, absolute_cap_rs: 10000 },
  LONG_STRANGLE:    { loss_fraction: 0.30, absolute_cap_rs: 10000 },
  LONG_CALL:        { loss_fraction: 0.30, absolute_cap_rs: 10000 },
  LONG_PUT:         { loss_fraction: 0.30, absolute_cap_rs: 10000 },
  IRON_CONDOR:      { loss_fraction: 0.50, absolute_cap_rs: 15000, cap_min_max_loss_rs: 20000 },
  IRON_BUTTERFLY:   { loss_fraction: 0.50, absolute_cap_rs: 15000, cap_min_max_loss_rs: 20000 },
  BEAR_CALL_SPREAD: { loss_fraction: 0.50, absolute_cap_rs: 15000, cap_min_max_loss_rs: 20000 },
  BULL_PUT_SPREAD:  { loss_fraction: 0.40, absolute_cap_rs: 10000 },
  BEAR_PUT_SPREAD:  { loss_fraction: 0.40, absolute_cap_rs: 10000 },
  BULL_CALL_SPREAD: { loss_fraction: 0.50, absolute_cap_rs: 10000 },
  JADE_LIZARD:      { loss_fraction: 0.40, absolute_cap_rs: 10000 },
  CALENDAR_SPREAD:  { loss_fraction: 0.50, absolute_cap_rs: 10000 },
};

function strategySlConfig(strategy) {
  const base = STRATEGY_SL_LIMITS[strategy] || STRATEGY_SL_DEFAULTS;
  return { ...STRATEGY_SL_DEFAULTS, ...base };
}

function capApplies(cfg, maxLossRs) {
  if (cfg.absolute_cap_rs == null) return false;
  if (cfg.cap_min_max_loss_rs != null && maxLossRs < cfg.cap_min_max_loss_rs) return false;
  return true;
}

function effectiveSlRs(strategy, maxLossRs) {
  if (maxLossRs == null || maxLossRs <= 0) return null;
  const cfg = strategySlConfig(strategy);
  const pct = maxLossRs * cfg.loss_fraction;
  if (capApplies(cfg, maxLossRs) && pct > cfg.absolute_cap_rs) return cfg.absolute_cap_rs;
  return pct;
}

function slExitPlanText(strategy, maxLossRs) {
  const cfg = strategySlConfig(strategy);
  const slRs = effectiveSlRs(strategy, maxLossRs);
  const pctLabel = `${Math.round(cfg.loss_fraction * 100)}%`;
  let capDesc = 'no cap';
  if (cfg.absolute_cap_rs != null) {
    capDesc = cfg.cap_min_max_loss_rs != null
      ? `₹${fmt(cfg.absolute_cap_rs)} cap when max loss ≥ ₹${fmt(cfg.cap_min_max_loss_rs)}`
      : `₹${fmt(cfg.absolute_cap_rs)} cap`;
  }
  if (slRs == null) return `exit on MTM loss (${pctLabel} of max loss, ${capDesc})`;
  return `exit if MTM loss reaches ₹${fmt(slRs)} (${pctLabel} of max loss; ${capDesc})`;
}

// ── Live risk monitor levels (mirrors config.py live_risk_monitor) ─────────
const LIVE_RISK_MONITOR = {
  target_fraction_at_min_dte: 0.50,
  target_fraction_at_max_dte: 0.80,
  target_min_dte: 3,
  target_max_dte: 15,
  trailing_sl_steps: [[0.50, 0.0], [0.80, 0.40]],
  pre_breach_fraction: 0.70,
};

function dteFromExpiry(expiryDateStr) {
  if (!expiryDateStr) return null;
  const s = String(expiryDateStr).slice(0, 10);
  const parts = s.split('-');
  if (parts.length !== 3) return null;
  const exp = new Date(+parts[0], +parts[1] - 1, +parts[2]);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.max(0, Math.round((exp - today) / 86400000));
}

function liveTargetFraction(dte) {
  const c = LIVE_RISK_MONITOR;
  if (dte == null) return c.target_fraction_at_max_dte;
  if (dte <= c.target_min_dte) return c.target_fraction_at_min_dte;
  if (dte >= c.target_max_dte) return c.target_fraction_at_max_dte;
  const span = c.target_max_dte - c.target_min_dte;
  if (span <= 0) return c.target_fraction_at_max_dte;
  const f = (dte - c.target_min_dte) / span;
  return c.target_fraction_at_min_dte
    + f * (c.target_fraction_at_max_dte - c.target_fraction_at_min_dte);
}

function renderLiveProfitLevels(t) {
  const sug = t.suggestion || {};
  const premium = tradePremiumFromLegs(t.legs || []);
  const premAttrs = premium
    ? ` data-premium-rs="${premium.rs}" data-premium-kind="${premium.kind}"`
    : '';
  const currentRow = `
        <div class="lpl-row lpl-current-row live-mtm" data-trade-id="${escapeHtml(t.trade_id)}"${premAttrs}>
          <span class="lpl-label">Current P&amp;L</span>
          <span class="lpl-val-line">
            <strong class="cpnl-val lpl-current-val">\u2014</strong><span class="cpnl-pct-bracket lpl-current-pct muted"></span>
          </span>
          <span class="muted lpl-note">Live MTM vs entry fills</span>
        </div>`;
  const mpRaw = t.actual_max_profit != null ? t.actual_max_profit
              : (sug.max_profit != null ? sug.max_profit : null);
  const mp = mpRaw != null ? parseFloat(mpRaw) : null;
  if (mp == null || mp <= 0 || isNaN(mp)) {
    return `
    <div class="live-profit-levels live-profit-levels--mtm-only" data-trade-id="${escapeHtml(t.trade_id)}"${premAttrs}>
      <div class="sl-monitor-label">Live P&amp;L</div>
      <div class="lpl-grid">${currentRow}</div>
    </div>`;
  }
  const expiry = sug.expiry_date;
  const dte = dteFromExpiry(expiry);
  const tgtFrac = liveTargetFraction(dte);
  const targetRs = Math.round(mp * tgtFrac);
  const strat = sug.strategy || '';
  const mlRaw = t.actual_max_loss != null ? t.actual_max_loss
              : (sug.max_loss != null ? sug.max_loss : null);
  const ml = mlRaw != null ? parseFloat(mlRaw) : null;
  const lossRs = effectiveSlRs(strat, ml);
  const stepIdx = parseInt(t.trailing_step_idx || 0, 10);
  const floor = t.trailing_pnl_floor != null ? parseFloat(t.trailing_pnl_floor) : null;
  const steps = LIVE_RISK_MONITOR.trailing_sl_steps || [];
  const nextStep = stepIdx < steps.length ? steps[stepIdx] : null;

  let floorValHtml;
  let floorNote;
  if (floor != null) {
    floorValHtml = `<span class="lpl-val lpl-floor live-profit-floor-val">\u20b9${fmt(floor)}</span>`;
    floorNote = 'Trailing lock — alert if MTM falls below (always below target)';
  } else if (nextStep) {
    const trigRs = Math.round(mp * nextStep[0]);
    const lockRs = Math.round(mp * nextStep[1]);
    floorValHtml = `<span class="lpl-val muted live-profit-floor-val">Not armed</span>`;
    floorNote = `Arms at \u20b9${fmt(trigRs)} profit (${Math.round(nextStep[0] * 100)}%) \u2192 floor \u20b9${fmt(lockRs)}`;
  } else {
    floorValHtml = `<span class="lpl-val muted live-profit-floor-val">\u2014</span>`;
    floorNote = 'All trailing steps armed';
  }

  const dteLabel = dte != null ? `, DTE ${dte}` : '';
  const floorAttr = floor != null ? String(floor) : '';
  return `
    <div class="live-profit-levels" data-trade-id="${escapeHtml(t.trade_id)}"
         data-max-profit="${mp}"
         data-max-loss="${ml != null && !isNaN(ml) ? ml : ''}"
         data-strategy="${escapeHtml(strat)}"
         data-expiry="${expiry ? escapeHtml(String(expiry).slice(0, 10)) : ''}"
         data-trailing-floor="${floorAttr}">
      <div class="sl-monitor-label">Live profit levels</div>
      <div class="lpl-grid">
        ${currentRow}
        <div class="lpl-row lpl-target-row">
          <span class="lpl-label">Target profit</span>
          <span class="lpl-val-line">
            <span class="lpl-val lpl-target">\u20b9${fmt(targetRs)}</span>
            <span class="lpl-status lpl-status-target" hidden></span>
          </span>
          <span class="muted lpl-note">Exit goal — ${Math.round(tgtFrac * 100)}% of max profit${dteLabel}</span>
        </div>
        <div class="lpl-row lpl-floor-row">
          <span class="lpl-label">Profit floor</span>
          <span class="lpl-val-line">
            ${floorValHtml}
            <span class="lpl-status lpl-status-floor" hidden></span>
          </span>
          <span class="muted lpl-note live-profit-floor-note">${floorNote}</span>
        </div>
        <div class="lpl-row lpl-loss-row">
          <span class="lpl-label">Loss limit</span>
          <span class="lpl-val-line">
            <span class="lpl-val lpl-loss">${lossRs != null ? `\u20b9${fmt(lossRs)} loss` : '\u2014'}</span>
            <span class="lpl-status lpl-status-loss" hidden></span>
          </span>
          <span class="muted lpl-note">${lossRs != null ? slExitPlanText(strat, ml) : 'Set max loss on trade'}</span>
        </div>
      </div>
      <div class="lpl-foot muted">Labels show <strong>current</strong> breach only — they clear when MTM recovers. Alerts stay in the Notifications tab.</div>
    </div>`;
}

function _fmtMtmSigned(v, premiumInfo) {
  if (v == null || isNaN(v)) return '—';
  const n = Math.round(v);
  const base = (n >= 0 ? '+₹' : '−₹') + fmt(Math.abs(n));
  return base + formatPnlPctText(v, premiumInfo);
}

function _computeTradeActionInstruction(opts) {
  const {
    liveMtm, lossHit, floorBreach, targetHit, preBreachNear,
    lossRs, targetRs, floor, strategy,
    dailyStatus, exitInstruction, riskNotif,
    premiumInfo,
  } = opts;
  const ds = (dailyStatus || '').toUpperCase();
  const rn = (riskNotif || '').toUpperCase();

  if (ds === 'EXIT_AT_OPEN' || ds === 'EXIT') {
    return {
      tone: 'critical',
      verb: 'CLOSE',
      title: 'Exit today — EOD / daily rule',
      instruction: exitInstruction
        || 'The exit engine flagged this trade for exit. Close all legs when you can.',
      why: `Daily status: ${dailyStatus}`,
      cta: 'Use Close Trade below and record exit prices.',
    };
  }
  if (lossHit || rn === 'LOSS_LIMIT_HIT' || rn === 'THESIS_FAIL' || rn === 'EXIT_THESIS_FAIL') {
    return {
      tone: 'critical',
      verb: 'CLOSE NOW',
      title: rn.includes('THESIS') ? 'Close — thesis failed' : 'Close the entire trade',
      instruction: rn.includes('THESIS')
        ? 'Long-vol thesis window closed (near expiry, still losing). Exit the full position.'
        : 'Buy back every short leg and sell every long leg — exit the full position. '
        + 'This is your MTM stop loss.',
      why: liveMtm != null && lossRs != null
        ? `Live MTM ${_fmtMtmSigned(liveMtm, premiumInfo)} · loss limit −₹${fmt(lossRs)}`
        : 'Loss limit alert is active.',
      cta: 'Tap Close Trade below → enter live exit prices → confirm.',
    };
  }
  if (rn === 'SL_TRIGGER') {
    const sideHint = ['IRON_CONDOR', 'IRON_BUTTERFLY'].includes(strategy)
      ? 'Close only the spread that spot breached (call side if Nifty rallied, put side if it fell), '
        + 'or close the whole trade if unsure.'
      : 'Spot crossed your stored SL level — close the threatened side or the full trade.';
    return {
      tone: 'critical',
      verb: 'CLOSE',
      title: 'Spot stop triggered',
      instruction: sideHint,
      why: 'Underlying price hit the stop-loss level in Stop-loss monitor.',
      cta: 'Use Close Trade below to exit.',
    };
  }
  if (rn === 'SHORT_LEG_STRESS') {
    return {
      tone: 'warn',
      verb: 'REVIEW',
      title: 'Short leg under stress',
      instruction: 'A short leg premium has risen to 2× entry or more. Check whole-trade MTM — '
        + 'this is an early risk warning, not a take-profit signal.',
      why: liveMtm != null
        ? `Live MTM ${_fmtMtmSigned(liveMtm, premiumInfo)} · one short leg is blowing up`
        : 'Short-leg stress alert is active.',
      cta: 'Review MTM and decide whether to reduce or exit.',
    };
  }
  if (floorBreach || rn === 'PROFIT_FLOOR_HIT') {
    return {
      tone: 'warn',
      verb: 'CLOSE',
      title: 'Close to protect profit',
      instruction: 'MTM dropped below your trailing profit floor. Exit now if you accept the '
        + '"lock gains" rule — don\'t wait for target again.',
      why: liveMtm != null && floor != null
        ? `Live MTM ${_fmtMtmSigned(liveMtm, premiumInfo)} · floor ₹${fmt(floor)} · target ₹${fmt(targetRs || 0)}`
        : 'Profit floor breach is active.',
      cta: 'Use Close Trade below.',
    };
  }
  if (targetHit || rn === 'TARGET_HIT') {
    return {
      tone: 'ok',
      verb: 'TAKE PROFIT',
      title: 'Target reached — your call',
      instruction: 'You may close now to lock profit, or hold for more if you still like the setup. '
        + 'No forced exit on target alone.',
      why: liveMtm != null && targetRs != null
        ? `Live MTM ${_fmtMtmSigned(liveMtm, premiumInfo)} · target ₹${fmt(targetRs)}`
        : 'Target alert is active.',
      cta: 'Optional: Close Trade below to book profit.',
    };
  }
  if (preBreachNear || rn === 'PRE_BREACH_WARNING') {
    return {
      tone: 'watch',
      verb: 'INFO ONLY',
      title: 'Approaching loss limit — monitor only',
      instruction: 'Informational early warning. Do NOT exit on this alone — wait for '
        + 'LOSS LIMIT HIT, THESIS FAIL, or an explicit sell signal from the system.',
      why: liveMtm != null && lossRs != null
        ? `Live MTM ${_fmtMtmSigned(liveMtm, premiumInfo)} · loss limit −₹${fmt(lossRs)}`
        : 'Pre-breach warning fired today.',
      cta: 'No mandatory action.',
    };
  }
  if (rn === 'ADVERSE_MOVE_WARNING' || rn === 'EXIT_ADVERSE_MOVE_WARNING') {
    return {
      tone: 'watch',
      verb: 'INFO ONLY',
      title: 'Adverse move — EOD advisory',
      instruction: 'End-of-day heads-up only. Long-vol and credit trades: do not treat this '
        + 'as a mandatory exit unless loss limit or thesis-fail fires.',
      why: 'From the daily exit engine (not a live stop).',
      cta: 'No mandatory action.',
    };
  }
  if (exitInstruction && /exit|close/i.test(exitInstruction)) {
    return {
      tone: 'warn',
      verb: 'REVIEW',
      title: 'EOD exit note on this trade',
      instruction: exitInstruction,
      why: 'From the daily exit engine (not live tick).',
      cta: 'Compare with live levels below, then decide.',
    };
  }
  return {
    tone: 'hold',
    verb: 'HOLD',
    title: 'No action required',
    instruction: '',
    why: liveMtm != null
      ? `Live MTM ${_fmtMtmSigned(liveMtm, premiumInfo)}`
        + (lossRs != null ? ` · loss limit −₹${fmt(lossRs)}` : '')
        + (targetRs != null ? ` · target ₹${fmt(targetRs)}` : '')
      : 'Waiting for live MTM during market hours.',
    cta: '',
  };
}

function renderTapLegendPopover() {
  return `<button type="button" class="tap-legend-btn" aria-label="What the colors mean">i</button>
    <span class="tap-legend-popover" role="tooltip">
      <p>Stay in the trade. Red = close at loss limit · Amber floor = protect profit · Green target = optional take profit.</p>
      <p class="muted" style="margin:.35rem 0 0">Monitor — Close Trade below when you choose to exit.</p>
    </span>`;
}

function renderTradeProfitZone(t, legs) {
  const strat = (t.suggestion && t.suggestion.strategy) || '';
  const shortCallLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE');
  const shortPutLeg  = legs.find(l => l.action === 'SELL' && l.option_type === 'PE');
  const longCallLeg  = legs.find(l => l.action === 'BUY'  && l.option_type === 'CE');
  const longPutLeg   = legs.find(l => l.action === 'BUY'  && l.option_type === 'PE');
  const ul = (t.suggestion && t.suggestion.underlying) || '';
  const spot = t.spot_at_execution != null ? parseFloat(t.spot_at_execution) : null;
  const execLegs = legs.filter(l => l.executed && l.fill_price != null);
  let actualNetCredit = 0;
  execLegs.forEach(l => { actualNetCredit += (l.action === 'SELL' ? 1 : -1) * parseFloat(l.fill_price || 0); });
  let realUpperBE = null;
  let realLowerBE = null;
  if (strat === 'BULL_CALL_SPREAD' && longCallLeg) {
    realLowerBE = parseFloat(longCallLeg.strike) + actualNetCredit;
  } else if (strat === 'BEAR_PUT_SPREAD' && longPutLeg) {
    realLowerBE = parseFloat(longPutLeg.strike) + actualNetCredit;
  } else {
    if (shortCallLeg) realUpperBE = parseFloat(shortCallLeg.strike) + actualNetCredit;
    if (shortPutLeg)  realLowerBE = parseFloat(shortPutLeg.strike)  - actualNetCredit;
  }
  const sug = t.suggestion || {};
  const profit = buildProfitScenario({
    strategy: strat,
    legs,
    underlying: ul,
    upperBE: sug.upper_breakeven != null ? sug.upper_breakeven : realUpperBE,
    lowerBE: sug.lower_breakeven != null ? sug.lower_breakeven : realLowerBE,
    dte: sug.dte,
    spot,
  });
  const beLine = (realUpperBE != null || realLowerBE != null) ? (() => {
    const parts = [];
    if (realLowerBE != null) parts.push(`Lower BE <strong>\u20b9${fmt(realLowerBE)}</strong>`);
    if (realUpperBE != null) parts.push(`Upper BE <strong>\u20b9${fmt(realUpperBE)}</strong>`);
    const spotBelowUpperBE = spot != null && realUpperBE != null && spot < realUpperBE;
    const spotAboveLowerBE = spot != null && realLowerBE != null && spot > realLowerBE;
    const safeAtEntry = (!realLowerBE || spotAboveLowerBE) && (!realUpperBE || spotBelowUpperBE);
    const beStatus = spot != null
      ? `<span class="pz-spot ${safeAtEntry ? 'pz-inside' : 'pz-outside'}">${safeAtEntry ? '\u2713 spot inside BEs at entry' : '\u26a0 spot outside BEs at entry'}</span>`
      : '';
    return `<div class="tpz-line pz-be-row">\u{1F4CF} Actual BEs (from fills): ${parts.join(' \u00b7 ')}${beStatus ? ' &nbsp;\u00b7&nbsp; ' + beStatus : ''}</div>`;
  })() : '';
  if (!profit.maxProfitText && !beLine) return '';
  const tag = profit.spotTag || '';
  const maxLine = profit.maxProfitText
    ? `<div class="tpz-line">\u{1F3AF} Max profit if ${escapeHtml(ul)} ${profit.maxProfitText}${tag ? ' &nbsp;\u00b7&nbsp; ' + tag : ''}</div>`
    : '';
  return `${maxLine}${beLine}`;
}

function renderTradeKvGrid(t, legs) {
  const premium = tradePremiumFromLegs(legs);
  const scLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE' && l.fill_price != null);
  const spLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'PE' && l.fill_price != null);
  let fillNetCredit = 0;
  legs.filter(l => l.executed && l.fill_price != null).forEach(l => {
    fillNetCredit += (l.action === 'SELL' ? 1 : -1) * parseFloat(l.fill_price);
  });
  const realUBE = scLeg ? parseFloat(scLeg.strike) + fillNetCredit : null;
  const realLBE = spLeg ? parseFloat(spLeg.strike) - fillNetCredit : null;
  const estMp = t.actual_max_profit != null ? t.actual_max_profit
              : (t.suggestion && t.suggestion.max_profit != null ? t.suggestion.max_profit : null);
  const estMl = t.actual_max_loss != null ? t.actual_max_loss
              : (t.suggestion && t.suggestion.max_loss != null ? t.suggestion.max_loss : null);
  const estPop = t.suggestion && t.suggestion.probability_of_profit != null
               ? t.suggestion.probability_of_profit : null;
  const estDte = t.suggestion && t.suggestion.dte != null ? t.suggestion.dte : null;
  const dteLeft = t.suggestion && t.suggestion.expiry_date
    ? dteFromExpiry(t.suggestion.expiry_date) : null;
  const dteDisplay = estDte != null
    ? `${estDte}${dteLeft != null ? ` (${dteLeft} left)` : ''}`
    : (dteLeft != null ? `${dteLeft} left` : null);
  const execWithFills = legs.filter(l => l.executed && l.fill_price != null);
  const estChg = execWithFills.length > 0
    ? estChargesFromLegs(execWithFills)
    : (t.suggestion && t.suggestion.estimated_charges_total != null
      ? t.suggestion.estimated_charges_total : null);
  const estNetPnl = (estMp != null && estChg != null) ? (estMp - estChg) : null;
  return `
      <div><span class="k">Entry date</span><br><span class="v">${fmtDt(t.executed_on)}</span></div>
      ${t.suggestion && t.suggestion.expiry_date ? `<div><span class="k">Options expiry</span><br><span class="v">${fmtDate(t.suggestion.expiry_date)}</span></div>` : '<div></div>'}
      <div><span class="k">Net credit (actual)</span><br><span class="v">\u20b9${fmt(t.net_credit_actual)}</span></div>
      ${premium ? `<div><span class="k">${premium.kind === 'received' ? 'Premium received' : 'Premium paid'}</span><br><span class="v">\u20b9${fmt(premium.rs)}</span></div>` : '<div></div>'}
      <div><span class="k">Type</span><br><span class="v">${escapeHtml(t.position_type)}</span></div>
      ${estMp != null ? `<div><span class="k">Est. max profit</span><br><span class="v pnl-profit">\u20b9${fmt(estMp)}</span></div>` : '<div></div>'}
      ${estMl != null ? `<div><span class="k">Est. max loss</span><br><span class="v pnl-loss">\u20b9${fmt(estMl)}<span class="econ-ml-hint">${pctHint(estMl, t.net_credit_actual, 'credit')}</span></span></div>` : '<div></div>'}
      ${estPop != null ? `<div><span class="k">Est. PoP</span><br><span class="v">${fmtPct(estPop)}</span></div>` : '<div></div>'}
      ${realUBE != null ? `<div><span class="k">Upper BE <span class="muted" style="font-size:.7rem">(from fills)</span></span><br><span class="v">\u20b9${fmt(realUBE)}</span></div>` : '<div></div>'}
      ${realLBE != null ? `<div><span class="k">Lower BE <span class="muted" style="font-size:.7rem">(from fills)</span></span><br><span class="v">\u20b9${fmt(realLBE)}</span></div>` : '<div></div>'}
      ${t.net_pnl != null ? `<div><span class="k">P&amp;L</span><br><span class="v">${formatPnlWithPct(t.net_pnl, premium)}</span></div>` : '<div></div>'}
      ${estChg != null ? `<div><span class="k">Est. charges <span class="muted" style="font-size:.7rem">(from fills)</span></span><br><span class="v">\u20b9${fmt(estChg)}</span></div>` : '<div></div>'}
      ${estNetPnl != null ? `<div><span class="k">Est. net P&amp;L</span><br><span class="v ${estNetPnl >= 0 ? 'pnl-profit' : 'pnl-loss'}">${formatPnlWithPct(estNetPnl, premium, { useGrossSign: false })}</span></div>` : '<div></div>'}
      ${dteDisplay != null ? `<div><span class="k">DTE at entry</span><br><span class="v">${escapeHtml(String(dteDisplay))}</span></div>` : '<div></div>'}
      <div><span class="k">Status</span><br><span class="v">${escapeHtml(t.status)}</span></div>
      ${t.closed_on ? `<div><span class="k">Exit date</span><br><span class="v">${fmtDt(t.closed_on)}</span></div>` : ''}`;
}

function renderTradeActionPanel(t) {
  const legs = t.legs || [];
  const premium = tradePremiumFromLegs(legs);
  const premAttrs = premium
    ? ` data-premium-rs="${premium.rs}" data-premium-kind="${premium.kind}"`
    : '';
  const ra = t.risk_alert || {};
  const sug = t.suggestion || {};
  const initial = _computeTradeActionInstruction({
    dailyStatus: t.daily_status,
    exitInstruction: t.exit_instruction,
    riskNotif: ra.notif_type,
    premiumInfo: premium,
  });
  const origSugHtml = renderOriginalSuggestion(t.suggestion);
  const isHold = initial.tone === 'hold';
  const profitZoneLines = renderTradeProfitZone(t, legs);
  const metaRow = origSugHtml ? `
      <div class="tap-meta-row">
        <details class="tap-details">
          <summary>Why? (numbers)</summary>
          <p class="tap-why muted">${escapeHtml(initial.why)}</p>
        </details>
        ${origSugHtml}
      </div>` : `
      <details class="tap-details">
        <summary>Why? (numbers)</summary>
        <p class="tap-why muted">${escapeHtml(initial.why)}</p>
      </details>`;
  return `
    <div class="trade-action-panel" data-trade-id="${escapeHtml(t.trade_id)}"${premAttrs}
         data-strategy="${escapeHtml(sug.strategy || '')}"
         data-daily-status="${escapeHtml(t.daily_status || '')}"
         data-exit-instruction="${escapeHtml(t.exit_instruction || '')}"
         data-risk-notif="${escapeHtml(ra.notif_type || '')}">
      <div class="trade-summary-row">
        <div class="trade-summary-metrics">
          <div class="card-id-row">
            <span class="id-chip" title="Trade ID">${escapeHtml(t.trade_id || '\u2014')}</span>
            ${t.suggestion_id ? `<span class="id-chip" title="Suggestion ID">${escapeHtml(t.suggestion_id)}</span>` : ''}
          </div>
          <div class="kv-grid kv-grid-trade">${renderTradeKvGrid(t, legs)}</div>
        </div>
        <div class="trade-summary-action">
          <div class="tap-inner tap-inner-${initial.tone}">
            <div class="tap-verb">${escapeHtml(initial.verb)}</div>
            <div class="tap-body">
              <div class="tap-title-row">
                <strong class="tap-title">${escapeHtml(initial.title)}</strong>
                <span class="tap-legend-wrap"${isHold ? '' : ' hidden'}>${renderTapLegendPopover()}</span>
              </div>
              <div class="tap-profit-zone"${isHold && profitZoneLines ? '' : ' hidden'}>${profitZoneLines}</div>
              <p class="tap-instruction"${isHold ? ' hidden' : ''}>${escapeHtml(initial.instruction)}</p>
              <p class="tap-cta"${isHold ? ' hidden' : ''}>${escapeHtml(initial.cta)}</p>
            </div>
          </div>
        </div>
      </div>
      ${metaRow}
    </div>`;
}

function _updateTradeActionPanel(tradeId, payload, stripState) {
  document.querySelectorAll(`.trade-action-panel[data-trade-id="${CSS.escape(tradeId)}"]`).forEach(panel => {
    const section = document.querySelector(
      `.live-profit-levels[data-trade-id="${CSS.escape(tradeId)}"]`,
    );
    const thresholds = section ? _resolveLiveLevelThresholds(section, payload || {}) : null;
    const liveMtm = (stripState && stripState.liveMtm != null)
      ? stripState.liveMtm
      : (payload && payload.mtm != null ? parseFloat(payload.mtm) : null);
    const lossRs = thresholds && thresholds.lossRs;
    const preBreachNear = (
      liveMtm != null && lossRs != null
      && liveMtm <= -(LIVE_RISK_MONITOR.pre_breach_fraction * lossRs)
      && liveMtm > -lossRs
    );
    const instr = _computeTradeActionInstruction({
      liveMtm,
      lossHit: stripState && stripState.lossHit,
      floorBreach: stripState && stripState.floorBreach,
      targetHit: stripState && stripState.targetHit,
      preBreachNear,
      lossRs,
      targetRs: thresholds && thresholds.targetRs,
      floor: thresholds && thresholds.floor,
      strategy: panel.dataset.strategy || '',
      dailyStatus: panel.dataset.dailyStatus,
      exitInstruction: panel.dataset.exitInstruction,
      riskNotif: panel.dataset.riskNotif,
      premiumInfo: _premiumFromDataset(panel),
    });
    const inner = panel.querySelector('.tap-inner');
    if (inner) {
      inner.className = `tap-inner tap-inner-${instr.tone}`;
      const verb = inner.querySelector('.tap-verb');
      const title = inner.querySelector('.tap-title');
      const instruction = inner.querySelector('.tap-instruction');
      const cta = inner.querySelector('.tap-cta');
      const legendWrap = inner.querySelector('.tap-legend-wrap');
      const profitZone = inner.querySelector('.tap-profit-zone');
      const isHold = instr.tone === 'hold';
      if (verb) verb.textContent = instr.verb;
      if (title) title.textContent = instr.title;
      if (legendWrap) legendWrap.hidden = !isHold;
      if (profitZone) profitZone.hidden = !isHold;
      if (instruction) {
        instruction.hidden = isHold;
        if (!isHold) instruction.textContent = instr.instruction;
      }
      if (cta) {
        cta.hidden = isHold;
        if (!isHold) cta.textContent = instr.cta;
      }
    }
    const why = panel.querySelector('.tap-why');
    if (why) why.textContent = instr.why;
  });
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
  const callCreditOnly = strategy === 'BEAR_CALL_SPREAD';
  const putCreditOnly = strategy === 'BULL_PUT_SPREAD';
  const putDebitOnly  = strategy === 'BEAR_PUT_SPREAD';
  const callDebitOnly = strategy === 'BULL_CALL_SPREAD';

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
    const maxLossRs = s.max_loss != null ? parseFloat(s.max_loss) : null;
    rows.push({ label: 'MTM stop loss', val: slExitPlanText(strategy, maxLossRs), key: true });
  } else if (callCreditOnly && scLeg) {
    if (slLevel != null) {
      const buf = Math.round(slLevel - scLeg.strike);
      const bufStr = buf > 0 ? ` (${buf} pts above short call ${fmt(scLeg.strike)})` : '';
      rows.push({ label: 'Call-side SL', val: `exit call spread if ${und} rises above ${fmt(slLevel)}${bufStr}` });
    } else {
      rows.push({ label: 'Call-side SL', val: `exit call spread if ${und} rises above short call ${fmt(scLeg.strike)}` });
    }
    rows.push({ label: 'MTM stop loss', val: slExitPlanText(strategy, s.max_loss != null ? parseFloat(s.max_loss) : null), key: true });
  } else if (putCreditOnly && spLeg) {
    if (slLevel != null) {
      const buf = Math.round(spLeg.strike - slLevel);
      const bufStr = buf > 0 ? ` (${buf} pts below short put ${fmt(spLeg.strike)})` : '';
      rows.push({ label: 'Put-side SL', val: `exit put spread if ${und} falls below ${fmt(slLevel)}${bufStr}` });
    } else {
      rows.push({ label: 'Put-side SL', val: `exit put spread if ${und} falls below short put ${fmt(spLeg.strike)}` });
    }
    rows.push({ label: 'MTM stop loss', val: slExitPlanText(strategy, s.max_loss != null ? parseFloat(s.max_loss) : null), key: true });
  } else if (putDebitOnly || callDebitOnly || strategy === 'LONG_CALL' || strategy === 'LONG_PUT'
      || (['LONG_STRADDLE', 'LONG_STRANGLE', 'CALENDAR_SPREAD'].includes(strategy) && isDebit)) {
    const maxLossRs = s.max_loss != null ? parseFloat(s.max_loss) : null;
    let note = '';
    if (putDebitOnly) note = ` — no spot trigger; bear put loses when ${und} rallies`;
    else if (callDebitOnly) note = ` — no spot trigger; bull call loses when ${und} falls`;
    else if (strategy === 'LONG_CALL') note = ` — ${und} decline hurts long call`;
    else if (strategy === 'LONG_PUT') note = ` — ${und} rally hurts long put`;
    rows.push({ label: 'Stop loss', val: slExitPlanText(strategy, maxLossRs) + note, key: true });
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

// ── Per-leg target close (must mirror renderExitPlan / leg_builder) ─────────
function longPremiumTargetMult(dte) {
  const TARGET_BASE = 0.50, TARGET_DTE_SCALE = 14.0, TARGET_MAX = 1.50;
  const dteSafe = (typeof dte === 'number' && dte > 0) ? dte : 0;
  return dteSafe === 0
    ? TARGET_BASE
    : Math.min(TARGET_MAX, TARGET_BASE + dteSafe / TARGET_DTE_SCALE);
}

function isDebitStrategy(strategy) {
  return ['LONG_STRADDLE', 'LONG_STRANGLE', 'LONG_CALL', 'LONG_PUT',
          'BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD', 'CALENDAR_SPREAD'].includes(strategy || '');
}

/** Credit structures with a persisted Nifty spot stop band (not MTM-only exits). */
function usesSpotStopLoss(strategy, slLevel) {
  if (isDebitStrategy(strategy)) return false;
  const sl = slLevel != null ? parseFloat(slLevel) : NaN;
  return !isNaN(sl) && sl > 0;
}

/** Exit price hint for one leg. SELL shorts: buy back lower. BUY longs: sell higher. */
function legTargetClosePrice(action, entry, strategy, dte) {
  const px = parseFloat(entry) || 0;
  if (!px) return null;
  if (action === 'SELL') {
    const capture = strategy === 'IRON_BUTTERFLY' ? 0.25 : 0.50;
    return Math.round(px * (1 - capture) * 100) / 100;
  }
  if (!isDebitStrategy(strategy)) {
    return null;
  }
  if (['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD'].includes(strategy)) {
    return Math.round(px * 1.5 * 100) / 100;
  }
  const mult = longPremiumTargetMult(dte);
  return Math.round(px * (1 + mult) * 100) / 100;
}

function legTargetCloseCaption(action, strategy, dte) {
  if (action === 'SELL') {
    return strategy === 'IRON_BUTTERFLY' ? '25% credit capture' : '50% credit capture';
  }
  if (!isDebitStrategy(strategy)) {
    return 'close with spread';
  }
  if (['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD'].includes(strategy)) {
    return '50% gain on premium';
  }
  return `${Math.round(longPremiumTargetMult(dte) * 100)}% gain on premium`;
}

function legTargetCloseHint(action, entry, strategy, dte, legOrder) {
  const targetClose = legTargetClosePrice(action, entry, strategy, dte);
  const orderAttr = legOrder != null ? ` data-leg-order="${legOrder}"` : '';
  if (targetClose == null) {
    return `<span class="leg-target-close muted">Target close: ${legTargetCloseCaption(action, strategy, dte)}</span>`;
  }
  const capLabel = legTargetCloseCaption(action, strategy, dte);
  const verb = action === 'SELL' ? 'buy back' : 'sell back';
  const cmp = action === 'SELL' ? '\u2264' : '\u2265';
  return `<span class="leg-target-close">Target close: ${verb} ${cmp} \u20b9<span class="target-close-val"${orderAttr}>${fmt(targetClose)}</span> (${capLabel})</span>`;
}

// ── Confidence checks breakdown ──────────────────────────────────────────────
// conditions_json is always [{label, status, detail}, ...] (array format).
// Legacy {conditions:[...strings...]} kept as safety fallback.
const CONFIDENCE_LEGACY_TOTAL = 7;
const CONFIDENCE_EXPANDED_TOTAL = 14; // sync with engine/confidence.py gate count

function parseConditionsJson(s) {
  if (!s || !s.conditions_json) return null;
  let raw = s.conditions_json;
  if (typeof raw === 'string') {
    try { raw = JSON.parse(raw); } catch { return null; }
  }
  if (!Array.isArray(raw) || !raw.length) {
    if (raw && raw.conditions && Array.isArray(raw.conditions) && raw.conditions.length) {
      return raw.conditions.map(lbl => ({
        label: lbl,
        status: 'PASS',
        detail: '(legacy format — no detail stored)',
      }));
    }
    return null;
  }
  return raw.map(c => ({
    label:  c.label  || '',
    status: c.status || (c.passed === true ? 'PASS' : c.passed === false ? 'FAIL' : 'PASS'),
    detail: c.detail || '',
  }));
}

/** Passed/total check counts — uses conditions_json when present, else DB score. */
function confidenceCounts(s) {
  const checks = parseConditionsJson(s);
  if (checks) {
    const nFail = checks.filter(c => c.status === 'FAIL').length;
    const nSoftFail = checks.filter(c => c.status === 'SOFT_FAIL').length;
    const total = checks.length;
    return { passed: total - nFail - nSoftFail, total };
  }
  const score = s?.confidence_score ?? s?.confidence;
  if (score == null) return null;
  const passed = score;
  const total = passed <= CONFIDENCE_LEGACY_TOTAL
    ? CONFIDENCE_LEGACY_TOTAL
    : Math.max(CONFIDENCE_EXPANDED_TOTAL, passed);
  return { passed, total };
}

function formatConfidence(s) {
  const c = confidenceCounts(s);
  return c ? `${c.passed}/${c.total}` : '';
}

function renderConfidenceChecks(s) {
  const checks = parseConditionsJson(s);
  if (!checks || !checks.length) return '';

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
  // Review item #10: expected-move calibration warning. Server-computed
  // when realised/expected median for (underlying, dte_band) deviates >25%
  // from 1.0 over the most recent expiry cohort.
  if (s.em_calibration_warning) {
    chips.push(`<span class="ctx-chip ctx-fail" title="Historical realised vs expected move drifted \u2014 short strikes may be miscalibrated">\u26A0 ${escapeHtml(s.em_calibration_warning)}</span>`);
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
  // Provenance: live suggestions show when they were generated (not a hardcoded job time).
  const genChipFmt = fmtChipDt(s.generated_on);
  const isLiveSource = s.data_source === 'LIVE' || s.trigger_type === 'LIVE_RUN';
  if (isLiveSource) {
    if (genChipFmt) {
      const tipParts = ['Suggestion generated at this date/time (IST)'];
      if (s.provider) tipParts.push(`Data: LIVE via ${s.provider}`);
      if (s.trigger_type) tipParts.push(`Trigger: ${s.trigger_type}`);
      if (s.trigger_reason) tipParts.push(s.trigger_reason);
      chips.push(
        `<span class="ctx-chip ctx-iv" title="${escapeHtml(tipParts.join('\n'))}">` +
        `Generated \u2192 ${escapeHtml(genChipFmt)}</span>`
      );
    } else {
      chips.push(`<span class="ctx-chip ctx-iv" title="Live market data">Live</span>`);
    }
  } else if (s.data_source) {
    const tip = s.provider ? `Source: ${s.data_source} via ${s.provider}` : `Source: ${s.data_source}`;
    chips.push(`<span class="ctx-chip" title="${escapeHtml(tip)}">${escapeHtml(s.data_source)}</span>`);
  }
  if (s.trigger_type && s.trigger_type !== 'LIVE_RUN') {
    const label = s.trigger_type === 'EOD_RUN'            ? 'EOD'
                : s.trigger_type === 'INTRADAY_VALIDATOR' ? '09:35 check'
                : s.trigger_type === 'WS_REGEN'           ? 'Tick regen'
                : s.trigger_type === 'MANUAL'             ? 'Manual'
                : s.trigger_type;
    const tip = s.trigger_reason ? `Trigger: ${s.trigger_type}\n${s.trigger_reason}` : `Trigger: ${s.trigger_type}`;
    chips.push(`<span class="ctx-chip" title="${escapeHtml(tip)}">${escapeHtml(label)}</span>`);
  }
  // OI change momentum: ΣΔPut OI / ΣΔCall OI
  // EOD = day-over-day change; Live = since market open today.
  if (s.oi_pcr_change != null) {
    const v = parseFloat(s.oi_pcr_change);
    const label = v.toFixed(2);
    const cls   = v > 1.2  ? 'ctx-chip ctx-fail'   // puts building → bearish pressure
                : v < 0.8  ? 'ctx-chip ctx-pass'   // calls building → bullish positioning
                :            'ctx-chip ctx-warn';   // balanced
    const srcLabel = (s.trigger_type === 'LIVE_RUN') ? 'since open' : 'day-over-day';
    const tip = `OI Change PCR (${srcLabel}): ${label}\n`
              + `>1.2 → puts building faster (bearish hedge)\n`
              + `0.8–1.2 → balanced OI addition\n`
              + `<0.8 → calls building faster (bullish positioning)`;
    chips.push(`<span class="${cls}" title="${escapeHtml(tip)}">OI\u0394 PCR ${escapeHtml(label)}</span>`);
  }
  if (s.confidence_score != null) {
    const cc = confidenceCounts(s);
    let _warnCount = 0, _errorCount = 0, _failCount = 0, _softFailCount = 0;
    const checks = parseConditionsJson(s);
    if (checks) {
      _warnCount     = checks.filter(c => c.status === 'PASS_WARN').length;
      _errorCount    = checks.filter(c => c.status === 'PASS_ERROR').length;
      _failCount     = checks.filter(c => c.status === 'FAIL').length;
      _softFailCount = checks.filter(c => c.status === 'SOFT_FAIL').length;
    }
    const displayScore = cc ? cc.passed : s.confidence_score;
    const _total = cc ? cc.total : CONFIDENCE_LEGACY_TOTAL;
    const hasIssues  = _warnCount > 0 || _errorCount > 0 || _softFailCount > 0;
    const chipClass  = _failCount > 0      ? 'ctx-chip ctx-fail conf-chip'
                     : _softFailCount > 0  ? 'ctx-chip ctx-warn conf-chip'
                     : hasIssues           ? 'ctx-chip ctx-warn conf-chip'
                     :                       'ctx-chip ctx-pass conf-chip';
    const warnSuffix = _errorCount > 0
      ? ` \u26a1 ${_errorCount} error${_errorCount > 1 ? 's' : ''}`
      : _softFailCount > 0 ? ` \u26a0 ${_softFailCount} soft fail${_softFailCount > 1 ? 's' : ''}`
      : _warnCount > 0 ? ` \u26a0 ${_warnCount} warned` : '';
    chips.push(`<span class="${chipClass}" data-sug-id="${escapeHtml(s.suggestion_id||'')}" style="cursor:pointer" title="Click to see all checks">${displayScore}/${_total} checks \u2713${warnSuffix} <span style="font-size:.7rem;opacity:.7">\u25bc</span></span><span class="conf-logic-info" tabindex="0" aria-label="Confidence gate logic">\u24d8<span class="conf-logic-popup"><strong>How gating works</strong><br><br><span style="color:#f87171">\u2717 Hard gate</span> &mdash; always blocks:<br>&nbsp;&bull; DTE within target band<br>&nbsp;&bull; ATM strikes liquid (spread within budget)<br><br><span style="color:#fbbf24">\u2717 Soft gates</span> &mdash; need \u22655 of 8:<br>&nbsp;&bull; IV Rank in actionable zone<br>&nbsp;&bull; VIX stable or falling<br>&nbsp;&bull; PCR in neutral band<br>&nbsp;&bull; OI walls visible<br>&nbsp;&bull; Trend identifiable<br>&nbsp;&bull; IV premium vs realised vol (HV-20)<br>&nbsp;&bull; FII positioning aligned with trend<br>&nbsp;&bull; OI change conviction aligned with trend<br><br><span style="color:#fbbf24">\u26a0 Advisory (live mode):</span><br>&nbsp;&bull; ATM IV trajectory<br>&nbsp;&bull; OI PCR momentum<br>&nbsp;&bull; IV Rank vs IV/HV alignment<br><br><span style="opacity:.6;font-size:.72rem">1\u20132 soft gate misses = trade proceeds with caution<br>3+ soft gate misses = blocked</span><br><br><span style="opacity:.5;font-size:.72rem">\u26a0 = data unavailable &nbsp;\u26a1 = gate error</span></span></span>`);
  }
  // Edge score (0-100) — composite quality blend; display + ranking only.
  if (s.edge_score != null) {
    const es = parseFloat(s.edge_score);
    const esCls = es >= 70 ? 'ctx-chip ctx-pass'
                : es >= 50 ? 'ctx-chip ctx-warn'
                :            'ctx-chip ctx-fail';
    const esTip = 'Edge score (0-100): weighted blend of PoP, credit-to-width (or debit discount), IV regime alignment, and confidence headroom. Higher = better trade quality.';
    chips.push(`<span class="${esCls}" title="${escapeHtml(esTip)}">Edge ${es.toFixed(0)}/100</span>`);
  }
  // Credit-to-width grade for credit strategies — visual quality tag
  if (s.credit_grade) {
    const gradeMap = { strong: 'ctx-chip ctx-pass', good: 'ctx-chip ctx-pass', weak: 'ctx-chip ctx-warn' };
    const gCls = gradeMap[s.credit_grade] || 'ctx-chip';
    const gTip = 'Credit-to-width ratio grade: weak (<25%), good (25-30%), strong (>=30%). Strong credits compensate better for the spread risk.';
    chips.push(`<span class="${gCls}" title="${escapeHtml(gTip)}">Credit: ${escapeHtml(s.credit_grade)}</span>`);
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
function execOrderSeqHtml(legs, strategy, mode) {
  if (!legs || legs.length <= 1) return null;
  const map = executionOrder(legs, strategy, mode);
  if (!map.size) return null;
  const ordered = [...legs].sort((a, b) => (map.get(a.leg_order) || 99) - (map.get(b.leg_order) || 99));
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
      ${l.strike || ''} ${escapeHtml(l.option_type || '')}
    </span>`;
  }).join('<span class="exec-seq-arrow">\u2192</span>');
  const heading = mode === 'close'
    ? 'Close in this order \u2014 buy back short legs FIRST, then sell longs:'
    : 'Execute in this order \u2014 acquire hedges (BUY) FIRST, then SELL shorts:';
  return { heading, seq };
}

function execOrderBanner(legs, strategy, mode) {
  const content = execOrderSeqHtml(legs, strategy, mode);
  if (!content) return '';
  const icon = mode === 'close' ? '\u26a0\ufe0f ' : '\u26a0\ufe0f ';
  return `<div class="exec-order-banner exec-order-${mode}">
    <div class="exec-order-heading">${icon}${content.heading}</div>
    <div class="exec-order-seq">${content.seq}</div>
  </div>`;
}

function execOrderFillsSection(legs, strategy, titleText) {
  const orderBanner = execOrderBanner(legs, strategy, 'close');
  return `
    <div class="close-col-title">${titleText}</div>
    ${orderBanner ? `<div class="close-order-static">${orderBanner}</div>` : ''}`;
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

/**
 * Single source of truth for "where must the underlying be for max profit?"
 * Used by both the Suggestion tab (Ideal scenario) and the Trade tab
 * (Max profit if… bar). Logic mirrors engine/leg_builder.py breakevens()
 * and max_profit_loss() — not inferred from leg action alone (which wrongly
 * treated every short put as a bull-put credit spread).
 *
 * @returns {{ ideal: string, maxProfitText: string|null, spotTag: string|null }}
 */
function buildProfitScenario({
  strategy,
  legs = [],
  underlying = 'NIFTY',
  upperBE = null,
  lowerBE = null,
  dte = null,
  spot = null,
}) {
  const ul = underlying || 'NIFTY';
  const name = ul === 'NIFTY' ? 'Nifty' : ul;
  const dteDesc = dte != null ? `with ${dte} DTE` : '';
  const scLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'CE');
  const spLeg = legs.find(l => l.action === 'SELL' && l.option_type === 'PE');
  const bcLeg = legs.find(l => l.action === 'BUY'  && l.option_type === 'CE');
  const bpLeg = legs.find(l => l.action === 'BUY'  && l.option_type === 'PE');
  const ub = upperBE != null && upperBE !== '' ? parseFloat(upperBE) : null;
  const lb = lowerBE != null && lowerBE !== '' ? parseFloat(lowerBE) : null;

  const spotTag = (inside, labelOk, labelBad) => {
    if (spot == null || isNaN(spot)) return null;
    const cls = inside ? 'pz-inside' : 'pz-outside';
    return `<span class="pz-spot ${cls}">Spot \u20b9${fmt(spot)} ${inside ? labelOk : labelBad}</span>`;
  };

  switch (strategy) {
    case 'IRON_CONDOR': {
      if (!spLeg || !scLeg) return { ideal: '', maxProfitText: null, spotTag: null };
      const lo = parseFloat(spLeg.strike);
      const hi = parseFloat(scLeg.strike);
      const inside = spot != null && spot >= lo && spot <= hi;
      return {
        ideal: `${name} drifts sideways ${dteDesc} between \u20b9${fmt(lo)} and \u20b9${fmt(hi)} (full credit zone).`,
        maxProfitText: `stays between <strong>\u20b9${fmt(lo)}</strong> and <strong>\u20b9${fmt(hi)}</strong>`,
        spotTag: spotTag(inside, '\u2713 inside zone', '\u26a0 outside zone'),
      };
    }
    case 'IRON_BUTTERFLY': {
      const atm = scLeg ? parseFloat(scLeg.strike) : (spLeg ? parseFloat(spLeg.strike) : null);
      if (atm == null) return { ideal: '', maxProfitText: null, spotTag: null };
      const lo = lb != null ? lb : atm - (ub != null && lb != null ? (ub - lb) / 2 : 0);
      const hi = ub != null ? ub : atm;
      const inside = spot != null && lb != null && ub != null
        ? spot >= lb && spot <= ub
        : spot != null && Math.abs(spot - atm) / atm < 0.005;
      return {
        ideal: `${name} closes at or near \u20b9${fmt(atm)} ${dteDesc} on expiry.`,
        maxProfitText: `pins near <strong>\u20b9${fmt(atm)}</strong>`,
        spotTag: spotTag(inside, '\u2713 near body', '\u26a0 away from body'),
      };
    }
    case 'BULL_PUT_SPREAD': {
      if (!spLeg) return { ideal: '', maxProfitText: null, spotTag: null };
      const k = parseFloat(spLeg.strike);
      const inside = spot != null && spot >= k;
      return {
        ideal: `${name} rises or stays comfortably above \u20b9${fmt(k)} ${dteDesc}.`,
        maxProfitText: `stays at or above <strong>\u20b9${fmt(k)}</strong>`,
        spotTag: spotTag(inside, '\u2713 above short put', '\u26a0 below short put'),
      };
    }
    case 'BEAR_CALL_SPREAD': {
      if (!scLeg) return { ideal: '', maxProfitText: null, spotTag: null };
      const k = parseFloat(scLeg.strike);
      const inside = spot != null && spot <= k;
      return {
        ideal: `${name} falls or remains flat, staying below \u20b9${fmt(k)} ${dteDesc}.`,
        maxProfitText: `stays at or below <strong>\u20b9${fmt(k)}</strong>`,
        spotTag: spotTag(inside, '\u2713 below short call', '\u26a0 above short call'),
      };
    }
    case 'JADE_LIZARD': {
      if (!spLeg) return { ideal: '', maxProfitText: null, spotTag: null };
      const k = parseFloat(spLeg.strike);
      const inside = spot != null && spot >= k;
      return {
        ideal: `${name} stays flat or rallies steadily through expiry ${dteDesc}.`,
        maxProfitText: `stays at or above <strong>\u20b9${fmt(k)}</strong>`,
        spotTag: spotTag(inside, '\u2713 above short put', '\u26a0 below short put'),
      };
    }
    case 'BULL_CALL_SPREAD': {
      if (!scLeg) return { ideal: '', maxProfitText: null, spotTag: null };
      const k = parseFloat(scLeg.strike);
      const inside = spot != null && spot >= k;
      return {
        ideal: `${name} climbs steadily to or above \u20b9${fmt(k)} ${dteDesc}.`,
        maxProfitText: `rises to or above <strong>\u20b9${fmt(k)}</strong>`,
        spotTag: spotTag(inside, '\u2713 at/above short call', '\u26a0 below short call'),
      };
    }
    case 'BEAR_PUT_SPREAD': {
      if (!spLeg) return { ideal: '', maxProfitText: null, spotTag: null };
      const k = parseFloat(spLeg.strike);
      const inside = spot != null && spot <= k;
      return {
        ideal: `${name} drifts or falls to or below \u20b9${fmt(k)} ${dteDesc}.`,
        maxProfitText: `falls to or below <strong>\u20b9${fmt(k)}</strong>`,
        spotTag: spotTag(inside, '\u2713 at/below short put', '\u26a0 above short put'),
      };
    }
    case 'LONG_STRADDLE': {
      const hi = ub;
      const lo = lb;
      if (hi == null || lo == null) return { ideal: '', maxProfitText: null, spotTag: null };
      const outside = spot != null && (spot >= hi || spot <= lo);
      return {
        ideal: `A surprise event triggers a large ${name} move in either direction before expiry ${dteDesc}.`,
        maxProfitText: `breaks above <strong>\u20b9${fmt(hi)}</strong> or below <strong>\u20b9${fmt(lo)}</strong>`,
        spotTag: spotTag(outside, '\u2713 outside BEs', '\u26a0 inside BEs (losing)'),
      };
    }
    case 'LONG_STRANGLE': {
      const hi = ub;
      const lo = lb;
      if (hi == null || lo == null) return { ideal: '', maxProfitText: null, spotTag: null };
      const outside = spot != null && (spot >= hi || spot <= lo);
      return {
        ideal: `A large gap-and-go move in either direction shortly after entry ${dteDesc}.`,
        maxProfitText: `breaks above <strong>\u20b9${fmt(hi)}</strong> or below <strong>\u20b9${fmt(lo)}</strong>`,
        spotTag: spotTag(outside, '\u2713 outside BEs', '\u26a0 inside BEs (losing)'),
      };
    }
    case 'LONG_CALL': {
      const be = ub != null ? ub : (bcLeg ? parseFloat(bcLeg.strike) : null);
      if (be == null) return { ideal: '', maxProfitText: null, spotTag: null };
      const inside = spot != null && spot >= be;
      return {
        ideal: `${name} surges upward quickly, well above the call strike ${dteDesc}.`,
        maxProfitText: `rises above <strong>\u20b9${fmt(be)}</strong>`,
        spotTag: spotTag(inside, '\u2713 above breakeven', '\u26a0 below breakeven'),
      };
    }
    case 'LONG_PUT': {
      const be = lb != null ? lb : (bpLeg ? parseFloat(bpLeg.strike) : null);
      if (be == null) return { ideal: '', maxProfitText: null, spotTag: null };
      const inside = spot != null && spot <= be;
      return {
        ideal: `${name} breaks down sharply before expiry ${dteDesc}.`,
        maxProfitText: `falls below <strong>\u20b9${fmt(be)}</strong>`,
        spotTag: spotTag(inside, '\u2713 below breakeven', '\u26a0 above breakeven'),
      };
    }
    default:
      return { ideal: '', maxProfitText: null, spotTag: null };
  }
}

/** Wrap buildProfitScenario output as the trade-card profit-zone bar. */
function renderProfitZoneBar(scenario, underlying, beHtml = '') {
  if (!scenario || !scenario.maxProfitText) return '';
  const tag = scenario.spotTag || '';
  return `<div class="profit-zone-bar">\u{1F3AF} Max profit if ${escapeHtml(underlying)} ${scenario.maxProfitText}${tag ? ' &nbsp;\u00b7&nbsp; ' + tag : ''}${beHtml}</div>`;
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
    },
    IRON_BUTTERFLY: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty is ${trendDesc}, and a Butterfly concentrates both short strikes at the ATM level (₹${fmt(scLeg?.strike || spLeg?.strike)}) to collect maximum credit. Higher premium than an Iron Condor, but a narrower profit zone.`,
      better: `Nifty pins close to ₹${fmt(scLeg?.strike || spLeg?.strike)} through expiry. IV crush after any event also accelerates profit. ${vixDesc}. Max credit is captured on expiry-at-the-strike.`,
    },
    BULL_PUT_SPREAD: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty's trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. Selling a put spread collects credit with downside risk capped at the spread width — you only lose if Nifty falls hard below ₹${fmt(spLeg?.strike)}.`,
      better: `Nifty rises or stays flat above ₹${fmt(spLeg?.strike)} (the short put). Even a mild pullback is fine as long as it holds above ₹${fmt(lb)}. ${vixDesc}. A rally above spot earns full credit${pop ? ` (${pop}% PoP)` : ''}.`,
    },
    BEAR_CALL_SPREAD: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty's trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. Selling a call spread collects credit with upside risk capped — you only lose if Nifty rallies hard above ₹${fmt(scLeg?.strike)}.`,
      better: `Nifty falls or stays flat below ₹${fmt(scLeg?.strike)} (the short call). Even a small bounce is fine as long as it stays under ₹${fmt(ub)}. ${vixDesc}. A continued decline earns full credit${pop ? ` (${pop}% PoP)` : ''}.`,
    },
    JADE_LIZARD: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}${pcrDesc ? `. ${pcrDesc.charAt(0).toUpperCase() + pcrDesc.slice(1)}` : ''}. A Jade Lizard (short OTM call spread + short OTM put) generates premium with zero upside risk — the call spread credit exactly offsets the short put's upside exposure.`,
      better: `Nifty rises or stays sideways. No loss on the upside${pop ? ` (${pop}% PoP)` : ''}. Downside risk only appears below ₹${fmt(spLeg?.strike)} minus net credit. ${vixDesc}.`,
    },
    CALENDAR_SPREAD: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Nifty is ${trendDesc}. A calendar spread sells the near expiry and buys a farther one — you profit when the index stays calm and time decay works in your favour.`,
      better: `Nifty stays near ₹${fmt(spot)} and does not make a large move before the near expiry. ${vixDesc}. Quiet days help the near leg lose value faster than the far leg.`,
    },
    LONG_STRADDLE: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Buying both ATM CE and PE ${dteDesc} at low cost lets you profit from any large directional move, regardless of which way Nifty goes.`,
      better: `A sharp breakout above ₹${fmt(ub)} or breakdown below ₹${fmt(lb)}. ${vixDesc}. Every day Nifty stays flat, the ₹${fmt(debit)}/unit debit decays — the move should come soon.`,
    },
    LONG_STRANGLE: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Buying OTM CE (₹${fmt(bcLeg?.strike)}) and OTM PE (₹${fmt(bpLeg?.strike)}) costs less than a Straddle but needs a bigger move to profit.`,
      better: `Nifty breaks sharply above ₹${fmt(ub)} or below ₹${fmt(lb)}. ${vixDesc}. Every day without a move, time decay chips away at the ₹${fmt(debit)}/unit paid.`,
    },
    LONG_CALL: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Long Call gives unlimited upside for a defined ₹${fmt(debit)}/unit debit${dte ? ` with ${dte} DTE` : ''}. High leverage, low capital at risk.`,
      better: `Nifty rallies strongly above ₹${fmt(bcLeg?.strike)}. Delta and gamma accelerate profits as Nifty moves higher. ${vixDesc}. Act early — time decay accelerates toward expiry.`,
    },
    LONG_PUT: {
      why:    `IV Rank is ${ivDesc} — ${ivPremDesc}. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Long Put profits from a decline while limiting max loss to ₹${fmt(debit)}/unit${dte ? ` with ${dte} DTE` : ''}.`,
      better: `Nifty falls sharply below ₹${fmt(bpLeg?.strike)}. ${vixDesc}. Avoid holding too close to expiry if the move hasn't materialised — theta decay accelerates.`,
    },
    BULL_CALL_SPREAD: {
      why:    `IV Rank is ${ivDesc} — not cheap enough for naked long calls, yet not rich enough for pure credit writing. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Bull Call Spread caps the debit while allowing upside to ₹${fmt(scLeg?.strike)}.`,
      better: `Nifty rises above ₹${fmt(scLeg?.strike)} by expiry — the full spread width is earned. ${vixDesc}. A flat or falling Nifty loses the debit paid.`,
    },
    BEAR_PUT_SPREAD: {
      why:    `IV Rank is ${ivDesc} — debit spreads work better than naked longs or pure credit writing here. Trend is ${trendDesc}${pcrDesc ? ` and ${pcrDesc}` : ''}. A Bear Put Spread profits from a decline to ₹${fmt(spLeg?.strike)} while capping the debit.`,
      better: `Nifty falls below ₹${fmt(spLeg?.strike)} by expiry — full spread width is earned. ${vixDesc}. A flat or rising Nifty loses the debit paid.`,
    },
  };

  const info = lookup[s.strategy];
  if (!info) return '';

  const betterLabel = s.regime_pair_type === 'range'
    ? 'When this trade wins (range)'
    : (s.regime_pair_type === 'breakout' ? 'When this trade wins (big move)' : 'What makes it better');

  const profit = buildProfitScenario({
    strategy: s.strategy,
    legs,
    underlying: s.underlying,
    upperBE: s.upper_breakeven,
    lowerBE: s.lower_breakeven,
    dte: dte,
    spot,
  });
  const idealHtml = profit.ideal
    ? `<div class="sr-row sr-ideal">
      <span class="sr-label">Ideal scenario</span>
      <span class="sr-text">${profit.ideal}</span>
    </div>`
    : '';

  return `<div class="strategy-rationale">
    <div class="sr-row">
      <span class="sr-label">Why this strategy today</span>
      <span class="sr-text">${info.why}</span>
    </div>
    <div class="sr-row">
      <span class="sr-label">${betterLabel}</span>
      <span class="sr-text">${info.better}</span>
    </div>
    ${idealHtml}
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
function suggestionCanExecute(s) {
  const status = (s.status || '').toUpperCase();
  if (status !== 'PENDING') return false;
  if (s.execution_gate) return !!s.execution_gate.ok;
  return !s.is_stale;
}

function renderExecutionGateBanner(s, { showBlockedActions = false } = {}) {
  const status = (s.status || '').toUpperCase();
  if (status === 'EXECUTED') {
    return `<div class="suggestion-gate-banner suggestion-gate-info">
      <span class="tag tag-ok">EXECUTED</span>
      <span>This suggestion was already acted on.</span>
    </div>`;
  }
  if (status === 'IGNORED') {
    return `<div class="suggestion-gate-banner suggestion-gate-retired">
      <div class="suggestion-gate-head">
        <span class="tag tag-warn">Retired</span>
        <strong>Not actionable</strong>
      </div>
      <p class="suggestion-gate-detail muted">This suggestion was retired (stale or superseded). Run <strong>Live Suggestion Engine</strong> from the Jobs tab for a fresh PENDING suggestion.</p>
    </div>`;
  }
  const gate = s.execution_gate;
  if (!gate || gate.ok) return '';
  const label = gate.label || 'Cannot execute';
  const detail = (gate.reason && gate.reason !== 'OK') ? gate.reason : '';
  const actions = showBlockedActions ? `
    <div class="suggestion-gate-actions btn-row">
      <button type="button" class="btn btn-sm btn-accent btn-mark-exec" disabled title="Execution blocked — see notice above">Mark Executed</button>
      <button type="button" class="btn btn-sm btn-ghost btn-ignore">Ignore / dismiss</button>
    </div>` : '';
  return `<div class="suggestion-gate-banner suggestion-gate-blocked" role="alert">
    <div class="suggestion-gate-head">
      <span class="tag tag-warn">${escapeHtml(label.toUpperCase())}</span>
      <strong>Execution blocked</strong>
    </div>
    ${detail ? `<p class="suggestion-gate-detail">${escapeHtml(detail)}</p>` : ''}
    <p class="suggestion-gate-hint muted">Run <strong>Live Suggestion Engine</strong> from the Jobs tab for a fresh PENDING suggestion, or dismiss this card.</p>
    ${actions}
  </div>`;
}

function renderSuggestion(s, readOnly = false, allSuggestions = [], inlineHeader = false, expanded = true) {
  const isNoSug = s.strategy === 'NONE' || s.status === 'NO_SUGGESTION';
  if (isNoSug) {
    return renderSitOutCard(s);
  }
  const sugStatus = (s.status || '').toUpperCase();
  const isRetired = sugStatus === 'IGNORED';
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
  const sugPremium = premiumFromCreditTotal(baseTotalCredit);
  const sugStrategy = s.strategy || '';
  const sugDte = s.dte != null ? parseInt(s.dte, 10) : null;
  const legsHtml = (s.legs || []).map(l => {
    const legTotal = (l.lots || 0) * (l.lot_size || 0) * (l.suggested_price || 0);
    // Threshold hint: SELL needs price >= low (to retain credit), BUY needs
    // price <= high (to keep debit small).
    const thresholdHint = l.action === 'SELL'
      ? `<span class="leg-threshold ok">Sell ≥ ₹${fmt(l.suggested_price_low)}</span>`
      : `<span class="leg-threshold warn">Buy ≤ ₹${fmt(l.suggested_price_high)}</span>`;
    const closeHint = legTargetCloseHint(
      l.action, l.suggested_price, sugStrategy, sugDte, l.leg_order,
    );
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
  const canExecute = !readOnly && suggestionCanExecute(s);
  const gateLabel = s.execution_gate?.label
    || (s.is_stale ? 'Stale' : null)
    || (sugStatus === 'IGNORED' ? 'Retired' : null);
  const gateBanner = (readOnly && sugStatus !== 'IGNORED')
    ? ''
    : renderExecutionGateBanner(s, {
      showBlockedActions: !readOnly && sugStatus === 'PENDING' && !canExecute,
    });
  const summaryHtml = `
    <div class="card-head collapsible-card-head">
      <h3>${escapeHtml(s.trade_name || s.suggestion_id)}</h3>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <span class="tag tag-accent">${escapeHtml(s.strategy || '')}</span>
        ${gateLabel && sugStatus === 'PENDING' ? `<span class="tag tag-warn" title="Cannot mark executed">${escapeHtml(gateLabel)}</span>` : ''}
        ${sugStatus === 'IGNORED' ? '<span class="tag tag-warn">Retired</span>' : ''}
        ${s.is_stale && sugStatus === 'PENDING' && !gateLabel ? '<span class="tag tag-warn">Stale</span>' : ''}
        ${_qualityBadge(s.entry_quality_score, '', {
          edge: s.edge_score, conf: s.confidence_score, pop: s.probability_of_profit,
        })}
      </div>
      <span class="collapsible-chevron" aria-hidden="true"></span>
    </div>
    <div class="collapsible-preview">
      <span>PoP <strong>${fmtPct(econ.pop)}</strong></span>
      <span>Credit <strong>₹${fmt(econ.np)}</strong>/u</span>
      <span>Max loss <strong>₹${fmt(econ.ml)}</strong></span>
      ${s.dte != null ? `<span>DTE <strong>${s.dte}</strong></span>` : ''}
    </div>`;
  // attach live lot-count recalc after DOM insert — see bindSuggestionActions
  const bodyHtml = `
    ${gateBanner}
    <div class="card-id-row">
      <span class="id-chip" title="Suggestion ID">${escapeHtml(s.suggestion_id || '—')}</span>
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
        const debitOrMtm = isDebitStrategy(s.strategy) || econ.sl == null;
        if (debitOrMtm) {
          const mtmNote = isDebitStrategy(s.strategy)
            ? `<span class="muted" style="font-size:.75rem;display:block;margin-top:2px">Spot SL not used — ${slExitPlanText(s.strategy, econ.ml)}</span>`
            : '';
          return `<div><span class="k">Stop loss</span><br><span class="v">MTM-based${mtmNote}</span></div>`;
        }
        if (!twoSided) {
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
      <div><span class="k">Est. net P&amp;L</span><br><span class="v econ-npnl">${formatPnlWithPct(econ.npnl, sugPremium, { useGrossSign: false })}</span></div>
      <div><span class="k">DTE</span><br><span class="v">${s.dte ?? '—'}</span></div>
    </div>
    ${execOrderBanner(s.legs, s.strategy, 'entry')}
    <div class="legs-grid">${legsHtml}</div>
    ${creditBreakdownHtml(s.legs, 'suggest')}
    ${readOnly ? '' : (canExecute ? `
    <div class="exec-spot-bar">
      <div class="sl-monitor-label" style="margin-bottom:6px">Nifty spot at execution</div>
      <div class="exec-spot-row">
        <div class="sl-field">
          <label class="sl-label">Your actual Nifty spot <span class="muted" style="font-size:.7rem">(suggested ₹${fmt(s.spot_at_generation)})</span></label>
          <input type="number" step="1" class="sl-input exec-spot-input"
                 placeholder="e.g. ${Math.round(s.spot_at_generation || 0)}">
        </div>
        ${usesSpotStopLoss(s.strategy, econ.sl) ? `
        <div class="sl-field">
          <label class="sl-label">Adjusted SL level</label>
          <span class="sl-prem-val exec-adj-sl">₹${fmt(econ.sl)}</span>
          <span class="muted exec-adj-note" style="font-size:.72rem">(suggested, fill spot to adjust)</span>
        </div>` : `
        <div class="sl-field">
          <label class="sl-label">Exit on loss</label>
          <span class="sl-prem-val">MTM-based</span>
          <span class="muted exec-adj-note" style="font-size:.72rem">${escapeHtml(slExitPlanText(s.strategy, econ.ml))}</span>
        </div>`}
      </div>
    </div>
    <div class="btn-row" style="margin-top:12px">
      <button class="btn btn-accent btn-mark-exec">Mark Executed</button>
      <button class="btn btn-ghost btn-ignore">Ignore</button>
    </div>` : '')}`;

  if (readOnly) {
    const detailsCls = inlineHeader
      ? 'orig-sug-details orig-sug-details-action'
      : 'orig-sug-details';
    const detailsStyle = inlineHeader ? '' : ' style="margin-top:10px"';
    return `<details class="${detailsCls}"${detailsStyle}>
      <summary class="orig-sug-summary">\ud83d\udccb Original suggestion</summary>
      <div class="orig-sug-body">${summaryHtml}${bodyHtml}</div>
    </details>`;
  }
  const cardAttrs = `
    data-sug-id="${escapeHtml(s.suggestion_id)}"
    data-strategy="${escapeHtml(s.strategy || '')}"
    data-dte="${s.dte != null ? parseInt(s.dte, 10) : ''}"
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
    data-short-put-strike="${((s.legs||[]).find(l=>l.action==='SELL'&&l.option_type==='PE')||{}).strike||''}"`;
  return wrapCollapsibleCard(summaryHtml, bodyHtml, {
    open: expanded && !isRetired,
    className: [canExecute ? '' : 'suggestion-not-executable', isRetired ? 'suggestion-retired' : ''].filter(Boolean).join(' '),
    attrs: cardAttrs.trim(),
  });
}

function bindFlagResetButtons() {
  $$('.btn-flag-reset').forEach(btn => {
    if (btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', async () => {
      const key = btn.dataset.flagKey;
      const val = btn.dataset.flagValue === 'true';
      if (!key) return;
      const confirmMsg = key === 'circuit_breaker_active'
        ? 'Reset the daily P&L circuit breaker?\n\nThis will re-enable executions. The flag will re-trigger tonight at 20:50 IST if aggregate open-trade losses still breach the limit.'
        : `Set runtime flag "${key}" to ${val}?`;
      if (!window.confirm(confirmMsg)) return;
      btn.disabled = true;
      const origText = btn.textContent;
      btn.textContent = 'Working\u2026';
      try {
        await API(`/api/runtime-flags/${encodeURIComponent(key)}`, {
          method: 'POST',
          body: JSON.stringify({ value: val }),
        });
        toast(`Flag "${key}" set to ${val}`, 'info');
        loadSuggestion();
      } catch (e) {
        toast(`Failed to update flag: ${e.message}`, 'err');
        btn.disabled = false;
        btn.textContent = origText;
      }
    });
  });
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
        if (tc) {
          const strat = card.dataset.strategy || '';
          const dteVal = parseInt(card.dataset.dte || '', 10);
          const dte = Number.isFinite(dteVal) ? dteVal : null;
          const tgt = legTargetClosePrice(action, price, strat, dte);
          if (tgt != null) tc.textContent = fmt(tgt);
        }
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
      const npnlEl = card.querySelector('.econ-npnl');
      if (npnlEl) {
        npnlEl.innerHTML = formatPnlWithPct(liveNpnl, premiumFromCreditTotal(liveTotalCredit), { useGrossSign: false });
      }
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
        if (!usesSpotStopLoss(card.dataset.strategy, card.dataset.baseSl)) return;
        const spot = parseFloat(inp.value);
        const sugSl   = parseFloat(card.dataset.baseSl)    || 0;
        const sugSpot = parseFloat(card.dataset.spotAtGen) || 0;
        const adjSlEl  = card.querySelector('.exec-adj-sl');
        const noteEl   = card.querySelector('.exec-adj-note');
        if (!adjSlEl || !noteEl) return;
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
    if (btn.disabled) return;
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
    const spotSlStrategy = usesSpotStopLoss(card.dataset.strategy, sugSl);
    const adjSl = (spotSlStrategy && spotVal != null && !isNaN(spotVal) && spotVal > 0 && sugSl > 0)
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
    c.className=''; c.innerHTML = data.trades.map((t, i) => renderTrade(t, i === 0)).join('');
    try {
      const snap = await API('/api/live/mtm/snapshot');
      _bootstrapLiveLevelsForTrades(data.trades, snap);
    } catch (_) {
      _bootstrapLiveLevelsForTrades(data.trades, { trades: {} });
    }
    // Phase 3 — #3: open SSE stream once after each trades render so live
    // MTM cells (.live-mtm[data-trade-id="..."]) update without polling.
    ensureLiveMTMStream();
    bindConfChips();
    $$('.btn-complete-trade').forEach(b => b.addEventListener('click', e => {
      openSupplementForm(e.target.dataset.tradeId);
    }));
    data.trades.forEach(t => {
      const legs = t.legs || [];
      const hasExecutedLegs = legs.some(l => l.executed);
      if (hasExecutedLegs) {
        openCloseForm(t.trade_id, parseFloat(t.net_credit_actual) || 0);
      }
    });
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
    bindGapReplayPanels();
    bindCollapsibleCardInteractions(c);
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
  const content = panel.querySelector('.close-trade-content');
  if (!content) return;
  content.innerHTML = '<div class="muted">Loading legs…</div>';
  try {
    const [data, sugg, snap] = await Promise.all([
      API(`/api/trades/${tradeId}/executed-legs`),
      API(`/api/trades/${tradeId}/close-suggestion`).catch(() => ({legs: [], est_gross_pnl: 0})),
      API('/api/live/mtm/snapshot').catch(() => ({trades: {}})),
    ]);
    if (!data.legs.length) {
      content.innerHTML = '<div class="muted">No executed legs found.</div>'; return;
    }
    const liveLtps = (snap.trades && snap.trades[tradeId] && snap.trades[tradeId].leg_ltps) || {};
    const marketOpen = _inMarketHours();
    const mktPriceMap = {};  // leg_order → current market price
    const priceSrcMap = {};
    (sugg.legs || []).forEach(s => {
      priceSrcMap[s.leg_order] = s.price_source || 'mid';
    });
    data.legs.forEach(l => {
      const live = _lookupLegLtp(liveLtps, l.symbol, l.strike, l.option_type);
      if (live != null && live > 0) {
        mktPriceMap[l.leg_order] = live;
        priceSrcMap[l.leg_order] = 'live';
      } else if (!marketOpen) {
        const s = (sugg.legs || []).find(x => x.leg_order === l.leg_order);
        if (s && s.suggested_close > 0) {
          mktPriceMap[l.leg_order] = s.suggested_close;
          priceSrcMap[l.leg_order] = s.price_source || 'mid';
        }
      }
    });
    const closeStrategy = (data.legs[0] && data.legs[0].strategy) || '';

    // ── LEFT: read-only live market price display ──
    const liveLegsHtml = data.legs.map(l => {
      const closeAction = l.action === 'SELL' ? 'Buy back' : 'Sell back';
      const lotsUsed = l.lots_actual || l.lots || 1;
      const mp = (mktPriceMap[l.leg_order] != null && mktPriceMap[l.leg_order] > 0)
                 ? mktPriceMap[l.leg_order] : null;
      const psrc = priceSrcMap[l.leg_order];
      const legKey = _normLegKey(l.symbol, l.strike, l.option_type);
      let priceDisplay = mp != null ? `\u20b9${fmt(mp)}` : '<span class="muted">—</span>';
      if (mp != null && psrc === 'intrinsic_fallback') priceDisplay += ' <span class="tag tag-warn" style="font-size:.65rem">intrinsic est.</span>';
      return `<div class="cf-live-leg"
                   data-leg-order="${l.leg_order}"
                   data-action="${escapeHtml(l.action)}"
                   data-fill-price="${l.fill_price || 0}"
                   data-lots="${lotsUsed}"
                   data-lot-size="${l.lot_size || 1}">
        <span class="muted" style="font-size:.78rem">${escapeHtml(closeAction)}</span>
        <strong>${escapeHtml(l.symbol)} ${l.strike} ${escapeHtml(l.option_type)}</strong>
        <span class="cf-live-price" data-leg-key="${escapeHtml(legKey)}"
              data-ltp="${mp != null ? mp : ''}">${priceDisplay}</span>
      </div>`;
    }).join('');

    // ── RIGHT: blank input fields for actual fills ──
    const fillLegsHtml = data.legs.map(l => {
      const closeAction = l.action === 'SELL' ? 'Buy back' : 'Sell back';
      const lotsUsed = l.lots_actual || l.lots || 1;
      const prefill = l.exit_price != null ? l.exit_price : '';
      return `
        <div class="leg-exit-row" data-leg-order="${l.leg_order}"
             data-action="${escapeHtml(l.action)}"
             data-fill-price="${l.fill_price || 0}"
             data-lots="${lotsUsed}"
             data-lot-size="${l.lot_size || 1}">
          <div class="leg-exit-head">
            ${execStepBadge(data.legs, l, closeStrategy, 'close')}
            <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'}">${escapeHtml(l.action)}</span>
            <strong>${escapeHtml(l.symbol)} ${l.strike} ${escapeHtml(l.option_type)}</strong>
            <span class="muted" style="font-size:.8rem">Entry \u20b9${fmt(l.fill_price)} \u00d7 ${lotsUsed} lots</span>
          </div>
          <div class="leg-exit-input">
            <span class="muted" style="font-size:.8rem">${escapeHtml(closeAction)} @ \u20b9</span>
            <input type="number" step="0.05" class="close-price" data-leg="${l.leg_order}"
                   value="${prefill}" placeholder="Enter actual fill">
          </div>
        </div>`;
    }).join('');

    const closePremium = tradePremiumFromLegs(data.legs);

    content.innerHTML = `
        <div class="close-two-col">

          <!-- LEFT: live market snapshot -->
          <div class="close-col close-col-live">
            <div id="live-feed-banner-${escapeHtml(tradeId)}" class="live-feed-banner" hidden></div>
            <div class="close-col-title">📡 Current market prices</div>
            <div class="cf-live-legs">${liveLegsHtml}</div>
            <div class="live-pnl-preview" id="live-pnl-${escapeHtml(tradeId)}"${closePremium ? ` data-premium-rs="${closePremium.rs}" data-premium-kind="${closePremium.kind}"` : ''}>
              <div class="live-pnl-label">If you close now</div>
              <div>Gross P&amp;L: <strong class="live-pnl-gross">\u2014</strong><span class="live-pnl-gross-pct pnl-pct-bracket muted"></span></div>
              <div class="muted" style="font-size:.82rem">Est. charges: <strong class="live-pnl-charges">\u2014</strong></div>
              <div>Net P&amp;L: <strong class="live-pnl-value">\u2014</strong><span class="live-pnl-pct pnl-pct-bracket muted"></span></div>
            </div>
          </div>

          <!-- RIGHT: actual fills entry -->
          <div class="close-col close-col-fills">
            ${execOrderFillsSection(data.legs, closeStrategy, '\u270f\ufe0f Enter your actual fills')}
            <div class="leg-exit-grid">${fillLegsHtml}</div>
            <div class="fill-pnl-preview" id="fill-pnl-${escapeHtml(tradeId)}"${closePremium ? ` data-premium-rs="${closePremium.rs}" data-premium-kind="${closePremium.kind}"` : ''}>
              <div class="live-pnl-label">P&amp;L based on your fills</div>
              <div>Gross P&amp;L: <strong class="fill-pnl-gross">\u2014</strong><span class="fill-pnl-gross-pct pnl-pct-bracket muted"></span></div>
              <div class="muted" style="font-size:.82rem">Est. charges: <strong class="fill-pnl-charges">\u2014</strong></div>
              <div>Net P&amp;L: <strong class="fill-pnl-value">\u2014</strong><span class="fill-pnl-pct pnl-pct-bracket muted"></span></div>
            </div>
            <div class="btn-row" style="margin-top:8px">
              <button class="btn btn-danger btn-close-submit" data-trade-id="${escapeHtml(tradeId)}">Confirm &amp; record fills</button>
            </div>
          </div>

        </div>`;

    // ── Recalc helper (shared by both panels) ──
    function _calcPnl(rows, priceSelector) {
      let gross = 0; let allFilled = true;
      const entryTxns = [], exitTxns = [];
      rows.forEach(row => {
        const action = row.dataset.action;
        const entryPrice = parseFloat(row.dataset.fillPrice) || 0;
        const lots = parseInt(row.dataset.lots) || 1;
        const lotSize = parseInt(row.dataset.lotSize) || 1;
        const closePrice = parseFloat(priceSelector(row));
        if (isNaN(closePrice) || closePrice <= 0) { allFilled = false; return; }
        gross += action === 'SELL'
          ? (entryPrice - closePrice) * lots * lotSize
          : (closePrice - entryPrice) * lots * lotSize;
        entryTxns.push({ action, fill_price: entryPrice, lots, lot_size: lotSize });
        exitTxns.push({ action: action === 'SELL' ? 'BUY' : 'SELL', fill_price: closePrice, lots, lot_size: lotSize });
      });
      if (!allFilled) return null;
      const charges = estChargesOneSide([...entryTxns, ...exitTxns]);
      return { gross, charges, net: gross - charges };
    }

    function _renderPnl(container, result) {
      if (!container) return;
      const prem = _premiumFromDataset(container) || closePremium;
      const isFill = container.classList.contains('fill-pnl-preview');
      const grossEl = container.querySelector(isFill ? '.fill-pnl-gross' : '.live-pnl-gross');
      const grossPctEl = container.querySelector(isFill ? '.fill-pnl-gross-pct' : '.live-pnl-gross-pct');
      const chargesEl = container.querySelector(isFill ? '.fill-pnl-charges' : '.live-pnl-charges');
      const netEl = container.querySelector(isFill ? '.fill-pnl-value' : '.live-pnl-value');
      const netPctEl = container.querySelector(isFill ? '.fill-pnl-pct' : '.live-pnl-pct');
      if (!netEl) return;
      if (!result) {
        _setPnlLine(grossEl, grossPctEl, null, prem);
        if (chargesEl) chargesEl.textContent = '\u2014';
        _setPnlLine(netEl, netPctEl, null, prem);
        container.querySelectorAll('.close-loss-warn').forEach(el => el.remove());
        return;
      }
      const { gross, charges, net } = result;
      _setPnlLine(grossEl, grossPctEl, gross, prem);
      if (chargesEl) chargesEl.textContent = `\u20b9${fmt(charges)}`;
      _setPnlLine(netEl, netPctEl, net, prem);
      let warnEl = container.querySelector('.close-loss-warn');
      if (net < 0) {
        if (!warnEl) { warnEl = document.createElement('div'); warnEl.className = 'close-loss-warn'; container.appendChild(warnEl); }
        warnEl.innerHTML = `\u26a0 Loss of <strong>\u20b9${fmt(Math.abs(net))}</strong>${formatPnlPctText(net, prem)} — verify before confirming.`;
      } else if (warnEl) warnEl.remove();
    }

    // LEFT panel: recalc from live price display spans
    function recalcLivePnl() {
      const rows = [...content.querySelectorAll('.cf-live-leg')];
      const result = _calcPnl(rows, row => row.querySelector('.cf-live-price')?.dataset.ltp);
      _renderPnl(content.querySelector('.live-pnl-preview'), result);
    }

    // RIGHT panel: recalc from user fill inputs
    function recalcFillPnl() {
      const rows = [...content.querySelectorAll('.leg-exit-row')];
      const result = _calcPnl(rows, row => row.querySelector('.close-price')?.value);
      _renderPnl(content.querySelector('.fill-pnl-preview'), result);
    }

    content.querySelectorAll('.close-price').forEach(inp => inp.addEventListener('input', recalcFillPnl));
    recalcFillPnl();
    recalcLivePnl();
    const hasLivePrices = data.legs.some(l =>
      (mktPriceMap[l.leg_order] > 0) && priceSrcMap[l.leg_order] === 'live');
    const usingEod = !marketOpen && data.legs.some(l => mktPriceMap[l.leg_order] > 0);
    _updateFeedTag(tradeId, { forCloseForm: true, hasLivePrices, usingEod });
    // Live prices come from SSE only during market hours — do NOT poll
    // close-suggestion (EOD bhavcopy) as it fights live ticks and flickers.

    panel.querySelector('.btn-close-submit').addEventListener('click', () =>
      submitClose(tradeId, content));
  } catch (err) {
    content.innerHTML = `<div class="muted">Error: ${escapeHtml(err.message)}</div>`;
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
    const fillPreview = panel.querySelector('.fill-pnl-preview');
    const pnlEl = fillPreview?.querySelector('.fill-pnl-value');
    const pctEl = fillPreview?.querySelector('.fill-pnl-pct');
    const pnlText = pnlEl && pnlEl.textContent !== '\u2014'
      ? ` \u00b7 Net P&L ${pnlEl.textContent}${pctEl ? pctEl.textContent : ''}`
      : '';
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
  return renderSuggestion(s, true, [], true);
}

function _gapReplayDecisionLabel(decision) {
  return (decision || 'HOLD').replace(/_/g, ' ');
}

function _gapReplayFlagTags(flags) {
  if (!flags || !flags.length) return '';
  const labels = {
    sl_hit: 'SL',
    pre_breach: 'Pre-SL',
    target: 'Target',
    thesis: 'Thesis fail',
  };
  return flags.map(f => {
    const cls = f === 'sl_hit' || f === 'thesis' ? 'tag tag-err tag-sm'
      : f === 'target' ? 'tag tag-ok tag-sm'
      : 'tag tag-warn tag-sm';
    return `<span class="${cls}">${escapeHtml(labels[f] || f)}</span>`;
  }).join(' ');
}

function renderGapReplayBody(data, premiumInfo) {
  if (!data || data.error) {
    return `<div class="muted" style="font-size:.8rem">${escapeHtml(data?.error || 'Replay unavailable')}</div>`;
  }
  if (!data.has_gap || !data.days || !data.days.length) {
    const last = data.monitor_last_seen
      ? `Last monitor snapshot: ${escapeHtml(data.monitor_last_seen)}.`
      : 'No live monitor snapshots yet.';
    return `<div class="muted" style="font-size:.8rem">${last} No EOD gap days to replay through ${escapeHtml(data.replay_through || 'today')}.</div>`;
  }

  let alertHtml = '';
  const fa = data.first_actionable;
  if (fa && (fa.decision === 'SL_HIT' || fa.decision === 'THESIS_FAIL')) {
    alertHtml = `<div class="gap-replay-alert">
      Would have triggered <strong>${escapeHtml(_gapReplayDecisionLabel(fa.decision))}</strong>
      at EOD on <strong>${escapeHtml(fa.date)}</strong>
      (MTM ${formatPnlWithPct(fa.mtm, premiumInfo)} vs SL \u2212\u20b9${fmt(data.sl_threshold_rs)}).
    </div>`;
  } else if (fa && fa.decision === 'TAKE_PROFIT') {
    alertHtml = `<div class="gap-replay-alert ok">
      Would have hit <strong>take profit</strong> at EOD on <strong>${escapeHtml(fa.date)}</strong>
      (MTM ${formatPnlWithPct(fa.mtm, premiumInfo)}).
    </div>`;
  } else {
    alertHtml = `<div class="gap-replay-alert ok">
      No SL / thesis / target breach at EOD across ${data.days.length} gap day(s).
    </div>`;
  }

  const rows = data.days.map(d => {
    const mtmCls = d.mtm >= 0 ? 'pnl-profit' : 'pnl-loss';
    const flagCls = (d.flags && d.flags.length) ? `flag-${d.flags[0]}` : '';
    return `<tr class="${flagCls}">
      <td>${escapeHtml(d.date)}</td>
      <td>${d.dte}</td>
      <td class="${mtmCls}">${formatPnlWithPct(d.mtm, premiumInfo)}</td>
      <td>\u2212\u20b9${fmt(d.sl_threshold_rs)}</td>
      <td>${escapeHtml(_gapReplayDecisionLabel(d.decision))}</td>
      <td>${_gapReplayFlagTags(d.flags)}</td>
    </tr>`;
  }).join('');

  const meta = [
    data.monitor_last_seen ? `Monitor last seen ${escapeHtml(data.monitor_last_seen)}` : null,
    data.replay_from ? `Replay ${escapeHtml(data.replay_from)} \u2192 ${escapeHtml(data.replay_through)}` : null,
    data.sl_label ? `SL: ${escapeHtml(data.sl_label)} (\u2212\u20b9${fmt(data.sl_threshold_rs)})` : null,
  ].filter(Boolean).join(' \u00b7 ');

  return `${alertHtml}
    <div class="muted" style="font-size:.75rem;margin-bottom:6px">${meta}</div>
    <table class="gap-replay-tbl">
      <thead><tr>
        <th>Date</th><th>DTE</th><th>EOD MTM</th><th>SL</th><th>Decision</th><th>Flags</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="gap-replay-note">${escapeHtml(data.disclaimer || '')}</div>`;
}

async function loadGapReplayPanel(section) {
  const tradeId = section.dataset.tradeId;
  const body = section.querySelector('.gap-replay-body');
  if (!tradeId || !body || body.dataset.loaded === '1') return;
  body.innerHTML = '<div class="muted" style="font-size:.8rem">Loading replay\u2026</div>';
  try {
    const data = await API(`/api/trades/${tradeId}/gap-replay`);
    body.innerHTML = renderGapReplayBody(data, _premiumFromDataset(section));
    body.dataset.loaded = '1';
    if (!data.has_gap) {
      section.querySelector('.gap-replay-chevron').textContent = '\u2014';
    }
  } catch (err) {
    body.innerHTML = `<div class="muted" style="font-size:.8rem">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function bindGapReplayPanels() {
  $$('.gap-replay-section').forEach(section => {
    const head = section.querySelector('.gap-replay-head');
    const body = section.querySelector('.gap-replay-body');
    if (!head || !body) return;
    section.hidden = false;
    const toggle = () => {
      const open = body.hidden;
      body.hidden = !open;
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
      section.querySelector('.gap-replay-chevron').textContent = open ? '\u25b2' : '\u25bc';
      if (open) loadGapReplayPanel(section);
    };
    head.addEventListener('click', toggle);
    head.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });
}

function renderTrade(t, expanded = true) {
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
    const _tradeDte = (t.suggestion && t.suggestion.dte != null)
      ? parseInt(t.suggestion.dte, 10) : null;
    const _netCr = t.net_credit_actual != null ? parseFloat(t.net_credit_actual)
      : (t.suggestion && t.suggestion.net_credit != null
        ? parseFloat(t.suggestion.net_credit) : null);
    const isCreditTrade = _netCr != null && _netCr > 0;
    const isDebitTrade  = _netCr != null && _netCr < 0;
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
        const tc = legTargetClosePrice(l.action, l.fill_price, _tradeStrategy, _tradeDte);
        const capLabel = legTargetCloseCaption(l.action, _tradeStrategy, _tradeDte);
        const lotsUsed = l.lots_actual || l.lots || 1;
        const lotSize = l.lot_size || 1;
        const qty = lotsUsed * lotSize;
        const closeVerb = l.action === 'SELL' ? 'Buy back' : 'Sell back';
        const sign = l.action === 'SELL' ? '\u2264' : '\u2265';
        const priceBit = tc != null
          ? `${closeVerb} ${sign} <strong>\u20b9${fmt(tc)}</strong>`
          : closeVerb;
        return `<div class="target-row">
          <span class="tag ${l.action === 'SELL' ? 'tag-err' : 'tag-ok'} tag-sm">${escapeHtml(l.action||'')}</span>
          <span><strong>${escapeHtml(l.symbol||'')} ${l.strike||''} ${escapeHtml(l.option_type||'')}</strong></span>
          <span>${priceBit} <span class="muted">(${capLabel} \u00b7 entry \u20b9${fmt(l.fill_price)} \u00d7 ${qty}u)</span></span>
        </div>`;
      }).join('');
      let footer = '';
      if (isCreditTrade) {
        footer = `<div class="target-exit-keep">Keep ~\u20b9${fmt(targetPct)} of the \u20b9${fmt(netCreditActual * totalQty)} total credit received</div>`;
      } else if (isDebitTrade) {
        const debit = Math.abs(_netCr);
        const mult = longPremiumTargetMult(_tradeDte);
        const targetGain = Math.round(debit * mult * 10) / 10;
        footer = `<div class="target-exit-keep">Close when position gains ~\u20b9${fmt(targetGain)}/unit (${Math.round(mult * 100)}% of \u20b9${fmt(debit)} debit paid)</div>`;
      }
      const titleLabel = isDebitTrade
        ? `${Math.round(longPremiumTargetMult(_tradeDte) * 100)}% debit gain target`
        : `${tradePctLabel} credit capture`;
      targetSummaryHtml = `<div class="target-exit-box">
        <div class="target-exit-title">\u{1F3AF} Target exit (${titleLabel})</div>
        ${targetRows}
        ${footer}
      </div>`;
    }
    legsHtml = `<div class="trade-legs-section">
      ${targetSummaryHtml}
      ${(() => {
        // Entry-order banner when there are still pending (un-executed) legs to fill.
        const pending = legs.filter(l => !l.executed);
        const tradeStrategy3 = (t.suggestion && t.suggestion.strategy) || '';
        return pending.length > 1 ? execOrderBanner(pending, tradeStrategy3, 'entry') : '';
      })()}
      <div class="trade-legs-grid">${(() => {
        const tradeStrategy = (t.suggestion && t.suggestion.strategy) || '';
        const tradeDte = (t.suggestion && t.suggestion.dte != null)
          ? parseInt(t.suggestion.dte, 10) : null;
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
              <span class="leg-exit-info">\u21b3 Closed @ \u20b9${fmt(l.exit_price)}${l.leg_pnl != null ? ` &nbsp;${formatLegPnlHtml(l)}` : ''}</span>
            </div>
          </div>`;
        } else if (done) {
          const targetClose = legTargetClosePrice(l.action, l.fill_price, tradeStrategy, tradeDte);
          const capLabel = legTargetCloseCaption(l.action, tradeStrategy, tradeDte);
          const closeHint = targetClose != null
            ? (l.action === 'SELL'
              ? `<span class="leg-target-close">Target buy back \u2264 \u20b9${fmt(targetClose)} <span class="muted" style="font-size:.72rem">(${capLabel})</span></span>`
              : `<span class="leg-target-close">Target sell back \u2265 \u20b9${fmt(targetClose)} <span class="muted" style="font-size:.72rem">(${capLabel})</span></span>`)
            : `<span class="leg-target-close muted">Target close: ${capLabel}</span>`;
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

  const _entryQualBadge = _qualityBadge(t.entry_quality_score, 'Entry quality: ');
  const _tradePremium = tradePremiumFromLegs(legs);
  const _premAttrs = _tradePremium
    ? ` data-premium-rs="${_tradePremium.rs}" data-premium-kind="${_tradePremium.kind}"`
    : '';
  const summaryHtml = `
    <div class="card-head collapsible-card-head">
      <h3>${escapeHtml(t.trade_name || t.trade_id)}</h3>
      <div class="card-head-tags">
        ${(() => {
          const ra = t.risk_alert;
          if (!ra || !ra.notif_type) return '';
          const cls =
            ra.notif_type === 'TARGET_HIT'         ? 'tag tag-ok'   :
            ra.notif_type === 'TARGET_LOCKED'      ? 'tag tag-ok'   :
            ra.notif_type === 'PROFIT_FLOOR_SET'   ? 'tag tag-ok'   :
            ra.notif_type === 'PROFIT_FLOOR_HIT'   ? 'tag tag-warn' :
            ra.notif_type === 'LOSS_LIMIT_HIT'     ? 'tag tag-err'  :
            ra.notif_type === 'THESIS_FAIL'        ? 'tag tag-err'  :
            ra.notif_type === 'SL_TRIGGER'         ? 'tag tag-err'  :
            ra.notif_type === 'SHORT_LEG_STRESS'   ? 'tag tag-warn' :
            ra.notif_type === 'PRE_BREACH_WARNING' ? 'tag tag-muted' : 'tag';
          const icon =
            ra.notif_type === 'TARGET_HIT'         ? '\u2705 '  :
            ra.notif_type === 'TARGET_LOCKED'      ? '\ud83d\udd12 ' :
            ra.notif_type === 'PROFIT_FLOOR_SET'   ? '\ud83d\udd12 ' :
            ra.notif_type === 'PROFIT_FLOOR_HIT'   ? '\u26a0\ufe0f ' :
            ra.notif_type === 'LOSS_LIMIT_HIT'     ? '\ud83d\uded1 ' :
            ra.notif_type === 'THESIS_FAIL'        ? '\ud83d\uded1 ' :
            ra.notif_type === 'SL_TRIGGER'         ? '\ud83d\uded1 ' :
            ra.notif_type === 'SHORT_LEG_STRESS'   ? '\u26a0\ufe0f ' :
            ra.notif_type === 'PRE_BREACH_WARNING' ? '\u2139\ufe0f ' : '';
          const tip = (ra.title || ra.notif_type) +
                      (ra.body ? ` — ${ra.body}` : '');
          return `<span class="${cls} risk-alert-static" title="${escapeHtml(tip)}">${icon}${escapeHtml(ra.notif_type.replace(/_/g, ' '))}</span>`;
        })()}
        <span class="tag live-risk-live" data-trade-id="${escapeHtml(t.trade_id)}" hidden></span>
        ${hasPendingClose ? `<span class="tag tag-warn" title="Record exit prices to compute P&L">CLOSE PENDING</span>` : ''}
        <span class="tag tag-${t.daily_status === 'EXIT_AT_OPEN' ? 'warn' : 'ok'}">
          ${escapeHtml(t.daily_status || t.status)}</span>
        <span class="tag tag-current-pnl live-mtm" data-trade-id="${escapeHtml(t.trade_id)}"${_premAttrs} title="Current profit/loss">
          <span class="cpnl-label">Current P&amp;L</span> <strong class="cpnl-val">\u2014</strong><span class="cpnl-pct-bracket muted"></span>
        </span>
        <span class="tag tag-warn live-feed-tag" data-trade-id="${escapeHtml(t.trade_id)}" title="Checking feed\u2026">\u2026</span>
        ${_entryQualBadge}
        <button type="button" class="btn btn-danger btn-void-trade card-head-btn" data-trade-id="${escapeHtml(t.trade_id)}">
          Void Trade</button>
      </div>
      <span class="collapsible-chevron" aria-hidden="true"></span>
    </div>
    <div class="collapsible-preview">
      ${t.suggestion?.strategy ? `<span>${escapeHtml(t.suggestion.strategy)}</span>` : ''}
      ${_tradePremium ? `<span>${_tradePremium.kind === 'received' ? 'Premium received' : 'Premium paid'} <strong>\u20b9${fmt(_tradePremium.rs)}</strong></span>` : (t.net_credit_actual != null ? `<span>Entry <strong>\u20b9${fmt(t.net_credit_actual)}</strong>/u</span>` : '')}
      ${t.spot_at_execution != null ? `<span>Spot @ entry <strong>\u20b9${fmt(t.spot_at_execution)}</strong></span>` : ''}
    </div>`;
  const bodyHtml = `
    ${renderTradeActionPanel(t)}
    ${(() => {
      const liveProfitHtml = renderLiveProfitLevels(t);
      const slMonitorHtml = `
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
          const debitSpreadSl = ['BULL_CALL_SPREAD', 'BEAR_PUT_SPREAD'].includes(
            (t.suggestion && t.suggestion.strategy) || ''
          );
          if (debitSpreadSl) {
            const ml = t.actual_max_loss != null ? parseFloat(t.actual_max_loss)
                     : (t.suggestion && t.suggestion.max_loss != null ? parseFloat(t.suggestion.max_loss) : null);
            const mtmSl = ml != null ? ml * 0.5 : null;
            const dir = (t.suggestion && t.suggestion.strategy) === 'BEAR_PUT_SPREAD'
              ? 'rally hurts this bear put'
              : 'decline hurts this bull call';
            return `<div class="sl-field" style="grid-column:1/-1">
              <label class="sl-label">Stop loss (MTM)</label>
              <span class="sl-prem-val">${mtmSl != null ? `\u20b9${fmt(mtmSl)} loss` : '50% of max debit'}</span>
              <span class="muted sl-prem-note">No Nifty spot trigger \u2014 ${dir}. Ignore legacy spot level if shown in old trades.</span>
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
      </div>
      <div class="sl-action-note">
        <strong>Spot SL</strong> = underlying crosses stored level (two-sided spreads: close breached side only).<br>
        <strong>Loss limit</strong> and <strong>profit floor</strong> are MTM-based — see Live profit levels.
      </div>
    </div>`;
      return `<div class="risk-levels-pair">${liveProfitHtml}${slMonitorHtml}</div>`;
    })()}
    ${hasPendingClose ? `<div class="pending-close-alert">\u26a0 Exit fills not recorded \u2014 use Close Trade below to compute P&amp;L</div>` : ''}
    ${hasExecutedLegs ? `
    <section class="gap-replay-section" id="gap-replay-${escapeHtml(t.trade_id)}" data-trade-id="${escapeHtml(t.trade_id)}"${_premAttrs} hidden>
      <div class="gap-replay-head" role="button" tabindex="0" aria-expanded="false">
        <span class="gap-replay-title">EOD replay (while monitor was off)</span>
        <span class="gap-replay-chevron">\u25bc</span>
      </div>
      <div class="gap-replay-body" hidden>
        <div class="muted" style="font-size:.8rem">Loading replay\u2026</div>
      </div>
    </section>` : ''}
    ${legsHtml}
    ${t.exit_instruction ? `<p class="muted" style="margin:8px 0 0">Exit: ${escapeHtml(t.exit_instruction)}</p>` : ''}
    ${brokenHtml}
    ${isPartial ? `<div class="btn-row" style="margin-top:10px">
      <button class="btn btn-warn btn-complete-trade" data-trade-id="${escapeHtml(t.trade_id)}">
        Complete Trade</button>
    </div>` : ''}
    ${hasExecutedLegs ? `
    <section class="close-trade-section" id="close-${escapeHtml(t.trade_id)}">
      <div class="close-trade-header sl-monitor-label">Close Trade</div>
      <div class="close-trade-content"><div class="muted">Loading…</div></div>
    </section>` : ''}
    ${isPartial ? `<div class="supplement-panel" id="supp-${escapeHtml(t.trade_id)}" hidden></div>` : ''}`;
  return wrapCollapsibleCard(summaryHtml, bodyHtml, { open: expanded });
}

// ---------------- Tab 3: History ----------------

function _localDateStr(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function _fillHistSelect(el, values, curVal, blankLabel) {
  if (!el) return;
  const cur = curVal != null ? curVal : el.value;
  const seen = new Set(values || []);
  el.innerHTML = `<option value="">${escapeHtml(blankLabel)}</option>`;
  (values || []).forEach(v => {
    const o = document.createElement('option');
    o.value = v;
    o.textContent = v;
    if (v === cur) o.selected = true;
    el.appendChild(o);
  });
  // Keep the active filter visible even when facet lists omit it.
  if (cur && !seen.has(cur)) {
    const o = document.createElement('option');
    o.value = cur;
    o.textContent = cur;
    o.selected = true;
    el.appendChild(o);
  }
}

function _histFilterSummary(count, noun) {
  const n = count || 0;
  return n === 1 ? `Showing 1 ${noun}` : `Showing ${n} ${noun}s`;
}

// ---- Sub-tab switcher ----
let _histActiveSubtab = 'trades';
document.querySelectorAll('.hist-subtab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.hist-subtab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _histActiveSubtab = btn.dataset.htab;
    $('#hist-pane-trades').hidden       = (_histActiveSubtab !== 'trades');
    $('#hist-pane-suggestions').hidden  = (_histActiveSubtab !== 'suggestions');
    $('#hist-pane-performance').hidden  = (_histActiveSubtab !== 'performance');
    $('#hist-pane-charts').hidden       = (_histActiveSubtab !== 'charts');
    if (_histActiveSubtab === 'trades')       loadHistory();
    if (_histActiveSubtab === 'suggestions')  loadHistorySuggestions();
    if (_histActiveSubtab === 'performance')  loadStrategyPerformance();
    if (_histActiveSubtab === 'charts')       loadPnlCharts();
  });
});

function loadHistory() {
  if (_histActiveSubtab !== 'trades') return;
  const c = $('#history-container');
  c.className='loading'; c.textContent='Loading…';

  // Default dates: today and 30 days ago
  const fromEl = $('#hist-from'), toEl = $('#hist-to'), instrEl = $('#hist-instrument');
  const stratEl = $('#hist-strategy'), pnlEl = $('#hist-pnl'), qualEl = $('#hist-quality');
  const summaryEl = $('#hist-summary');
  if (!fromEl.value) { const d = new Date(); d.setDate(d.getDate()-30); fromEl.value = _localDateStr(d); }
  if (!toEl.value)   { toEl.value = _localDateStr(); }

  const params = new URLSearchParams();
  params.set('from_date', fromEl.value);
  params.set('to_date', toEl.value);
  if (instrEl.value) params.set('underlying', instrEl.value);
  if (stratEl.value) params.set('strategy', stratEl.value);
  if (pnlEl.value)   params.set('pnl', pnlEl.value);
  if (qualEl.value)  params.set('quality_band', qualEl.value);

  API('/api/history/closed-trades?' + params).then(data => {
    _fillHistSelect(instrEl, data.underlyings, instrEl.value, 'All instruments');
    _fillHistSelect(stratEl, data.strategies, stratEl.value, 'All strategies');

    if (!data.trades.length) {
      if (summaryEl) summaryEl.textContent = _histFilterSummary(0, 'trade');
      c.className=''; c.innerHTML='<div class="empty">No closed trades match the selected filters.</div>'; return;
    }
    if (summaryEl) summaryEl.textContent = _histFilterSummary(data.count, 'trade');
    c.className='';
    c.innerHTML = data.trades.map(renderHistoryTrade).join('');
  }).catch(e => {
    if (summaryEl) summaryEl.textContent = '';
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  });
}

async function loadHistorySuggestions() {
  if (_histActiveSubtab !== 'suggestions') return;
  const c = $('#hsug-container');
  c.className='loading'; c.textContent='Loading…';

  const fromEl = $('#hsug-from'), toEl = $('#hsug-to');
  const instrEl = $('#hsug-instrument'), statusEl = $('#hsug-status');
  const stratEl = $('#hsug-strategy'), qualEl = $('#hsug-quality');
  const summaryEl = $('#hsug-summary');
  if (!fromEl.value) { const d = new Date(); d.setDate(d.getDate()-30); fromEl.value = _localDateStr(d); }
  if (!toEl.value)   { toEl.value = _localDateStr(); }

  const params = new URLSearchParams();
  params.set('from_date', fromEl.value);
  params.set('to_date',   toEl.value);
  if (instrEl.value)  params.set('underlying', instrEl.value);
  if (statusEl.value) params.set('status',     statusEl.value);
  if (stratEl.value)  params.set('strategy',   stratEl.value);
  if (qualEl.value)   params.set('quality_band', qualEl.value);

  try {
    const data = await API('/api/history/suggestions?' + params);

    _fillHistSelect(instrEl, data.underlyings, instrEl.value, 'All instruments');
    _fillHistSelect(stratEl, data.strategies, stratEl.value, 'All strategies');

    if (!data.suggestions.length) {
      if (summaryEl) summaryEl.textContent = _histFilterSummary(0, 'suggestion');
      c.className=''; c.innerHTML='<div class="empty">No suggestions match the selected filters.</div>'; return;
    }
    if (summaryEl) summaryEl.textContent = _histFilterSummary(data.count, 'suggestion');
    c.className='';
    c.innerHTML = data.suggestions.map(renderHistorySuggestion).join('');
  } catch (e) {
    if (summaryEl) summaryEl.textContent = '';
    c.className=''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

// ---------------- Charts sub-tab ----------------

const CHART_STRATEGY_COLORS = [
  '#38bdf8','#86efac','#fcd34d','#f9a8d4','#c4b5fd',
  '#fdba74','#6ee7b7','#93c5fd','#fca5a5','#d9f99d',
];

function _chartColor(idx) { return CHART_STRATEGY_COLORS[idx % CHART_STRATEGY_COLORS.length]; }

// SVG helpers
function _svgLine(points, color, width = 2) {
  if (points.length < 2) return '';
  const d = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  return `<path d="${d}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linejoin="round" stroke-linecap="round"/>`;
}
function _svgCircle(x, y, r, fill, cls = '', extra = '') {
  return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}" fill="${fill}" class="${cls}" ${extra}/>`;
}
function _svgText(x, y, text, anchor = 'middle', cls = '') {
  return `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${anchor}" class="chart-axis-label ${cls}">${escapeHtml(String(text))}</text>`;
}

function _buildLineChart(trades, strategies, W = 700, H = 320) {
  const PAD = { top: 20, right: 20, bottom: 48, left: 72 };
  const cW = W - PAD.left - PAD.right;
  const cH = H - PAD.top - PAD.bottom;

  // Collect all cumulative series
  // overall: {date, cum}[]
  // per strategy: strategy → {date, cum}[]
  const overallSeries = trades.map(t => ({ date: new Date(t.closed_on), cum: t.cum_pnl_overall, t }));
  const stratSeries = {};
  strategies.forEach(s => { stratSeries[s] = []; });
  trades.forEach(t => {
    if (stratSeries[t.strategy]) stratSeries[t.strategy].push({ date: new Date(t.closed_on), cum: t.cum_pnl_strategy, t });
  });

  if (!overallSeries.length) return '<div class="empty">No data</div>';

  const allDates = overallSeries.map(p => p.date);
  const minDate = new Date(Math.min(...allDates));
  const maxDate = new Date(Math.max(...allDates));
  const dateRange = Math.max(maxDate - minDate, 86400000); // at least 1 day

  // All y values including 0 baseline
  const allY = [0, ...overallSeries.map(p => p.cum),
    ...Object.values(stratSeries).flat().map(p => p.cum)];
  const minY = Math.min(...allY);
  const maxY = Math.max(...allY);
  const yPad = Math.max((maxY - minY) * 0.1, 500);
  const yMin = minY - yPad, yMax = maxY + yPad;
  const yRange = yMax - yMin;

  const toX = d => PAD.left + ((d - minDate) / dateRange) * cW;
  const toY = v => PAD.top + (1 - (v - yMin) / yRange) * cH;
  const zero = toY(0);

  // Y-axis gridlines + labels
  const yTicks = 5;
  let gridLines = '', yLabels = '';
  for (let i = 0; i <= yTicks; i++) {
    const v = yMin + (yRange / yTicks) * i;
    const y = toY(v);
    gridLines += `<line x1="${PAD.left}" y1="${y.toFixed(1)}" x2="${PAD.left + cW}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.06)" stroke-width="1"/>`;
    yLabels += _svgText(PAD.left - 6, y + 4, v >= 1000 ? `₹${(v/1000).toFixed(0)}k` : v <= -1000 ? `-₹${(Math.abs(v)/1000).toFixed(0)}k` : `₹${v.toFixed(0)}`, 'end');
  }

  // Zero line
  const zeroLine = `<line x1="${PAD.left}" y1="${zero.toFixed(1)}" x2="${PAD.left + cW}" y2="${zero.toFixed(1)}" stroke="rgba(255,255,255,.2)" stroke-width="1" stroke-dasharray="4,3"/>`;

  // X-axis labels (up to 6 dates)
  let xLabels = '';
  const xTickCount = Math.min(6, overallSeries.length);
  for (let i = 0; i < xTickCount; i++) {
    const idx = Math.round(i * (overallSeries.length - 1) / Math.max(xTickCount - 1, 1));
    const p = overallSeries[idx];
    const x = toX(p.date);
    const label = p.date.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
    xLabels += _svgText(x, PAD.top + cH + 20, label, 'middle');
  }

  // Strategy lines
  let stratLines = '';
  strategies.forEach((s, si) => {
    const pts = (stratSeries[s] || []).map(p => [toX(p.date), toY(p.cum)]);
    if (pts.length) stratLines += _svgLine(pts, _chartColor(si), 1.5);
  });

  // Overall line (bold white)
  const overallPts = overallSeries.map(p => [toX(p.date), toY(p.cum)]);
  const overallLine = _svgLine(overallPts, '#f1f5f9', 2.5);

  // Profit/loss fill area under overall line
  const areaPath = overallPts.length > 1 ? (() => {
    const d = overallPts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    return `<path d="${d} L${overallPts[overallPts.length-1][0].toFixed(1)},${zero.toFixed(1)} L${overallPts[0][0].toFixed(1)},${zero.toFixed(1)} Z" fill="url(#pnlGrad)" opacity="0.3"/>`;
  })() : '';

  // Trade dots (coloured by win/loss)
  let dots = '';
  trades.forEach(t => {
    const x = toX(new Date(t.closed_on));
    const y = toY(t.cum_pnl_overall);
    const fill = t.net_pnl >= 0 ? '#86efac' : '#fca5a5';
    const prem = premiumInfoFromTrade(t);
    const pnlTxt = (t.net_pnl >= 0 ? '+' : '') + '₹' + Number(t.net_pnl).toLocaleString('en-IN') + formatPnlPctText(t.net_pnl, prem);
    const tip = `${t.trade_name || t.trade_id} | ${t.strategy} | ${pnlTxt}`;
    dots += _svgCircle(x, y, 4, fill, 'chart-dot', `data-tip="${escapeHtml(tip)}"`);
  });

  // Legend
  let legend = `<div class="chart-legend"><span class="chart-legend-item"><span class="chart-legend-line" style="background:#f1f5f9"></span>Overall</span>`;
  strategies.forEach((s, si) => {
    legend += `<span class="chart-legend-item"><span class="chart-legend-line" style="background:${_chartColor(si)}"></span>${escapeHtml(s)}</span>`;
  });
  legend += `</div>`;

  const svg = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.6"/>
        <stop offset="100%" stop-color="#38bdf8" stop-opacity="0"/>
      </linearGradient>
    </defs>
    ${gridLines}${zeroLine}${xLabels}${yLabels}
    ${areaPath}${stratLines}${overallLine}${dots}
  </svg>`;

  return `<div class="chart-title">Cumulative P&L over time</div>${legend}${svg}`;
}

function _buildBarChart(trades, strategies, W = 700, H = 220) {
  const PAD = { top: 20, right: 20, bottom: 48, left: 72 };
  const cW = W - PAD.left - PAD.right;
  const cH = H - PAD.top - PAD.bottom;

  // Per-strategy total P&L and premium deployed
  const totals = {};
  const premTotals = {};
  strategies.forEach(s => { totals[s] = 0; premTotals[s] = 0; });
  trades.forEach(t => {
    totals[t.strategy] = (totals[t.strategy] || 0) + t.net_pnl;
    const premRs = t.premium_rs > 0 ? t.premium_rs : Math.abs(t.net_credit_actual || 0);
    premTotals[t.strategy] = (premTotals[t.strategy] || 0) + premRs;
  });
  const sorted = [...strategies].sort((a, b) => (totals[b] || 0) - (totals[a] || 0));

  if (!sorted.length) return '';
  const vals = sorted.map(s => totals[s]);
  const maxAbs = Math.max(...vals.map(Math.abs), 1);
  const barW = Math.min(60, (cW / sorted.length) * 0.65);
  const gap   = cW / sorted.length;
  const zero  = PAD.top + cH / 2;

  let bars = '', xLabels = '', yLabels = '';
  const yTicks = 4;
  for (let i = -yTicks/2; i <= yTicks/2; i++) {
    const v = (maxAbs / (yTicks/2)) * i;
    const y = zero - (v / maxAbs) * (cH / 2);
    yLabels += `<line x1="${PAD.left}" y1="${y.toFixed(1)}" x2="${PAD.left + cW}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.06)" stroke-width="1"/>`;
    if (i !== 0) yLabels += _svgText(PAD.left - 6, y + 4, Math.abs(v) >= 1000 ? `${v < 0 ? '-' : ''}₹${(Math.abs(v)/1000).toFixed(0)}k` : `₹${v.toFixed(0)}`, 'end');
  }
  yLabels += `<line x1="${PAD.left}" y1="${zero.toFixed(1)}" x2="${PAD.left+cW}" y2="${zero.toFixed(1)}" stroke="rgba(255,255,255,.2)" stroke-width="1"/>`;

  sorted.forEach((s, i) => {
    const v = totals[s] || 0;
    const x = PAD.left + gap * i + gap / 2 - barW / 2;
    const barH = Math.abs(v / maxAbs) * (cH / 2);
    const y = v >= 0 ? zero - barH : zero;
    const fill = v >= 0 ? 'rgba(134,239,172,.75)' : 'rgba(252,165,165,.75)';
    const stratPrem = premTotals[s] > 0 ? { rs: premTotals[s], kind: 'paid' } : null;
    const tip = stratPrem
      ? `${s}: ${formatPnlWithPct(v, stratPrem, { aggregate: true })}`
      : `${s}: ${v >= 0 ? '+' : ''}₹${v.toLocaleString('en-IN', {maximumFractionDigits:0})}`;
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW}" height="${barH.toFixed(1)}" rx="3" fill="${fill}" class="chart-bar" data-tip="${escapeHtml(tip)}"/>`;
    xLabels += _svgText(PAD.left + gap * i + gap / 2, PAD.top + cH + 20, s.replace('_', ' '), 'middle');
  });

  const svg = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" xmlns="http://www.w3.org/2000/svg">
    ${yLabels}${bars}${xLabels}
  </svg>`;
  return `<div class="chart-title">Total P&L by strategy</div>${svg}`;
}

async function loadPnlCharts() {
  const cumEl  = $('#chart-cumulative');
  const barEl  = $('#chart-strategy-bar');
  const sumEl  = $('#chart-summary-row');
  if (!cumEl) return;
  cumEl.className = 'chart-wrap loading'; cumEl.textContent = 'Loading…';
  barEl.className = 'chart-wrap loading'; barEl.textContent = '';

  const fromEl = $('#chart-from'), toEl = $('#chart-to');
  if (!fromEl.value) { const d = new Date(); d.setFullYear(d.getFullYear() - 1); fromEl.value = _localDateStr(d); }
  if (!toEl.value)   { toEl.value = _localDateStr(); }

  const qs = new URLSearchParams();
  if (fromEl.value) qs.set('from_date', fromEl.value);
  if (toEl.value)   qs.set('to_date',   toEl.value);

  try {
    const data = await API('/api/stats/pnl-timeline?' + qs);
    if (!data.trades.length) {
      cumEl.className = 'chart-wrap'; cumEl.innerHTML = '<div class="empty">No closed trades in selected period.</div>';
      barEl.className = 'chart-wrap'; barEl.innerHTML = '';
      sumEl.innerHTML = '';
      return;
    }

    // Summary strip
    const pnlCls = data.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    const totalPrem = data.total_invested > 0 ? { rs: data.total_invested, kind: 'paid' } : null;
    sumEl.innerHTML = `
      <div class="chart-kv"><span>Total net P&amp;L</span><strong class="${pnlCls}">${formatPnlWithPct(data.total_pnl, totalPrem, { aggregate: true })}</strong></div>
      <div class="chart-kv"><span>Total premium deployed</span><strong>₹${fmt(data.total_invested)}</strong></div>
      <div class="chart-kv"><span>Return on premium</span><strong class="${pnlCls}">${data.total_invested > 0 ? (data.total_pnl / data.total_invested * 100).toFixed(1) + '%' : '—'}</strong></div>
      <div class="chart-kv"><span>Total charges paid</span><strong>₹${fmt(data.total_charges)}</strong></div>
      <div class="chart-kv"><span>Trades</span><strong>${data.trades.length}</strong></div>`;

    cumEl.className = 'chart-wrap';
    cumEl.innerHTML = _buildLineChart(data.trades, data.strategies);

    barEl.className = 'chart-wrap';
    barEl.innerHTML = _buildBarChart(data.trades, data.strategies);

    // Tooltip binding
    const tooltip = $('#chart-tooltip');
    document.querySelectorAll('.chart-dot, .chart-bar').forEach(el => {
      el.addEventListener('mouseenter', e => {
        tooltip.textContent = el.dataset.tip;
        tooltip.hidden = false;
      });
      el.addEventListener('mousemove', e => {
        tooltip.style.left = (e.clientX + 12) + 'px';
        tooltip.style.top  = (e.clientY - 28) + 'px';
      });
      el.addEventListener('mouseleave', () => { tooltip.hidden = true; });
    });
  } catch (err) {
    cumEl.className = 'chart-wrap'; cumEl.innerHTML = `<div class="empty">Error: ${escapeHtml(err.message)}</div>`;
  }
}

$('#chart-refresh')?.addEventListener('click', loadPnlCharts);
$('#chart-reset')?.addEventListener('click', () => {
  $('#chart-from').value = '';
  $('#chart-to').value   = '';
  loadPnlCharts();
});

// ---------------- Performance sub-tab ----------------
async function loadStrategyPerformance() {
  const c = $('#perf-container');
  if (!c) return;
  c.className = 'loading'; c.textContent = 'Loading…';
  try {
    const data = await API('/api/stats/strategy-performance');
    if (!data.strategies.length) {
      c.className = ''; c.innerHTML = '<div class="empty">No closed trades found. Close some trades first.</div>'; return;
    }
    c.className = '';
    c.innerHTML = _renderPerfPage(data);
  } catch (e) {
    c.className = ''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

$('#perf-refresh')?.addEventListener('click', loadStrategyPerformance);

function _perfColor(v) {
  if (v == null) return '';
  return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '';
}

function _winBar(wins, total) {
  const pct = total ? Math.round(wins / total * 100) : 0;
  const cls = pct >= 60 ? 'win-bar-high' : pct >= 40 ? 'win-bar-mid' : 'win-bar-low';
  return `<div class="win-bar-wrap" title="${wins} wins / ${total} trades">
    <div class="win-bar-fill ${cls}" style="width:${pct}%"></div>
  </div>`;
}

function _renderPerfPage(data) {
  const ov = data.overall;

  const overallHtml = `
    <div class="perf-overall">
      <div class="perf-overall-title">Overall (all strategies)</div>
      <div class="perf-overall-grid">
        <div class="perf-kv"><span>Total trades</span><strong>${ov.total}</strong></div>
        <div class="perf-kv"><span>Win rate</span><strong class="${ov.win_rate >= 50 ? 'pnl-pos' : 'pnl-neg'}">${ov.win_rate}%</strong></div>
        <div class="perf-kv"><span>Total net P&amp;L</span><strong class="${_perfColor(ov.total_pnl)}">${_perfPnlHtml(ov.total_pnl, ov.total_premium, 'paid', { aggregate: true })}</strong></div>
        <div class="perf-kv"><span>Avg net P&amp;L / trade</span><strong class="${_perfColor(ov.avg_pnl)}">${_perfPnlHtml(ov.avg_pnl, ov.total_premium && ov.total ? ov.total_premium / ov.total : 0, 'paid')}</strong></div>
        <div class="perf-kv"><span>Best trade</span><strong class="pnl-pos">${_perfPnlHtml(ov.best_trade, ov.best_trade_premium_rs, ov.best_trade_premium_kind)}</strong></div>
        <div class="perf-kv"><span>Worst trade</span><strong class="pnl-neg">${_perfPnlHtml(ov.worst_trade, ov.worst_trade_premium_rs, ov.worst_trade_premium_kind)}</strong></div>
        <div class="perf-kv"><span>Profit factor</span><strong class="${ov.losses === 0 ? 'pnl-pos' : ov.profit_factor != null ? (ov.profit_factor >= 1 ? 'pnl-pos' : 'pnl-neg') : ''}">${ov.losses === 0 ? '∞' : ov.profit_factor != null ? ov.profit_factor + 'x' : '—'}</strong></div>
      </div>
    </div>`;

  const stratCards = data.strategies.map(s => {
    const winRateCls = s.win_rate >= 60 ? 'pnl-pos' : s.win_rate >= 40 ? '' : 'pnl-neg';
    return `
    <div class="perf-card ${s.total_pnl >= 0 ? 'perf-card--pos' : 'perf-card--neg'}">
      <div class="perf-card-head">
        <span class="perf-strategy-name">${escapeHtml(s.strategy)}</span>
        <span class="perf-total-pnl ${_perfColor(s.total_pnl)}">${_perfPnlHtml(s.total_pnl, s.total_premium, 'paid', { aggregate: true })}</span>
      </div>
      ${_winBar(s.wins, s.total)}
      <div class="perf-stats-grid">
        <div class="perf-stat">
          <div class="perf-stat-val ${winRateCls}">${s.win_rate}%</div>
          <div class="perf-stat-lbl">Win rate</div>
        </div>
        <div class="perf-stat">
          <div class="perf-stat-val">${s.wins}W / ${s.losses}L</div>
          <div class="perf-stat-lbl">${s.total} trades</div>
        </div>
        <div class="perf-stat">
          <div class="perf-stat-val ${_perfColor(s.avg_pnl)}">${_perfPnlHtml(s.avg_pnl, s.total_premium && s.total ? s.total_premium / s.total : 0, 'paid')}</div>
          <div class="perf-stat-lbl">Avg P&L</div>
        </div>
        <div class="perf-stat">
          <div class="perf-stat-val ${s.losses === 0 ? 'pnl-pos' : s.profit_factor != null ? (s.profit_factor >= 1 ? 'pnl-pos' : 'pnl-neg') : ''}">${s.losses === 0 ? '∞' : s.profit_factor != null ? s.profit_factor + 'x' : '—'}</div>
          <div class="perf-stat-lbl">Profit factor</div>
        </div>
        <div class="perf-stat">
          <div class="perf-stat-val pnl-pos">${_perfPnlHtml(s.avg_win, s.total_premium && s.wins ? s.total_premium / s.total : 0, 'paid')}</div>
          <div class="perf-stat-lbl">Avg win</div>
        </div>
        <div class="perf-stat">
          <div class="perf-stat-val pnl-neg">${_perfPnlHtml(s.avg_loss, s.total_premium && s.losses ? s.total_premium / s.total : 0, 'paid')}</div>
          <div class="perf-stat-lbl">Avg loss</div>
        </div>
        <div class="perf-stat">
          <div class="perf-stat-val pnl-pos">${_perfPnlHtml(s.best_trade, s.best_trade_premium_rs, s.best_trade_premium_kind)}</div>
          <div class="perf-stat-lbl">Best trade</div>
        </div>
        <div class="perf-stat">
          <div class="perf-stat-val pnl-neg">${_perfPnlHtml(s.worst_trade, s.worst_trade_premium_rs, s.worst_trade_premium_kind)}</div>
          <div class="perf-stat-lbl">Worst trade</div>
        </div>
        ${s.avg_hold_days != null ? `<div class="perf-stat"><div class="perf-stat-val">${s.avg_hold_days}d</div><div class="perf-stat-lbl">Avg hold</div></div>` : ''}
        ${s.avg_max_profit != null ? `<div class="perf-stat"><div class="perf-stat-val">₹${fmt(s.avg_max_profit)}</div><div class="perf-stat-lbl">Avg max profit</div></div>` : ''}
      </div>
    </div>`;
  }).join('');

  return `${overallHtml}<div class="perf-cards">${stratCards}</div>`;
}

function renderHistorySuggestion(s) {
  const statusCls = s.status === 'EXECUTED' ? 'tag-ok'
                  : s.status === 'IGNORED'  ? 'tag-warn'
                  : s.status === 'PENDING'  ? 'tag-acc'
                  : 'tag-muted';
  const confLabel = s.confidence_display || formatConfidence(s);
  const confHtml = confLabel
    ? `<span class="muted" style="font-size:.85rem">${escapeHtml(confLabel)} checks passed</span>`
    : (s.confidence_score != null
      ? `<span class="muted" style="font-size:.85rem">${s.confidence_score} checks passed</span>`
      : '');
  return `
  <div class="hist-card" style="border-left-color: var(--accent)">
    <div class="hist-card-head">
      <div class="hist-card-title">
        <strong class="hist-instr">${escapeHtml(s.underlying || '')}</strong>
        <span class="tag tag-accent">${escapeHtml(s.strategy || '')}</span>
        <span class="tag ${statusCls}">${escapeHtml(s.status || '')}</span>
        ${_qualityBadge(s.entry_quality_score)}
        ${s.expiry_type ? `<span class="muted" style="font-size:.78rem">${escapeHtml(s.expiry_type)}</span>` : ''}
      </div>
      <div class="hist-card-pnl">
        ${confHtml}
      </div>
    </div>
    <div class="hist-card-meta muted">
      ${escapeHtml(s.trade_name || s.suggestion_id)}
      &nbsp;·&nbsp;Suggestion Date: ${fmtDt(s.generated_on)}
      ${s.expiry_date ? '&nbsp;·&nbsp;Expiry: '+fmtDt(s.expiry_date) : ''}
      ${s.dte != null ? '&nbsp;·&nbsp;DTE: '+s.dte : ''}
      ${s.entry_date ? '&nbsp;·&nbsp;Execution Date: '+fmtDt(s.entry_date) : ''}
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
  const premium = premiumInfoFromTrade(t);
  const sugPremium = premiumFromSuggestion(s);
  const pnlClass = pnl != null ? (pnl >= 0 ? 'pnl-profit' : 'pnl-loss') : '';
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
      <td class="num ${lpc}">${l.leg_pnl != null ? formatPnlWithPct(l.leg_pnl, legPremiumFromLeg(l)) : '—'}</td>
    </tr>`;
  }).join('');

  return `
  <div class="hist-card">
    <div class="hist-card-head">
      <div class="hist-card-title">
        <strong class="hist-instr">${escapeHtml(s.underlying || t.trade_id)}</strong>
        <span class="tag tag-accent">${escapeHtml(s.strategy || '')}</span>
        <span class="tag ${statusCls}">${escapeHtml(t.status || '')}</span>
        ${_qualityBadge(t.entry_quality_score, 'Entry quality: ')}
        ${t.position_type ? `<span class="muted" style="font-size:.78rem">${escapeHtml(t.position_type)}</span>` : ''}
      </div>
      <div class="hist-card-pnl ${pnlClass}">${pnl != null ? formatPnlWithPct(pnl, premium) : '—'}</div>
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
      ${cmp('Gross P&amp;L',     '—',                  t.gross_pnl != null ? formatPnlWithPct(t.gross_pnl, premium, { useGrossSign: true }) : '—')}
      ${premium ? cmp(premium.kind === 'received' ? 'Premium received' : 'Premium paid', '—', r(premium.rs)) : ''}
      ${cmp('Charges / tax',     r(s.est_charges),     r(t.total_charges))}
      ${cmp('Net P&amp;L',       s.est_net_pnl != null ? formatPnlWithPct(s.est_net_pnl, sugPremium, { useGrossSign: false }) : '—',     `<span class="${pnlClass} hcmp-bold">${pnl != null ? formatPnlWithPct(pnl, premium, { useGrossSign: true }) : '—'}</span>`)}
      ${cmp('Spot at entry',     s.spot != null ? fmt(s.spot) : '—',  t.spot_at_execution != null ? fmt(t.spot_at_execution) : '—')}
      ${cmp('Upper breakeven',   s.upper_be != null ? fmt(s.upper_be) : '—',  t.actual_upper_be != null ? fmt(t.actual_upper_be) : '—')}
      ${cmp('Lower breakeven',   s.lower_be != null ? fmt(s.lower_be) : '—',  t.actual_lower_be != null ? fmt(t.actual_lower_be) : '—')}
      ${cmp('Stop loss level',   s.stop_loss != null ? fmt(s.stop_loss) : '—',  t.actual_stop_loss != null ? fmt(t.actual_stop_loss) : '—')}
      ${s.pop != null ? cmp('Prob. of profit', fmtPct(s.pop), '—') : ''}
      ${s.confidence != null ? cmp('Confidence', formatConfidence({ confidence_score: s.confidence }) || `${s.confidence} passed`, '—') : ''}
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
    const logs = await API('/api/logs?' + params);
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
$('#hist-strategy').addEventListener('change', loadHistory);
$('#hist-pnl').addEventListener('change', loadHistory);
$('#hist-quality').addEventListener('change', loadHistory);
$('#hist-from').addEventListener('change', loadHistory);
$('#hist-to').addEventListener('change', loadHistory);

// History filter bindings — Suggestions sub-tab
$('#hsug-refresh').addEventListener('click', loadHistorySuggestions);
$('#hsug-instrument').addEventListener('change', loadHistorySuggestions);
$('#hsug-strategy').addEventListener('change', loadHistorySuggestions);
$('#hsug-quality').addEventListener('change', loadHistorySuggestions);
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

// ---------------- Suggestion Quality Score ----------------
function _qualityLabelFromScore(score) {
  const cls = score >= 80 ? 'qs-excellent'
            : score >= 65 ? 'qs-good'
            : score >= 50 ? 'qs-fair'
            : score >= 35 ? 'qs-weak'
            :               'qs-poor';
  const label = score >= 80 ? 'Excellent'
              : score >= 65 ? 'Good'
              : score >= 50 ? 'Fair'
              : score >= 35 ? 'Weak'
              :               'Poor';
  return { score, cls, label };
}

function _qualityTooltip(score, detail) {
  let tip = `Quality score ${score}/100 (stored at suggestion generation)`;
  if (!detail) return tip;
  const e = detail.edge != null ? parseFloat(detail.edge) : null;
  const c = detail.conf != null ? detail.conf : null;
  const p = detail.pop != null ? parseFloat(detail.pop) : null;
  if (e != null) tip += `\nEdge score: ${e.toFixed(0)}`;
  if (c != null) tip += `\nConfidence gates: ${c}/14`;
  if (p != null) tip += `\nPoP: ${p.toFixed(0)}%`;
  return tip;
}

function _qualityBadge(storedScore, prefix = '', detail = null) {
  if (storedScore == null || storedScore === '') return '';
  const score = parseInt(storedScore, 10);
  if (isNaN(score)) return '';
  const q = _qualityLabelFromScore(score);
  const tip = _qualityTooltip(score, detail);
  return `<span class="quality-score ${q.cls}" title="${escapeHtml(tip)}">${prefix}${q.score}<span class="qs-label">${q.label}</span></span>`;
}

// ---------------- Phase 3 #3: Live MTM SSE consumer ----------------
let _liveMTMSource = null;

function _applyMtmEvent(m) {
  _updateCurrentPnlBadge(m.trade_id, m.mtm, m.as_of, true);
  _updateLiveProfitLevels(m.trade_id, m);

  // Update LEFT panel live price spans with latest leg LTPs
  if (m.leg_ltps && typeof m.leg_ltps === 'object') {
    _applyLegLtpsToClosePanel(m.trade_id, m.leg_ltps);
    _updateFeedTag(m.trade_id, { forCloseForm: true, hasLivePrices: true });
  }
}

function ensureLiveMTMStream() {
  if (_liveMTMSource && _liveMTMSource.readyState !== 2 /* CLOSED */) return;
  try {
    _liveMTMSource = new EventSource('/api/live/mtm');
    _liveMTMSource.onmessage = (ev) => {
      try { _applyMtmEvent(JSON.parse(ev.data)); } catch (_) { /* ignore */ }
    };
    _liveMTMSource.onerror = () => {
      // Browser auto-reconnects; just clear stale ref so next render reopens.
      if (_liveMTMSource && _liveMTMSource.readyState === 2) _liveMTMSource = null;
    };
  } catch (_) { /* SSE unsupported — silently degrade */ }
}

// ---------------- Tab 6: Jobs (scheduler monitor + manual trigger) ----------------
const JOB_STATUS_META = {
  RUNNING: { label: 'Running',   cls: 'js-running'  },
  SUCCESS: { label: 'Success',   cls: 'js-success'  },
  FAILED:  { label: 'Failed',    cls: 'js-failed'   },
  NO_DATA: { label: 'No data',   cls: 'js-no-data'  },
  SKIPPED: { label: 'Skipped',   cls: 'js-skipped'  },
  NEVER:   { label: 'Never run', cls: 'js-never'    },
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

function renderJobsGrid(jobs) {
  let html = '';
  let lastGroup = null;
  for (const j of jobs) {
    const group = j.display_group || 'Other';
    if (group !== lastGroup) {
      html += `<div class="jobs-section-head">${escapeHtml(group)}</div>`;
      lastGroup = group;
    }
    html += renderJobCard(j);
  }
  return html;
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
    c.innerHTML = `<div class="jobs-grid">${renderJobsGrid(data.jobs)}</div>`;
  } catch (e) {
    c.className = ''; c.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderJobCard(j) {
  const sm = JOB_STATUS_META[j.status] || JOB_STATUS_META.NEVER;
  const isRunning  = j.status === 'RUNNING';
  const isFailed   = j.status === 'FAILED';
  const isNoData   = j.status === 'NO_DATA';
  const isSkipped  = j.status === 'SKIPPED';
  const cardCls = `job-card${isRunning ? ' job-card--running' : isFailed ? ' job-card--failed' : isNoData ? ' job-card--no-data' : ''}`;

  const dur = _jobDuration(j.started_at, j.finished_at);
  const lastRunIso = j.finished_at || j.started_at;
  const lastRunRel = _jobRelTime(lastRunIso);
  const nextRunRel = _jobRelTime(j.next_run);

  const msgIcon = isFailed ? '⚠' : isNoData ? '📭' : 'ℹ';
  const errLine = ((isFailed || isNoData || isSkipped) && j.error_message)
    ? `<div class="job-error job-error--${j.status.toLowerCase()}">${msgIcon} ${escapeHtml(String(j.error_message).slice(0, 300))}</div>`
    : '';

  const rowsLine = (j.rows_processed != null)
    ? `<div class="job-meta-row"><span>Rows</span><span>${escapeHtml(String(j.rows_processed))}</span></div>`
    : '';

  const scheduleNote = j.via_pipeline
    ? (j.pipeline_parent === 'morning_eod_catchup'
        ? ' • via Morning EOD @ 09:00'
        : ' • via EOD pipeline')
    : (j.cron_enabled === false || !j.enabled ? ' • manual only' : '');

  const triggerBtn = `<button class="btn job-trigger-btn" data-job="${escapeHtml(j.job_name)}"
      ${isRunning || j.manual_enabled === false ? 'disabled' : ''}>
      ${isRunning ? '⏳ Running…' : '▶ Run now'}
    </button>`;

  const stepBadge = (j.via_pipeline && j.pipeline_step)
    ? `<span class="job-pipeline-step">Step ${j.pipeline_step}</span>`
    : '';

  return `<div class="${cardCls}">
    <div class="job-card-head">
      <span class="job-icon">${escapeHtml(j.icon)}</span>
      <div class="job-title-block">
        <div class="job-name">${escapeHtml(j.display_name)}${stepBadge}</div>
        <div class="job-schedule">${escapeHtml(j.schedule)}${scheduleNote}</div>
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
  if (!confirm(`Trigger "${jobName}" now?\n\nMissing weekdays in the last month will be filled automatically.`)) return;

  try {
    await API(`/api/jobs/${encodeURIComponent(jobName)}/trigger`, { method: 'POST' });
    toast(`Job queued: ${jobName} (auto backfill)`, 'ok');
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
  // Treat naive strings as IST by appending +05:30.
  const s = iso && /[Z+\-]\d{2}:\d{2}$|Z$/.test(iso) ? iso : iso + '+05:30';
  const t = new Date(s).getTime();
  if (!isFinite(t)) return '—';
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60)   return sec + 's ago';
  if (sec < 3600) return Math.round(sec / 60) + 'm ago';
  return Math.round(sec / 3600) + 'h ago';
}
// Format any ISO timestamp (with or without TZ offset) as IST wall time
// e.g.  "2026-05-20T11:50:29+00:00"  →  "20 May 17:20:29 IST"
function _fmtIst(iso) {
  // Accepts both:
  //   naive IST  "2026-05-20T17:20:29"        (new from ws_monitor)
  //   UTC-aware  "2026-05-20T11:50:29+00:00"  (old snapshots / transition)
  if (!iso) return '—';
  try {
    // If no timezone info, treat as IST by appending +05:30 before parsing.
    const s = /[Z+\-]\d{2}:\d{2}$|Z$/.test(iso) ? iso : iso + '+05:30';
    const d = new Date(s);
    if (!isFinite(d.getTime())) return iso;
    return d.toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit', month: 'short',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    }) + ' IST';
  } catch { return iso; }
}
function _wsmonStateClass(state) {
  const s = (state || '').toLowerCase();
  if (s === 'connected')                       return 'wsmon-state-ok';
  if (s === 'connecting' || s === 'unknown')   return 'wsmon-state-warn';
  if (s === 'degraded')                        return 'wsmon-state-warn';
  if (s === 'stale' || s === 'token_expired' || s === 'disconnected' || s === 'stopped') return 'wsmon-state-err';
  return 'wsmon-state-warn';
}

async function loadWsMonitor({ silent = false } = {}) {
  if (!silent) loadZerodhaStatus();
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
    // Outside NSE market hours (09:15–15:30 IST Mon–Fri) show "off-market"
    // instead of "connected" — the WS stays up but no ticks flow, so showing
    // "connected" alongside "stale" is confusing.
    const _nowIst = new Date(new Date().toLocaleString('en-US', {timeZone:'Asia/Kolkata'}));
    const _dow = _nowIst.getDay(); // 0=Sun,6=Sat
    const _hhmm = _nowIst.getHours() * 100 + _nowIst.getMinutes();
    const _inMarket = _dow >= 1 && _dow <= 5 && _hhmm >= 915 && _hhmm <= 1530;
    const _rawRunner = snap.runner_state || snap.connection_state || 'unknown';
    const runnerState = (!_inMarket && _rawRunner === 'connected') ? 'off-market' : _rawRunner;
    const runnerStateClass = runnerState === 'off-market' ? 'wsmon-state-warn' : stateClass;
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
          <div class="wsmon-card-value ${runnerStateClass}">${escapeHtml(runnerState)}</div>
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
        Snapshot @ ${escapeHtml(_fmtIst(snap.generated_at))} &middot; uptime ${Math.round(snap.uptime_seconds || 0)}s
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
        const isOption = e.strike != null && e.option_type;
        const instrLabel = isOption
          ? `${e.symbol || '?'} <strong>${fmt(e.strike)} ${e.option_type}</strong>`
          : (e.symbol || '?');
        const typeTag = isOption
          ? `<span class="wsmon-opt-tag">${e.option_type === 'PE' ? '🔴 PUT' : '🟢 CALL'}</span>`
          : `<span class="wsmon-idx-tag">IDX</span>`;
        detail = `${typeTag} <span class="wsmon-ev-sym">${instrLabel}</span> <span class="wsmon-ev-px">@ ${px}</span>`;
      } else if (tag === 'connection_state') {
        detail = `<span class="wsmon-ev-state ${_wsmonStateClass(e.state)}">${escapeHtml(e.state || '?')}</span>${e.detail ? ` <span class="muted">${escapeHtml(String(e.detail))}</span>` : ''}`;
      } else if (tag === 'token_expired') {
        detail = `<span class="wsmon-ev-state wsmon-state-err">token_expired</span>`;
      } else {
        detail = escapeHtml(JSON.stringify(e));
      }
      return `<div class="wsmon-ev wsmon-ev-${escapeHtml(tag)}">
        <span class="wsmon-ev-ts">${escapeHtml(_fmtIst(e.ts))}</span>
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
refreshGlobalBanners();
// Keep global banner strip live — new CRITICAL alerts appear within a minute.
setInterval(refreshGlobalBanners, 60000);

// ---------------- Zerodha session card ----------------
async function loadZerodhaStatus() {
  const el = document.getElementById('zerodha-status');
  const headerBtn = document.getElementById('zerodha-login-btn');
  const headerIcon = document.getElementById('zerodha-login-icon');
  const headerLabel = document.getElementById('zerodha-login-label');
  // Defensive: always force the header pill to point at the OAuth redirect
  // route (works even if a stale HTML cache still has the old hash href).
  if (headerBtn) {
    headerBtn.setAttribute('href', '/zerodha/login');
    headerBtn.removeAttribute('target');
  }
  try {
    const r = await fetch('/api/zerodha/status');
    const d = await r.json();
    _zerodhaHasSession = !!d.has_session;
    _zerodhaValid = !!d.valid;
    const callbackEl = document.getElementById('zerodha-callback-url');
    if (callbackEl && d.redirect_url) {
      callbackEl.textContent = d.redirect_url;
    }
    const manual = !!d.kite_manual_paste_flow;
    const httpsWarn = document.getElementById('zerodha-https-warning');
    const stepsAuto = document.getElementById('zerodha-steps-auto');
    const stepsManual = document.getElementById('zerodha-steps-manual');
    const kiteConsoleEl = document.getElementById('zerodha-kite-console-url');
    if (httpsWarn) {
      httpsWarn.style.display = manual ? 'block' : 'none';
    }
    if (stepsAuto) stepsAuto.style.display = manual ? 'none' : '';
    if (stepsManual) stepsManual.style.display = manual ? '' : 'none';
    if (kiteConsoleEl && d.kite_console_redirect_url) {
      kiteConsoleEl.textContent = d.kite_console_redirect_url;
    }
    if (headerBtn) {
      if (manual) {
        headerBtn.setAttribute('target', '_blank');
        headerBtn.setAttribute('rel', 'noopener');
        headerBtn.title = 'Opens Kite login in a new tab — paste request_token back here when done.';
      } else {
        headerBtn.removeAttribute('target');
        headerBtn.removeAttribute('rel');
      }
    }
    _refreshAllFeedTags();
    // Update header pill (always present)
    if (headerBtn && headerIcon && headerLabel) {
      if (!d.has_session) {
        headerIcon.textContent = '🔑';
        headerLabel.textContent = 'Login';
        headerBtn.style.color = '#b91c1c';
        headerBtn.title = 'Zerodha — no session. Click to log in.';
      } else if (d.valid) {
        headerIcon.textContent = '✓';
        headerLabel.textContent = 'Zerodha';
        headerBtn.style.color = '#047857';
        headerBtn.title = `Zerodha session valid (user_id ${d.user_id || ''}).`;
      } else {
        headerIcon.textContent = '⚠';
        headerLabel.textContent = 'Re-login';
        headerBtn.style.color = '#b45309';
        headerBtn.title = 'Zerodha session expired. Click to re-login.';
      }
    }
    // Update Config-tab card if present
    if (el) {
      if (!d.has_session) {
        el.innerHTML = '<span style="color:#b91c1c;font-weight:600;">✗ No session.</span> ' +
          'Click <em>Open Login Flow</em> to mint today\'s token.';
      } else if (d.valid) {
        el.innerHTML = `<span style="color:#047857;font-weight:600;">✓ Valid</span> ` +
          `&middot; user_id <code>${escapeHtml(d.user_id || '')}</code> ` +
          `&middot; generated <code>${escapeHtml(d.generated_at || '')}</code>`;
      } else {
        el.innerHTML = '<span style="color:#b45309;font-weight:600;">⚠ Expired.</span> ' +
          'Re-login required (token resets daily at 06:00 IST).';
      }
    }
  } catch (e) {
    if (el) el.textContent = 'Status unavailable: ' + e;
  }
}

// ============================================================
// Notifications Tab (dedicated panel)
// ============================================================

const _NF_CAT_LABELS = {
  sl:         { icon: '⛔', label: 'Stop Loss' },
  profit:     { icon: '🎯', label: 'Take Profit' },
  exit:       { icon: '🚪', label: 'Exit Required' },
  event:      { icon: '📅', label: 'Event Risk' },
  system:     { icon: '⚙️', label: 'System' },
  suggestion: { icon: '💡', label: 'Suggestion' },
  other:      { icon: '📋', label: 'Other' },
};

const _NF_TYPE_CAT = {
  SL_TRIGGER: 'sl', SL_HIT: 'sl', PRE_BREACH_WARNING: 'sl',
  LOSS_LIMIT_HIT: 'sl', THESIS_FAIL: 'sl', PROFIT_FLOOR_HIT: 'sl', SHORT_LEG_STRESS: 'sl',
  TARGET_HIT: 'profit', TAKE_PROFIT: 'profit', TARGET_LOCKED: 'profit',
  PROFIT_FLOOR_SET: 'profit',
  EXIT_TOMORROW: 'exit', TIME_DECAY_DONE: 'exit', EXPIRE: 'exit', AUTO_SETTLED: 'exit',
  EVENT_AHEAD_REVIEW: 'event',
  CIRCUIT_BREAKER: 'system', BROKEN_TRADE: 'system', DATA_REPAIR: 'system', KILL_SWITCH: 'system',
  NEW_SUGGESTION: 'suggestion', NO_SUGGESTION: 'suggestion', STRATEGY_VETO: 'suggestion',
};

let _nfState = {
  sev:     '',    // '' | 'CRITICAL' | 'WARNING' | 'INFO'
  cat:     '',    // '' | 'sl' | 'profit' | 'exit' | 'event' | 'system' | 'suggestion'
  unread:  false,
  tradeId: '',
  from:    '',
  to:      '',
  offset:  0,
  limit:   25,
  total:   0,
};

function _nfBuildQuery() {
  const p = new URLSearchParams({ limit: _nfState.limit, offset: _nfState.offset });
  if (_nfState.sev)     p.set('severity', _nfState.sev);
  if (_nfState.cat)     p.set('category', _nfState.cat);
  if (_nfState.unread)  p.set('unread', '1');
  if (_nfState.tradeId) p.set('trade_id', _nfState.tradeId.trim());
  if (_nfState.from)    p.set('from', _nfState.from);
  if (_nfState.to)      p.set('to', _nfState.to);
  return p.toString();
}

function _nfSevIcon(sev) {
  if (!sev) return '';
  const s = sev.toUpperCase();
  if (s === 'CRITICAL') return '🔴';
  if (s === 'WARNING')  return '🟠';
  return '🔵';
}

function _nfRenderCard(n) {
  const sev       = (n.severity  || 'INFO').toUpperCase();
  const nt        = (n.notif_type || '').toUpperCase();
  const cat       = _NF_TYPE_CAT[nt] || 'other';
  const catMeta   = _NF_CAT_LABELS[cat] || _NF_CAT_LABELS.other;
  const unread    = !n.is_read;
  const stripeClass = sev === 'CRITICAL' ? 'nf-card-stripe-critical'
                    : sev === 'WARNING'  ? 'nf-card-stripe-warning'
                    : 'nf-card-stripe-info';
  const tradeLink = n.related_trade_id
    ? `<a href="#trades" class="nf-trade-link" data-trade="${escapeHtml(n.related_trade_id)}"
          title="Jump to trade">${escapeHtml(n.related_trade_id)}</a>`
    : '';
  const sugLink = n.related_suggestion_id
    ? `<span>SUG: ${escapeHtml(n.related_suggestion_id)}</span>` : '';

  const card = document.createElement('div');
  card.className = 'nf-card' + (unread ? ' unread' : '');
  card.dataset.nid = n.id;
  card.innerHTML = `
    <div class="nf-card-stripe ${stripeClass}"></div>
    <div class="nf-card-body">
      <div class="nf-card-row1">
        ${unread ? '<span class="nf-card-unread-dot" title="Unread"></span>' : ''}
        <span class="notif-sev notif-sev-${sev.toLowerCase()}">${_nfSevIcon(sev)} ${escapeHtml(sev)}</span>
        <span class="nf-card-type">${escapeHtml(catMeta.icon)} ${escapeHtml(nt || '—')}</span>
        <span class="nf-card-title">${escapeHtml(n.title || '')}</span>
        ${unread ? `<button class="nf-card-read-btn" title="Mark read">✓ Read</button>` : ''}
      </div>
      ${n.body ? `<p class="nf-card-body-text">${escapeHtml(n.body)}</p>` : ''}
      <div class="nf-card-meta">
        <span>${fmtDt(n.created_at)}</span>
        ${tradeLink}
        ${sugLink}
        ${n.is_read ? `<span class="muted">Read ${fmtDt(n.read_at)}</span>` : ''}
      </div>
    </div>`;

  // Mark-read on the button
  const readBtn = card.querySelector('.nf-card-read-btn');
  if (readBtn) {
    readBtn.addEventListener('click', async e => {
      e.stopPropagation();
      await API(`/api/notifications/${n.id}/read`, { method: 'POST' });
      card.classList.remove('unread');
      card.querySelector('.nf-card-unread-dot')?.remove();
      readBtn.remove();
      refreshNotifBadge();
      refreshGlobalBanners();
      _nfRefreshStats();
    });
  }

  // Click card body → mark read + optionally jump to trade
  card.addEventListener('click', async () => {
    if (unread) {
      await API(`/api/notifications/${n.id}/read`, { method: 'POST' }).catch(()=>{});
      card.classList.remove('unread');
      card.querySelector('.nf-card-unread-dot')?.remove();
      card.querySelector('.nf-card-read-btn')?.remove();
      refreshNotifBadge();
      refreshGlobalBanners();
      _nfRefreshStats();
    }
  });

  // Trade link — switch tab without full reload
  card.querySelector('.nf-trade-link')?.addEventListener('click', e => {
    e.stopPropagation();
    switchTab('trades');
  });

  return card;
}

async function _nfRefreshStats() {
  try {
    const st = await API('/api/notifications/stats');
    const total = st.total_unread || 0;
    // Update sidebar badge
    const badge = document.getElementById('notif-tab-badge');
    if (badge) { badge.textContent = total; badge.hidden = total === 0; }

    // Update chip counts (show only non-zero)
    const sevMap = st.by_severity || {};
    const catMap = st.by_category || {};
    const update = (id, val) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (val > 0) { el.textContent = val; el.classList.add('visible'); }
      else         { el.textContent = ''; el.classList.remove('visible'); }
    };
    update('nf-cnt-sev-critical',  sevMap['CRITICAL']  || 0);
    update('nf-cnt-sev-warning',   sevMap['WARNING']   || 0);
    update('nf-cnt-sev-info',      sevMap['INFO']      || 0);
    update('nf-cnt-cat-sl',         catMap['sl']         || 0);
    update('nf-cnt-cat-profit',     catMap['profit']     || 0);
    update('nf-cnt-cat-exit',       catMap['exit']       || 0);
    update('nf-cnt-cat-event',      catMap['event']      || 0);
    update('nf-cnt-cat-system',     catMap['system']     || 0);
    update('nf-cnt-cat-suggestion', catMap['suggestion'] || 0);
  } catch(e) { /* non-fatal */ }
}

async function loadNotifications() {
  const list = document.getElementById('nf-list');
  if (!list) return;
  list.className = 'nf-list loading'; list.textContent = 'Loading…';

  await _nfRefreshStats();

  try {
    const data = await API('/api/notifications?' + _nfBuildQuery());
    const rows  = data.notifications || [];
    _nfState.total = data.total || rows.length;

    // Total label
    const lbl = document.getElementById('nf-total-label');
    if (lbl) {
      const unreadSuffix = _nfState.unread ? ' (unread)' : '';
      lbl.textContent = `${_nfState.total} notification${_nfState.total !== 1 ? 's' : ''}${unreadSuffix}`;
    }

    list.className = 'nf-list';
    list.innerHTML = '';

    if (!rows.length) {
      list.innerHTML = '<div class="nf-empty">No notifications match the current filters.</div>';
    } else {
      rows.forEach(n => list.appendChild(_nfRenderCard(n)));
    }

    // Pagination
    const pg   = document.getElementById('nf-pagination');
    const info = document.getElementById('nf-page-info');
    if (pg) {
      const pages = _nfState.total > _nfState.limit;
      pg.hidden = !pages;
      if (info) {
        const from = _nfState.offset + 1;
        const to   = Math.min(_nfState.offset + rows.length, _nfState.total);
        info.textContent = `${from}–${to} of ${_nfState.total}`;
      }
      document.getElementById('nf-prev').disabled = _nfState.offset === 0;
      document.getElementById('nf-next').disabled = _nfState.offset + _nfState.limit >= _nfState.total;
    }
  } catch(e) {
    list.className = 'nf-list';
    list.innerHTML = `<div class="nf-empty" style="color:var(--err)">Error loading notifications: ${escapeHtml(String(e))}</div>`;
  }
}

// ── Wire up notification tab controls ──

// Severity chips
$$('[data-nf-sev]').forEach(btn => btn.addEventListener('click', () => {
  $$('[data-nf-sev]').forEach(b => b.classList.remove('nf-chip-active'));
  btn.classList.add('nf-chip-active');
  _nfState.sev    = btn.dataset.nfSev;
  _nfState.offset = 0;
  loadNotifications();
}));

// Category chips
$$('[data-nf-cat]').forEach(btn => btn.addEventListener('click', () => {
  $$('[data-nf-cat]').forEach(b => b.classList.remove('nf-chip-active'));
  btn.classList.add('nf-chip-active');
  _nfState.cat    = btn.dataset.nfCat;
  _nfState.offset = 0;
  loadNotifications();
}));

// Unread toggle
document.getElementById('nf-unread-only')?.addEventListener('change', e => {
  _nfState.unread = e.target.checked;
  _nfState.offset = 0;
  loadNotifications();
});

// Trade ID filter (debounced)
let _nfTradeTimer;
document.getElementById('nf-trade-filter')?.addEventListener('input', e => {
  clearTimeout(_nfTradeTimer);
  _nfTradeTimer = setTimeout(() => {
    _nfState.tradeId = e.target.value;
    _nfState.offset  = 0;
    loadNotifications();
  }, 400);
});

// Date range
document.getElementById('nf-from')?.addEventListener('change', e => {
  _nfState.from   = e.target.value ? e.target.value + 'T00:00:00' : '';
  _nfState.offset = 0;
  loadNotifications();
});
document.getElementById('nf-to')?.addEventListener('change', e => {
  _nfState.to     = e.target.value ? e.target.value + 'T23:59:59' : '';
  _nfState.offset = 0;
  loadNotifications();
});

// Pagination
document.getElementById('nf-prev')?.addEventListener('click', () => {
  if (_nfState.offset <= 0) return;
  _nfState.offset = Math.max(0, _nfState.offset - _nfState.limit);
  loadNotifications();
});
document.getElementById('nf-next')?.addEventListener('click', () => {
  if (_nfState.offset + _nfState.limit >= _nfState.total) return;
  _nfState.offset += _nfState.limit;
  loadNotifications();
});

// Refresh button
document.getElementById('nf-refresh')?.addEventListener('click', () => loadNotifications());

// Mark all read
document.getElementById('nf-mark-all')?.addEventListener('click', async () => {
  await API('/api/notifications/read-all', { method: 'POST' });
  refreshNotifBadge();
  refreshGlobalBanners();
  await loadNotifications();
  toast('All notifications marked as read', 'ok');
});

// Refresh stats (header pill + sidebar badge) on boot and every 60s.
_nfRefreshStats();
setInterval(_nfRefreshStats, 60000);

refreshIndexSpotStrip();
setInterval(refreshIndexSpotStrip, 10000);

loadZerodhaStatus();
setInterval(loadZerodhaStatus, 60000);
setInterval(_refreshAllFeedTags, 30000);

// Return from /zerodha/callback server-side exchange (?tab=wsmon&zerodha=ok).
(function _handleZerodhaOAuthReturn() {
  try {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get('tab');
    const ok = params.get('zerodha') === 'ok';
    const err = params.get('zerodha_error');
    if (!tab && !ok && !err) return;
    const finish = () => {
      if (tab === 'wsmon' || tab === 'config') switchTab('wsmon');
      if (ok) {
        toast('Zerodha session saved — live feed should connect shortly.', 'ok');
        loadZerodhaStatus();
        const card = document.getElementById('zerodha-session-card');
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      if (err) {
        toast('Zerodha login failed: ' + decodeURIComponent(err), 'err');
        const inp = document.getElementById('zerodha-token-input');
        if (inp) inp.focus();
      }
      window.history.replaceState({}, document.title, window.location.pathname + '#wsmon');
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', finish, { once: true });
    } else {
      finish();
    }
  } catch (e) { /* non-fatal */ }
})();

// Extract request_token from a pasted string. Accepts:
//   • a full redirect URL (any host:port — we don't care):
//       http://localhost:5000/?action=login&status=success&request_token=ABC123
//   • a query-string fragment:  ?request_token=ABC123&...
//   • the raw token value:      ABC123
function _extractRequestToken(raw) {
  const s = (raw || '').trim();
  if (!s) return '';
  const m = s.match(/[?&]request_token=([^&\s]+)/i);
  if (m) return decodeURIComponent(m[1]);
  // Looks like a bare token if it has no spaces and no '=' / '?'
  if (!/[\s?=]/.test(s)) return s;
  return '';
}

async function _submitZerodhaRequestToken(rt) {
  const inp = document.getElementById('zerodha-token-input');
  const msg = document.getElementById('zerodha-submit-msg');
  const btn = document.getElementById('zerodha-submit-btn');
  const token = _extractRequestToken(rt);
  if (!token) {
    if (msg) { msg.textContent = '⚠ Could not find request_token. Paste the full redirect URL or just the token value.'; msg.style.color = '#c00'; }
    return false;
  }
  if (inp) inp.value = token;
  if (btn) btn.disabled = true;
  if (msg) { msg.textContent = 'Exchanging request_token…'; msg.style.color = ''; }
  try {
    const r = await fetch('/api/zerodha/exchange', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_token: token }),
    });
    const d = await r.json();
    if (d.ok) {
      if (msg) { msg.textContent = `✓ Logged in as ${d.user_id} at ${d.generated_at}`; msg.style.color = '#0a0'; }
      if (inp) inp.value = '';
      loadZerodhaStatus();
      toast('Zerodha session saved — live feed should connect shortly.', 'ok');
      return true;
    }
    if (msg) { msg.textContent = '✗ ' + (d.error || 'exchange failed'); msg.style.color = '#c00'; }
    return false;
  } catch (e) {
    if (msg) { msg.textContent = '✗ ' + e; msg.style.color = '#c00'; }
    return false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Legacy redirect to /?request_token=… — show on WS Monitor, then auto-submit.
(function _autoCaptureRequestToken() {
  try {
    const params = new URLSearchParams(window.location.search);
    const rt = params.get('request_token');
    if (!rt) return;
    const apply = async () => {
      switchTab('wsmon');
      const card = document.getElementById('zerodha-session-card');
      if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      // Strip query string before exchange so refresh can't reuse the token.
      window.history.replaceState({}, document.title, window.location.pathname + '#wsmon');
      const msg = document.getElementById('zerodha-submit-msg');
      if (msg) { msg.textContent = 'request_token captured from URL — submitting…'; msg.style.color = ''; }
      await _submitZerodhaRequestToken(rt);
      return true;
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => { apply(); }, { once: true });
    } else {
      apply();
    }
  } catch (e) { /* non-fatal */ }
})();

// Submit handler — parses paste, posts to /api/zerodha/exchange.
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('#zerodha-submit-btn');
  if (!btn) return;
  const inp = document.getElementById('zerodha-token-input');
  await _submitZerodhaRequestToken(inp ? inp.value : '');
});

// Logout handler — wires up the button on the WS Monitor session card.
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('#zerodha-logout-btn');
  if (!btn) return;
  if (!confirm('Clear Zerodha session and disconnect the WS runner?\n\nYou will need to re-login before the next market open.')) return;
  btn.disabled = true;
  try {
    const r = await fetch('/api/zerodha/logout', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      toast(d.message || 'Session cleared.');
      loadZerodhaStatus();
    } else {
      toast('Logout failed: ' + (d.error || 'unknown'));
    }
  } catch (e) {
    toast('Logout failed: ' + e);
  } finally {
    btn.disabled = false;
  }
});
