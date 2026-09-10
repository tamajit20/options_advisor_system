"""
lifecycle/archive_orchestrator.py
=================================

Weekly copy of aged hot rows into *_Archive tables. Hot rows stay until ACK.
"""

from __future__ import annotations

import logging

from database.archive_repo import run_weekly_archive
from database.connection import SQLServerConnection
from utils import today_ist

logger = logging.getLogger(__name__)


def run_archive(db: SQLServerConnection) -> int:
    today = today_ist()
    n = run_weekly_archive(db, today)
    db.commit()
    from lifecycle.sql_backup import shrink_transaction_log_quietly

    shrink_transaction_log_quietly(db)
    logger.info("weekly archive complete: ~%d rows copied", n)
    return n
