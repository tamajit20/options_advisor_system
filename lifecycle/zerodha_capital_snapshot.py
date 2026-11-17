"""
lifecycle/zerodha_capital_snapshot.py
=====================================

Scheduler job: refresh Zerodha account margins and persist a capital
snapshot when net / idle / utilised figures change.

Runs during the NSE session so History → Capital can chart seating idle
cash going forward (no backfill of past Zerodha balances).
"""

from __future__ import annotations

import logging

from database.connection import SQLServerConnection
from providers.ws_health import in_nse_session
from utils import now_ist

logger = logging.getLogger(__name__)


def run_zerodha_capital_snapshot(db: SQLServerConnection) -> int:
    """Force-refresh Kite margins; insert a row only on capital change.

    Returns 1 if a new snapshot row was written, else 0.
    """
    del db  # fetch opens its own connection for persist
    now = now_ist()
    if not in_nse_session(now):
        return 0

    from providers.zerodha.account_snapshot import fetch_account_snapshot

    out = fetch_account_snapshot(
        force_refresh=True,
        persist_capital=True,
        trigger_reason="scheduler",
    )
    if not out.get("available"):
        logger.debug(
            "zerodha_capital_snapshot skipped: %s",
            out.get("reason") or "unavailable",
        )
        return 0
    return 1 if out.get("capital_persisted") else 0
