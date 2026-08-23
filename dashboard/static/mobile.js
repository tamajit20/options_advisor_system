/**
 * Mobile UX — More drawer, sticky trade bar, bottom-nav wiring.
 */
(function () {
  'use strict';

  const MOBILE_MQ = window.matchMedia('(max-width: 1024px)');

  function isMobile() {
    return MOBILE_MQ.matches;
  }

  function openMoreDrawer() {
    const drawer = document.getElementById('more-drawer');
    if (!drawer) return;
    drawer.hidden = false;
    document.body.classList.add('more-drawer-open');
    const moreBtn = document.getElementById('bnav-more-btn');
    if (moreBtn) moreBtn.classList.add('active');
  }

  function closeMoreDrawer() {
    const drawer = document.getElementById('more-drawer');
    if (!drawer) return;
    drawer.hidden = true;
    document.body.classList.remove('more-drawer-open');
    const moreBtn = document.getElementById('bnav-more-btn');
    if (moreBtn) moreBtn.classList.remove('active');
  }

  function bindMoreDrawer() {
    const drawer = document.getElementById('more-drawer');
    if (!drawer || drawer.dataset.bound === '1') return;
    drawer.dataset.bound = '1';

    drawer.querySelector('.more-drawer-backdrop')?.addEventListener('click', closeMoreDrawer);
    drawer.querySelector('[data-more-close]')?.addEventListener('click', closeMoreDrawer);

    drawer.querySelectorAll('[data-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        closeMoreDrawer();
        if (tab && typeof window.switchTab === 'function') window.switchTab(tab);
      });
    });

    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape' && !drawer.hidden) closeMoreDrawer();
    });
  }

  function bindBottomNav() {
    const moreBtn = document.getElementById('bnav-more-btn');
    if (moreBtn && moreBtn.dataset.bound !== '1') {
      moreBtn.dataset.bound = '1';
      moreBtn.addEventListener('click', ev => {
        ev.preventDefault();
        ev.stopPropagation();
        const drawer = document.getElementById('more-drawer');
        if (drawer && drawer.hidden) openMoreDrawer();
        else closeMoreDrawer();
      });
    }
  }

  /** Sticky bar above bottom nav for the most urgent open trade. */
  function updateTradeMobileBar() {
    const bar = document.getElementById('trade-mobile-bar');
    if (!bar) return;
    if (!isMobile()) {
      bar.hidden = true;
      document.body.classList.remove('trade-mobile-bar-visible');
      return;
    }

    const panel = document.getElementById('panel-trades');
    if (!panel || !panel.classList.contains('active')) {
      bar.hidden = true;
      document.body.classList.remove('trade-mobile-bar-visible');
      return;
    }

    const cards = Array.from(document.querySelectorAll('#trades-container .collapsible-card[data-trade-id]'));
    if (!cards.length) {
      bar.hidden = true;
      document.body.classList.remove('trade-mobile-bar-visible');
      return;
    }

    const priority = card => {
      const notif = (card.dataset.riskNotif || '').toUpperCase();
      if (['LOSS_LIMIT_HIT', 'THESIS_FAIL', 'SL_TRIGGER'].includes(notif)) return 0;
      if (['PROFIT_FLOOR_HIT', 'SHORT_LEG_STRESS', 'PRE_BREACH_WARNING'].includes(notif)) return 1;
      if ((card.dataset.dailyStatus || '').toUpperCase().includes('EXIT')) return 2;
      return 3;
    };
    cards.sort((a, b) => priority(a) - priority(b));
    const pick = cards[0];
    const tid = pick.dataset.tradeId;
    const nameEl = pick.querySelector('h3');
    const name = nameEl ? nameEl.textContent.trim() : tid;
    const pnlEl = pick.querySelector(`.tag-current-pnl[data-trade-id="${CSS.escape(tid)}"] .cpnl-val`);
    const pnlText = pnlEl ? pnlEl.textContent.trim() : '—';

    bar.hidden = false;
    document.body.classList.add('trade-mobile-bar-visible');
    bar.dataset.tradeId = tid;
    const nameSpan = bar.querySelector('.trade-mobile-name');
    const pnlSpan = bar.querySelector('.trade-mobile-pnl');
    if (nameSpan) nameSpan.textContent = name;
    if (pnlSpan) pnlSpan.textContent = pnlText;

    const closeBtn = bar.querySelector('.btn-trade-mobile-close');
    if (closeBtn && closeBtn.dataset.tradeId !== tid) {
      closeBtn.dataset.tradeId = tid;
    }
  }

  function scrollToTradeClose(tradeId) {
    const card = document.getElementById(`trade-card-${tradeId}`);
    if (card && !card.open) card.open = true;
    const section = document.getElementById(`close-${tradeId}`);
    if (section) {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
      section.classList.add('sheet-open');
    }
  }

  function bindTradeMobileBar() {
    const bar = document.getElementById('trade-mobile-bar');
    if (!bar || bar.dataset.bound === '1') return;
    bar.dataset.bound = '1';
    bar.querySelector('.btn-trade-mobile-close')?.addEventListener('click', () => {
      const tid = bar.dataset.tradeId;
      if (tid) scrollToTradeClose(tid);
    });
  }

  function patchSwitchTab() {
    if (typeof window.switchTab !== 'function' || window.switchTab.__mobilePatched) return;
    const orig = window.switchTab;
    window.switchTab = function (name) {
      closeMoreDrawer();
      const out = orig(name);
      setTimeout(updateTradeMobileBar, 80);
      return out;
    };
    window.switchTab.__mobilePatched = true;
  }

  function init() {
    bindMoreDrawer();
    bindBottomNav();
    bindTradeMobileBar();
    patchSwitchTab();
    MOBILE_MQ.addEventListener('change', updateTradeMobileBar);

    document.addEventListener('trade-mtm-updated', () => updateTradeMobileBar());

    setTimeout(updateTradeMobileBar, 500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.closeMoreDrawer = closeMoreDrawer;
  window.updateTradeMobileBar = updateTradeMobileBar;
})();
