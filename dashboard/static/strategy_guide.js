// Strategy learning — UI. Copy lives in strategy_guide.json (single source
// for the Learning page and the ⓘ modal on Suggestion / My Trades).
(function () {
  let _guide = null;
  let _guidePromise = null;

  function _esc(s) {
    if (typeof escapeHtml === 'function') return escapeHtml(s);
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _cacheBust() {
    return (window.__CACHE_BUST__ != null) ? String(window.__CACHE_BUST__) : String(Date.now());
  }

  function loadStrategyGuide() {
    if (_guide) return Promise.resolve(_guide);
    if (_guidePromise) return _guidePromise;
    _guidePromise = fetch('/static/strategy_guide.json?v=' + encodeURIComponent(_cacheBust()))
      .then(r => {
        if (!r.ok) throw new Error('Could not load strategy guide');
        return r.json();
      })
      .then(data => {
        _guide = data;
        window.STRATEGY_GUIDE = data;
        return data;
      })
      .catch(err => {
        _guidePromise = null;
        throw err;
      });
    return _guidePromise;
  }

  function _pct(frac) {
    const n = Number(frac);
    if (isNaN(n)) return '—';
    return Math.round(n * 100) + '%';
  }

  function _rs(n) {
    const v = Number(n);
    if (isNaN(v)) return '—';
    return '₹' + Math.round(v).toLocaleString('en-IN');
  }

  function liveTargetBlurb(code, kind) {
    const rules = (typeof PNL_RULES === 'object' && PNL_RULES) ? PNL_RULES : {};
    if (kind === 'credit') {
      const map = rules.strategy_take_profit_fraction || {};
      const frac = map[code] != null ? map[code] : rules.take_profit_fraction;
      return `This system’s take-profit is ${_pct(frac)} of max profit (credit captured). Green up-triangle on the rail/card = that rupee target printed. It does not scale up just because DTE is larger.`;
    }
    if (kind === 'debit_spread') {
      const frac = rules.debit_spread_target_fraction;
      return `This system’s take-profit is ${_pct(frac)} of the debit paid (not of max profit). Green up-triangle = that debit-fraction target.`;
    }
    const base = rules.long_premium_target_base;
    const scale = rules.long_premium_target_dte_scale;
    const cap = rules.long_premium_target_max;
    const at7 = Math.min(cap, base + 7 / scale);
    return `This system’s take-profit is a DTE-aware multiple of debit paid: base ${_pct(base)} + DTE/${scale}, capped at ${_pct(cap)}. At 7 DTE that is ${_pct(at7)} of debit. More DTE raises the rupee target; it does not make theta safer.`;
  }

  function liveSlBlurb(code) {
    const rules = (typeof PNL_RULES === 'object' && PNL_RULES) ? PNL_RULES : {};
    const row = (rules.strategy_sl_limits || {})[code] || rules.strategy_sl_defaults || {};
    const frac = row.loss_fraction;
    const cap = row.absolute_cap_rs;
    const parts = [];
    if (frac != null) parts.push(_pct(frac) + ' of max loss');
    if (cap != null) parts.push('capped at ' + _rs(cap));
    if (!parts.length) return 'Stop uses this system’s strategy SL table (Config → Profit targets & stop-loss). Red octagon = loss limit hit.';
    return 'Stop-loss MTM is ' + parts.join(', ') + '. Red octagon = that limit hit. Hexagon − = currently losing but not there yet.';
  }

  function familyLabel(guide, familyId) {
    const fam = ((guide && guide.intro && guide.intro.families) || []).find(f => f.id === familyId);
    return fam ? fam.label : familyId;
  }

  function renderStrategyGuideArticle(guide, code, { modal } = {}) {
    const s = guide.strategies && guide.strategies[code];
    if (!s) {
      return `<div class="empty">No learning note for <code>${_esc(code)}</code>.</div>`;
    }
    const ul = (arr) => `<ul class="sg-list">${(arr || []).map(t => `<li>${_esc(t)}</li>`).join('')}</ul>`;
    const look = (rows) => `<dl class="sg-look">${(rows || []).map(r =>
      `<div class="sg-look-row"><dt>${_esc(r.place)}</dt><dd>${_esc(r.why)}</dd></div>`
    ).join('')}</dl>`;
    const openFull = modal
      ? `<p class="sg-open-full"><button type="button" class="btn btn-ghost" data-sg-open-page="${_esc(code)}">Open in Learning page</button></p>`
      : '';
    const pnl = s.pnl || {};
    return `<article class="sg-article" id="guide-${_esc(code)}" data-strategy="${_esc(code)}">
      <header class="sg-article-head">
        <p class="sg-kicker">${_esc(familyLabel(guide, s.family))}</p>
        <h3>${_esc(s.name)} <code>${_esc(code)}</code></h3>
      </header>
      <p class="sg-what">${_esc(s.what)}</p>
      <h4>When this system applies it</h4>
      ${ul(s.when)}
      <h4>Key pointers before you trade</h4>
      ${ul(s.checks)}
      <h4>Profit vs loss</h4>
      <div class="sg-pnl">
        <p><strong>Wins when.</strong> ${_esc(pnl.wins_when || '')}</p>
        <p><strong>Loses when.</strong> ${_esc(pnl.loses_when || '')}</p>
        <p><strong>DTE.</strong> ${_esc(pnl.dte || '')}</p>
        <p class="sg-live"><strong>This system — target.</strong> ${_esc(liveTargetBlurb(code, s.target_kind))}</p>
        <p class="sg-live"><strong>This system — stop.</strong> ${_esc(liveSlBlurb(code))}</p>
      </div>
      <h4>Where to look — Suggestion</h4>
      ${look(s.look && s.look.suggestion)}
      <h4>Where to look — My Trades</h4>
      ${look(s.look && s.look.trade)}
      ${openFull}
    </article>`;
  }

  function renderLearningPageHtml(guide, filter) {
    const q = String(filter || '').trim().toLowerCase();
    const intro = guide.intro || {};
    const fam = (intro.families || []).map(f =>
      `<div class="sg-family"><h3>${_esc(f.label)}</h3><p>${_esc(f.text)}</p></div>`
    ).join('');
    const sig = (intro.signals || []).length
      ? `<ul class="sg-list sg-signals">${intro.signals.map(t => `<li>${_esc(t)}</li>`).join('')}</ul>`
      : '';
    const onCard = (intro.on_card || []).length
      ? `<dl class="sg-look">${intro.on_card.map(r =>
          `<div class="sg-look-row"><dt>${_esc(r.place)}</dt><dd>${_esc(r.why)}</dd></div>`
        ).join('')}</dl>`
      : '';
    const order = guide.order || Object.keys(guide.strategies || {});
    const toc = [];
    const articles = [];
    order.forEach(code => {
      const s = guide.strategies[code];
      if (!s) return;
      const blob = [s.name, code, s.what, s.family].join(' ').toLowerCase();
      if (q && !blob.includes(q) && !(s.when || []).some(t => t.toLowerCase().includes(q))) return;
      toc.push(`<a href="#learn/${_esc(code)}" data-sg-jump="${_esc(code)}">${_esc(s.name)}</a>`);
      articles.push(renderStrategyGuideArticle(guide, code, { modal: false }));
    });
    return `<div class="sg-page">
      <p class="sg-lede">${_esc(intro.lede || '')}</p>
      <div class="sg-families">${fam}</div>
      <h3>Already on the Suggestion / My Trades card</h3>
      ${onCard}
      <h3>Header shapes (this dashboard)</h3>
      ${sig}
      <input type="search" id="sg-filter" class="sg-filter" placeholder="Filter strategies…" value="${_esc(filter || '')}">
      <nav class="sg-toc" aria-label="Strategies">${toc.join('')}</nav>
      ${articles.join('') || '<div class="empty">No strategies match that filter.</div>'}
    </div>`;
  }

  async function renderLearningPage() {
    const c = document.getElementById('learn-container');
    if (!c) return;
    c.className = 'loading';
    c.textContent = 'Loading…';
    try {
      const guide = await loadStrategyGuide();
      const hash = (window.location.hash || '').replace(/^#/, '');
      const want = hash.startsWith('learn/') ? hash.slice(6) : '';
      c.className = '';
      paintLearn(c, guide, '');
      if (want && guide.strategies[want]) {
        const el = document.getElementById('guide-' + want);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    } catch (e) {
      c.className = '';
      c.innerHTML = `<div class="empty">Error: ${_esc(e.message)}</div>`;
    }
  }

  function paintLearn(c, guide, q) {
    c.innerHTML = renderLearningPageHtml(guide, q);
    bindLearnPage(c, guide);
    const filter = document.getElementById('sg-filter');
    if (filter) {
      filter.addEventListener('input', () => {
        const val = filter.value;
        paintLearn(c, guide, val);
        const f2 = document.getElementById('sg-filter');
        if (f2) {
          f2.focus();
          try { f2.setSelectionRange(val.length, val.length); } catch (_) {}
        }
      });
    }
  }

  function bindLearnPage(c, guide) {
    c.querySelectorAll('[data-sg-jump]').forEach(a => {
      a.addEventListener('click', (ev) => {
        ev.preventDefault();
        const code = a.getAttribute('data-sg-jump');
        try { history.replaceState(null, '', '#learn/' + code); } catch (_) {}
        const el = document.getElementById('guide-' + code);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  }

  function ensureModal() {
    let modal = document.getElementById('strategy-guide-modal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'strategy-guide-modal';
    modal.className = 'sg-modal';
    modal.hidden = true;
    modal.innerHTML = `
      <div class="sg-modal-backdrop" data-sg-close="1"></div>
      <div class="sg-modal-panel" role="dialog" aria-modal="true" aria-labelledby="sg-modal-title">
        <button type="button" class="sg-modal-close" data-sg-close="1" aria-label="Close">\u00d7</button>
        <div id="strategy-guide-modal-body"></div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', (ev) => {
      if (ev.target.closest('[data-sg-close]')) closeStrategyGuide();
      const openPage = ev.target.closest('[data-sg-open-page]');
      if (openPage) {
        const code = openPage.getAttribute('data-sg-open-page');
        closeStrategyGuide();
        try { history.replaceState(null, '', '#learn/' + code); } catch (_) {}
        if (typeof switchTab === 'function') switchTab('learn');
        setTimeout(() => {
          const el = document.getElementById('guide-' + code);
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 50);
      }
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && modal && !modal.hidden) closeStrategyGuide();
    });
    return modal;
  }

  async function openStrategyGuide(code) {
    if (!code || code === 'NONE') return;
    const modal = ensureModal();
    const body = document.getElementById('strategy-guide-modal-body');
    body.innerHTML = '<div class="loading">Loading…</div>';
    modal.hidden = false;
    document.body.classList.add('sg-modal-open');
    try {
      const guide = await loadStrategyGuide();
      body.innerHTML = renderStrategyGuideArticle(guide, code, { modal: true });
    } catch (e) {
      body.innerHTML = `<div class="empty">${_esc(e.message)}</div>`;
    }
  }

  function closeStrategyGuide() {
    const modal = document.getElementById('strategy-guide-modal');
    if (modal) modal.hidden = true;
    document.body.classList.remove('sg-modal-open');
  }

  // Capture phase: summary buttons call stopPropagation, which would hide
  // this from a bubble listener and also toggle the trade/suggestion card.
  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.strategy-guide-btn');
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    openStrategyGuide(btn.getAttribute('data-strategy'));
  }, true);

  window.loadStrategyGuide = loadStrategyGuide;
  window.openStrategyGuide = openStrategyGuide;
  window.closeStrategyGuide = closeStrategyGuide;
  window.renderLearningPage = renderLearningPage;
  window.renderStrategyGuideArticle = function (code) {
    return loadStrategyGuide().then(g => renderStrategyGuideArticle(g, code, { modal: false }));
  };

  loadStrategyGuide().catch(() => { /* fail-open until user opens a note */ });
})();
