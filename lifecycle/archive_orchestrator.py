"""
lifecycle/archive_orchestrator.py
=================================

Weekly move of aged hot rows into *_Archive tables (no deletes except logs).
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
    logger.info("weekly archive complete: ~%d rows moved", n)
    return n
