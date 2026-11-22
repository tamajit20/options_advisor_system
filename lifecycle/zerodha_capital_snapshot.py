"""
lifecycle/zerodha_capital_snapshot.py
=====================================

Scheduler job: refresh Zerodha account margins and persist a capital
snapshot when net / idle / utilised figures change.

Runs during the NSE session so History → Capital can chart seating idle
cash going forward (no backfill of past Zerodha balances).

Each dashboard user with a live Zerodha token is snapshotted with their
own Kite session — never the admin feed token for other traders.
"""

from __future__ import annotations

import logging

from database.connection import SQLServerConnection
from providers.ws_health import in_nse_session
from utils import now_ist

logger = logging.getLogger(__name__)


def run_zerodha_capital_snapshot(db: SQLServerConnection) -> int:
    """Force-refresh Kite margins for every logged-in trader; persist on change.

    Returns the number of new snapshot rows written.
    """
    now = now_ist()
    if not in_nse_session(now):
        return 0

    from database.zerodha_session_repo import ZerodhaSessionRepo
    from providers.zerodha.account_snapshot import fetch_account_snapshot

    try:
        user_ids = ZerodhaSessionRepo(db).list_user_ids_with_token()
    except Exception:
        logger.debug("zerodha_capital_snapshot: list sessions failed", exc_info=True)
        user_ids = []

    if not user_ids:
        # Fallback: admin/feed session only (legacy single-user / no SQL tokens).
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

    written = 0
    for uid in user_ids:
        try:
            out = fetch_account_snapshot(
                force_refresh=True,
                persist_capital=True,
                trigger_reason="scheduler",
                app_user_id=int(uid),
            )
        except Exception:
            logger.debug(
                "zerodha_capital_snapshot user=%s failed", uid, exc_info=True,
            )
            continue
        if not out.get("available"):
            logger.debug(
                "zerodha_capital_snapshot user=%s skipped: %s",
                uid, out.get("reason") or "unavailable",
            )
            continue
        if out.get("capital_persisted"):
            written += 1
    return written
