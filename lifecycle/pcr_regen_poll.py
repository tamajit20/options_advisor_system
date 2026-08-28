"""
lifecycle/pcr_regen_poll.py
===========================

Scheduler job: snapshot chain OI and detect PCR band crossings for
``OPPORTUNITY_REGEN_HINT`` notifications.

Runs during NSE session only. Uses REST/NSE-live chain fetch (same path as
intraday close snapshot) — does not require WebSocket ticks.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Optional

from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from engine.indicators import pcr
from lifecycle.opportunity_regen_watcher import OpportunityRegenWatcher
from notifications import build_notifier
from providers.registry import get_market_data
from providers.tick_routing import options_underlyings
from providers.ws_health import in_nse_session
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)

_watcher: Optional[OpportunityRegenWatcher] = None


def _get_watcher(db: SQLServerConnection) -> OpportunityRegenWatcher:
    global _watcher
    if _watcher is None:
        _watcher = OpportunityRegenWatcher(build_notifier(db))
    return _watcher


def run_pcr_regen_poll(
    db: SQLServerConnection,
    trade_date: Optional[date] = None,
    *,
    provider=None,
) -> int:
    """Fetch chains and feed PCR observations. Returns symbols checked."""
    cfg = STRATEGY_CONFIG.get("pcr_regen_poll") or {}
    if not cfg.get("enabled", True):
        return 0

    now = now_ist()
    if not in_nse_session(now):
        return 0

    trade_date = trade_date or today_ist()
    p = provider if provider is not None else get_market_data()
    watcher = _get_watcher(db)

    expiries_by_symbol: Dict[str, date] = {}
    for und in options_underlyings():
        row = db.fetch_one(
            "SELECT TOP 1 expiry_date FROM options_suggestions "
            "WHERE underlying = ? AND status = 'PENDING' "
            "ORDER BY generated_on DESC",
            [und],
        )
        if row and row.get("expiry_date"):
            expiries_by_symbol[und] = row["expiry_date"]

    if not expiries_by_symbol:
        for und in options_underlyings():
            expiries_by_symbol[und] = trade_date

    checked = 0
    for symbol, expiry in expiries_by_symbol.items():
        try:
            chain_rows = p.get_chain(symbol, trade_date, expiry)
        except Exception as exc:
            logger.warning("pcr_regen_poll: get_chain(%s) failed: %s", symbol, exc)
            continue
        if not chain_rows:
            continue
        pcr_val = pcr(list(chain_rows))
        if pcr_val is None:
            continue
        watcher.on_pcr_observation(symbol, pcr_val)
        checked += 1

    if checked:
        db.commit()
    return checked
