/**
 * Mobile UX — app switcher, options subtabs, menu drawer, touch popovers.
 */
(function () {
  'use strict';

  const MOBILE_MQ = window.matchMedia('(max-width: 1024px)');
  const OPTIONS_TABS = ['suggestion', 'trades', 'learn', 'history', 'logs', 'jobs', 'wsmon', 'config'];
  const SYSTEM_TABS = ['logs', 'jobs', 'wsmon'];

  function getActiveTab() {
    if (typeof window.TABS === 'object' && Array.isArray(window.TABS)) {
      const found = window.TABS.find(t => {
        const panel = document.getElementById(`panel-${t}`);
        return panel && panel.classList.contains('active');
      });
      if (found) return found;
    }
    const panel = document.querySelector('.tab-panels > .panel.active');
    if (panel && panel.id.startsWith('panel-')) return panel.id.slice('panel-'.length);
    return null;
  }

  function syncMenuButtonActive() {
    const moreBtn = document.getElementById('bnav-more-btn');
    const drawer = document.getElementById('more-drawer');
    if (!moreBtn) return;
    const drawerOpen = drawer && !drawer.hidden;
    const onSystem = SYSTEM_TABS.includes(getActiveTab());
    moreBtn.classList.toggle('active', drawerOpen || onSystem);
  }

  function openMoreDrawer() {
    const drawer = document.getElementById('more-drawer');
    if (!drawer) return;
    drawer.hidden = false;
    document.body.classList.add('more-drawer-open');
    syncMenuButtonActive();
  }

  function closeMoreDrawer() {
    const drawer = document.getElementById('more-drawer');
    if (!drawer) return;
    drawer.hidden = true;
    document.body.classList.remove('more-drawer-open');
    syncMenuButtonActive();
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

  function bindAppSwitcher() {
    document.querySelectorAll('[data-bnav-app]').forEach(btn => {
      if (btn.dataset.bound === '1') return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => {
        const app = btn.dataset.bnavApp;
        if (app === 'options') {
          let tab = 'suggestion';
          try {
            const saved = localStorage.getItem('activeTab');
            if (saved && OPTIONS_TABS.includes(saved)) tab = saved;
          } catch (_) {}
          if (typeof window.switchTab === 'function') window.switchTab(tab);
        }
      });
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

  function patchSwitchTab() {
    if (typeof window.switchTab !== 'function' || window.switchTab.__mobilePatched) return;
    const orig = window.switchTab;
    window.switchTab = function (name) {
      closeMoreDrawer();
      const out = orig(name);
      syncMenuButtonActive();
      return out;
    };
    window.switchTab.__mobilePatched = true;
  }

  const TOUCH_POPOVER_MQ = window.matchMedia('(hover: none), (max-width: 1024px)');

  function usesTouchPopovers() {
    return TOUCH_POPOVER_MQ.matches;
  }

  /** Hover-only tooltips → tap-to-open bottom sheets on touch devices. */
  function closeTouchPopovers() {
    document.querySelectorAll('.touch-popover-open').forEach(el => {
      el.classList.remove('touch-popover-open');
    });
    document.querySelectorAll('.touch-popover-backdrop').forEach(el => el.remove());
  }

  function openTouchPopover(host) {
    closeTouchPopovers();
    host.classList.add('touch-popover-open');
    const backdrop = document.createElement('div');
    backdrop.className = 'touch-popover-backdrop';
    backdrop.setAttribute('aria-hidden', 'true');
    backdrop.addEventListener('click', closeTouchPopovers);
    document.body.appendChild(backdrop);
  }

  function bindTouchPopovers() {
    if (document.body.dataset.touchPopoversBound === '1') return;
    document.body.dataset.touchPopoversBound = '1';

    document.addEventListener('click', ev => {
      if (!usesTouchPopovers()) return;

      const confHost = ev.target.closest('.conf-logic-info');
      if (confHost) {
        ev.preventDefault();
        ev.stopPropagation();
        if (confHost.classList.contains('touch-popover-open')) closeTouchPopovers();
        else openTouchPopover(confHost);
        return;
      }

      const legendBtn = ev.target.closest('.tap-legend-btn');
      const legendHost = ev.target.closest('.tap-legend-wrap');
      if (legendBtn && legendHost) {
        ev.preventDefault();
        ev.stopPropagation();
        if (legendHost.classList.contains('touch-popover-open')) closeTouchPopovers();
        else openTouchPopover(legendHost);
        return;
      }

      if (!ev.target.closest('.conf-logic-popup') && !ev.target.closest('.tap-legend-popover')) {
        closeTouchPopovers();
      }
    });

    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape') closeTouchPopovers();
    });
  }

  function init() {
    bindMoreDrawer();
    bindAppSwitcher();
    bindBottomNav();
    bindTouchPopovers();
    patchSwitchTab();
    TOUCH_POPOVER_MQ.addEventListener('change', () => closeTouchPopovers());
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.closeMoreDrawer = closeMoreDrawer;
})();
