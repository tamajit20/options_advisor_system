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
    IF OBJECT_ID('scout_config', 'U') IS NULL
    CREATE TABLE scout_config (
        config_key      NVARCHAR(64)   NOT NULL PRIMARY KEY,
        config_value    NVARCHAR(MAX)  NOT NULL,
        updated_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_by      NVARCHAR(64)   NULL
    )
    """,
    """
    IF OBJECT_ID('scout_trades', 'U') IS NULL
    CREATE TABLE scout_trades (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        signal_id       BIGINT         NULL,
        symbol          NVARCHAR(32)   NOT NULL,
        action          NVARCHAR(8)    NOT NULL,
        signal_type     NVARCHAR(64)   NULL,
        entry_price     DECIMAL(18,4)  NOT NULL,
        quantity        INT            NOT NULL DEFAULT 1,
        executed_at     DATETIME2      NOT NULL,
        exit_price      DECIMAL(18,4)  NULL,
        closed_at       DATETIME2      NULL,
        status          NVARCHAR(16)   NOT NULL DEFAULT 'OPEN',
        pnl             DECIMAL(18,4)  NULL,
        pnl_pct         DECIMAL(9,4)   NULL,
        exit_reason     NVARCHAR(256)  NULL,
        notes           NVARCHAR(512)  NULL,
        created_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_scout_trades_status ON scout_trades (status, executed_at DESC)",
    "CREATE INDEX IF NOT EXISTS IX_scout_trades_symbol ON scout_trades (symbol, executed_at DESC)",
    """
    IF OBJECT_ID('scout_trade_orders', 'U') IS NULL
    CREATE TABLE scout_trade_orders (
        id                  BIGINT IDENTITY(1,1) PRIMARY KEY,
        trade_id            BIGINT         NOT NULL,
        step_num            TINYINT        NOT NULL,
        leg                 NVARCHAR(16)   NOT NULL,
        kite_order_id       NVARCHAR(32)   NULL,
        exchange_order_id   NVARCHAR(64)   NULL,
        order_type          NVARCHAR(16)   NULL,
        transaction_type    NVARCHAR(8)    NULL,
        product             NVARCHAR(8)    NULL,
        quantity            INT            NOT NULL,
        price               DECIMAL(18,4)  NULL,
        trigger_price       DECIMAL(18,4)  NULL,
        status              NVARCHAR(24)   NOT NULL DEFAULT 'PENDING',
        status_message      NVARCHAR(512)  NULL,
        placed_at           DATETIME2      NULL,
        updated_at          DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        meta_json           NVARCHAR(MAX)  NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_scout_trade_orders_trade ON scout_trade_orders (trade_id, step_num, leg)",
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
    """
    IF OBJECT_ID('scout_zerodha_log', 'U') IS NULL
    CREATE TABLE scout_zerodha_log (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        logged_at       DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        run_id          NVARCHAR(36)   NOT NULL,
        trigger_source  NVARCHAR(32)   NOT NULL,
        severity        NVARCHAR(16)   NOT NULL,
        code            NVARCHAR(64)   NOT NULL,
        message         NVARCHAR(1024) NOT NULL,
        detail          NVARCHAR(MAX)  NULL,
        user_id         NVARCHAR(32)   NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_scout_zerodha_log_at ON scout_zerodha_log (logged_at DESC)",
    "CREATE INDEX IF NOT EXISTS IX_scout_zerodha_log_sev ON scout_zerodha_log (severity, logged_at DESC)",
]


def create_scout_tables(db: SQLServerConnection) -> None:
    """Ensure scout tables exist. Caller commits."""
    _migrate_paper_trades_to_trades(db)
    for raw in SCOUT_TABLE_DDL:
        cur = db.execute(_normalize_ddl(raw))
        cur.close()
    _migrate_scout_trades_net_pnl(db)
    _migrate_scout_trades_execution(db)
    _migrate_scout_trades_signal_unique(db)
    logger.info("Scout tables ensured (%d DDL statements).", len(SCOUT_TABLE_DDL))


def _migrate_scout_trades_net_pnl(db: SQLServerConnection) -> None:
    """Add net-P&L and trailing columns to scout_trades when missing."""
    exists = db.fetch_one(
        "SELECT 1 AS ok FROM sys.tables WHERE name = 'scout_trades'"
    )
    if not exists:
        return
    for col, ddl in (
        ("gross_pnl", "DECIMAL(18,4) NULL"),
        ("total_charges", "DECIMAL(18,4) NULL"),
        ("net_pnl", "DECIMAL(18,4) NULL"),
        ("peak_price", "DECIMAL(18,4) NULL"),
    ):
        row = db.fetch_one(
            "SELECT 1 AS ok FROM sys.columns "
            "WHERE object_id = OBJECT_ID('scout_trades') AND name = ?",
            [col],
        )
        if not row:
            cur = db.execute(f"ALTER TABLE scout_trades ADD {col} {ddl}")
            cur.close()
            logger.info("scout_trades: added column %s", col)


def _migrate_scout_trades_execution(db: SQLServerConnection) -> None:
    """Add execution-mode columns to scout_trades when missing."""
    exists = db.fetch_one(
        "SELECT 1 AS ok FROM sys.tables WHERE name = 'scout_trades'"
    )
    if not exists:
        return
    for col, ddl in (
        ("execution_mode", "NVARCHAR(16) NULL"),
        ("effective_stop_price", "DECIMAL(18,4) NULL"),
    ):
        row = db.fetch_one(
            "SELECT 1 AS ok FROM sys.columns "
            "WHERE object_id = OBJECT_ID('scout_trades') AND name = ?",
            [col],
        )
        if not row:
            cur = db.execute(f"ALTER TABLE scout_trades ADD {col} {ddl}")
            cur.close()
            logger.info("scout_trades: added column %s", col)


def _migrate_scout_trades_signal_unique(db: SQLServerConnection) -> None:
    """One active trade row per signal_id (prevents duplicate auto-enter)."""
    exists = db.fetch_one(
        "SELECT 1 AS ok FROM sys.tables WHERE name = 'scout_trades'"
    )
    if not exists:
        return
    idx = db.fetch_one(
        "SELECT 1 AS ok FROM sys.indexes WHERE name = 'UX_scout_trades_signal_active'"
    )
    if idx:
        return
    cur = db.execute(
        "CREATE UNIQUE INDEX UX_scout_trades_signal_active ON scout_trades (signal_id) "
        "WHERE signal_id IS NOT NULL AND status IN "
        "('OPEN', 'PENDING_ENTRY', 'UNPROTECTED', 'CLOSING')"
    )
    cur.close()
    logger.info("scout_trades: added filtered unique index on signal_id")


def _migrate_paper_trades_to_trades(db: SQLServerConnection) -> None:
    """One-time rename from early v1 scout_paper_trades → scout_trades."""
    row = db.fetch_one(
        "SELECT 1 AS ok FROM sys.tables WHERE name = 'scout_paper_trades'"
    )
    if not row:
        return
    exists = db.fetch_one(
        "SELECT 1 AS ok FROM sys.tables WHERE name = 'scout_trades'"
    )
    if exists:
        return
    cur = db.execute("EXEC sp_rename 'scout_paper_trades', 'scout_trades'")
    cur.close()
    # Rename legacy column names if present (entered_at → executed_at, etc.)
    for old, new in (("entered_at", "executed_at"), ("exited_at", "closed_at")):
        col = db.fetch_one(
            "SELECT 1 AS ok FROM sys.columns WHERE object_id = OBJECT_ID('scout_trades') AND name = ?",
            [old],
        )
        if col:
            c2 = db.execute(f"EXEC sp_rename 'scout_trades.{old}', '{new}', 'COLUMN'")
            c2.close()
    logger.info("Migrated scout_paper_trades → scout_trades")


def scout_table_names() -> List[str]:
    return [
        "scout_signals", "scout_scan_log", "scout_config",
        "scout_trades", "scout_trade_orders", "scout_zerodha_log",
    ]
