"""
database/arb_schema.py
======================

DDL for the Arb Monitor module. Tables use the ``arb_`` prefix — separate
from ``options_`` and ``scout_`` schemas.
"""

from __future__ import annotations

import logging
from typing import List

from database.connection import SQLServerConnection
from database.schema import _normalize_ddl

logger = logging.getLogger(__name__)

ARB_TABLE_DDL: List[str] = [
    """
    IF OBJECT_ID('arb_pairs', 'U') IS NULL
    CREATE TABLE arb_pairs (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        symbol        NVARCHAR(50)  NOT NULL,
        nse_symbol    NVARCHAR(50)  NOT NULL,
        bse_symbol    NVARCHAR(50)  NOT NULL,
        isin          NVARCHAR(20)  NULL,
        nse_token     BIGINT        NOT NULL,
        bse_token     BIGINT        NOT NULL,
        active        BIT           NOT NULL DEFAULT 1,
        updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_arb_pairs_symbol UNIQUE (symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_arb_pairs_active ON arb_pairs (active, symbol)",
    """
    IF OBJECT_ID('arb_gaps', 'U') IS NULL
    CREATE TABLE arb_gaps (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        symbol          NVARCHAR(50)  NOT NULL,
        isin            NVARCHAR(20)  NULL,
        started_at      DATETIME2(0)  NOT NULL,
        ended_at        DATETIME2(0)  NULL,
        duration_sec    INT           NULL,
        nse_ltp         DECIMAL(18,4) NULL,
        bse_ltp         DECIMAL(18,4) NULL,
        gap_abs         DECIMAL(18,4) NULL,
        gap_pct         DECIMAL(10,4) NULL,
        direction       NVARCHAR(16)  NULL,
        nse_bid         DECIMAL(18,4) NULL,
        nse_ask         DECIMAL(18,4) NULL,
        nse_bid_qty     INT           NULL,
        nse_ask_qty     INT           NULL,
        bse_bid         DECIMAL(18,4) NULL,
        bse_ask         DECIMAL(18,4) NULL,
        bse_bid_qty     INT           NULL,
        bse_ask_qty     INT           NULL,
        max_gap_pct     DECIMAL(10,4) NULL,
        sample_count    INT           NOT NULL DEFAULT 0,
        created_at      DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        updated_at      DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_arb_gaps_started ON arb_gaps (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS IX_arb_gaps_symbol ON arb_gaps (symbol, started_at DESC)",
    """
    IF OBJECT_ID('arb_config', 'U') IS NULL
    CREATE TABLE arb_config (
        config_key      NVARCHAR(64)   NOT NULL PRIMARY KEY,
        config_value    NVARCHAR(MAX)  NOT NULL,
        updated_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_by      NVARCHAR(64)   NULL
    )
    """,
]


def create_arb_tables(db: SQLServerConnection) -> None:
    """Ensure arb tables exist. Caller commits."""
    for raw in ARB_TABLE_DDL:
        cur = db.execute(_normalize_ddl(raw))
        cur.close()
    logger.info("Arb tables ensured (%d DDL statements).", len(ARB_TABLE_DDL))


def arb_table_names() -> List[str]:
    return ["arb_pairs", "arb_gaps", "arb_config"]
