"""
database/basis_schema.py
========================

DDL for the Cash-Futures Basis Monitor module. Tables use the ``basis_`` prefix.
"""

from __future__ import annotations

import logging
from typing import List

from database.connection import SQLServerConnection
from database.schema import _normalize_ddl

logger = logging.getLogger(__name__)

BASIS_TABLE_DDL: List[str] = [
    """
    IF OBJECT_ID('basis_pairs', 'U') IS NULL
    CREATE TABLE basis_pairs (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        symbol        NVARCHAR(50)  NOT NULL,
        spot_symbol   NVARCHAR(50)  NOT NULL,
        fut_symbol    NVARCHAR(50)  NOT NULL,
        spot_token    BIGINT        NOT NULL,
        fut_token     BIGINT        NOT NULL,
        fut_expiry    DATE          NOT NULL,
        active        BIT           NOT NULL DEFAULT 1,
        updated_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_basis_pairs_symbol UNIQUE (symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_basis_pairs_active ON basis_pairs (active, symbol)",
    """
    IF OBJECT_ID('basis_episodes', 'U') IS NULL
    CREATE TABLE basis_episodes (
        id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        symbol            NVARCHAR(50)  NOT NULL,
        fut_expiry        DATE          NULL,
        started_at        DATETIME2(0)  NOT NULL,
        ended_at          DATETIME2(0)  NULL,
        duration_sec      INT           NULL,
        spot_ltp          DECIMAL(18,4) NULL,
        fut_ltp           DECIMAL(18,4) NULL,
        basis_abs         DECIMAL(18,4) NULL,
        basis_pct         DECIMAL(10,4) NULL,
        annualized_pct    DECIMAL(10,4) NULL,
        direction         NVARCHAR(16)  NULL,
        spot_bid          DECIMAL(18,4) NULL,
        spot_ask          DECIMAL(18,4) NULL,
        spot_bid_qty      INT           NULL,
        spot_ask_qty      INT           NULL,
        fut_bid           DECIMAL(18,4) NULL,
        fut_ask           DECIMAL(18,4) NULL,
        fut_bid_qty       INT           NULL,
        fut_ask_qty       INT           NULL,
        max_basis_pct     DECIMAL(10,4) NULL,
        sample_count      INT           NOT NULL DEFAULT 0,
        created_at        DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        updated_at        DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_basis_episodes_started ON basis_episodes (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS IX_basis_episodes_symbol ON basis_episodes (symbol, started_at DESC)",
    """
    IF OBJECT_ID('basis_config', 'U') IS NULL
    CREATE TABLE basis_config (
        config_key      NVARCHAR(64)   NOT NULL PRIMARY KEY,
        config_value    NVARCHAR(MAX)  NOT NULL,
        updated_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_by      NVARCHAR(64)   NULL
    )
    """,
]


def create_basis_tables(db: SQLServerConnection) -> None:
    """Ensure basis tables exist. Caller commits."""
    for raw in BASIS_TABLE_DDL:
        cur = db.execute(_normalize_ddl(raw))
        cur.close()
    logger.info("Basis tables ensured (%d DDL statements).", len(BASIS_TABLE_DDL))


def basis_table_names() -> List[str]:
    return ["basis_pairs", "basis_episodes", "basis_config"]
