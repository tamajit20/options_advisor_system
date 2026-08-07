"""
database/scout_schema.py
========================

DDL for the Intraday Scout module. Tables use the ``scout_`` prefix — not
``options_`` — to keep the module isolated from the options advisor schema.
"""

from __future__ import annotations

import logging
from typing import List

from database.connection import SQLServerConnection
from database.schema import _normalize_ddl

logger = logging.getLogger(__name__)

SCOUT_TABLE_DDL: List[str] = [
    """
    IF OBJECT_ID('scout_signals', 'U') IS NULL
    CREATE TABLE scout_signals (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        scan_id         NVARCHAR(64)   NOT NULL,
        symbol          NVARCHAR(32)   NOT NULL,
        exchange        NVARCHAR(16)   NOT NULL DEFAULT 'NSE',
        action          NVARCHAR(8)    NOT NULL,
        signal_type     NVARCHAR(64)   NOT NULL,
        reason          NVARCHAR(512)  NOT NULL,
        ltp             DECIMAL(18,4)  NOT NULL,
        invalidation    DECIMAL(18,4)  NULL,
        strength        NVARCHAR(16)   NOT NULL DEFAULT 'WEAK',
        triggered_at    DATETIME2      NOT NULL,
        created_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        meta_json       NVARCHAR(MAX)  NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_scout_signals_triggered ON scout_signals (triggered_at DESC)",
    "CREATE INDEX IF NOT EXISTS IX_scout_signals_symbol ON scout_signals (symbol, triggered_at DESC)",
    """
    IF OBJECT_ID('scout_scan_log', 'U') IS NULL
    CREATE TABLE scout_scan_log (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        scan_id         NVARCHAR(64)   NOT NULL,
        started_at      DATETIME2      NOT NULL,
        finished_at     DATETIME2      NULL,
        status          NVARCHAR(16)   NOT NULL,
        symbols_scanned INT            NOT NULL DEFAULT 0,
        signals_found   INT            NOT NULL DEFAULT 0,
        error_message   NVARCHAR(500)  NULL
    )
    """,
]


def create_scout_tables(db: SQLServerConnection) -> None:
    """Ensure scout tables exist. Caller commits."""
    for raw in SCOUT_TABLE_DDL:
        cur = db.execute(_normalize_ddl(raw))
        cur.close()
    logger.info("Scout tables ensured (%d DDL statements).", len(SCOUT_TABLE_DDL))


def scout_table_names() -> List[str]:
    return ["scout_signals", "scout_scan_log"]
