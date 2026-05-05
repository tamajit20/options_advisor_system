"""
database/schema.py
==================

DDL for all 18 tables of `OptionsAdvisorDB`.

Design rules:
    * Every table prefixed `options_`.
    * Idempotent: each `CREATE TABLE` is wrapped in an `IF OBJECT_ID(...) IS NULL`
      guard so `--init-db` is safe to re-run.
    * No foreign key cascades — deletions handled by the retention/cleanup job.
    * All timestamps stored as `DATETIME2(0)` in IST (we store naive datetimes
      consistently — see `utils.now_ist`).
    * String identifiers (suggestion_id, trade_id, trade_name) are NVARCHAR.
"""

from __future__ import annotations

import logging
from typing import List

import pyodbc

from config import DATABASE_CONFIG
from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CREATE TABLE statements (each guarded by OBJECT_ID check)
# ---------------------------------------------------------------------------

_TABLE_DDL: List[str] = [
    # ---------------- Raw data ----------------
    """
    IF OBJECT_ID('options_fo_eod', 'U') IS NULL
    CREATE TABLE options_fo_eod (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        trade_date    DATE          NOT NULL,
        symbol        NVARCHAR(50)  NOT NULL,
        instrument    NVARCHAR(10)  NOT NULL,
        expiry_date   DATE          NOT NULL,
        strike        DECIMAL(18,4) NOT NULL,
        option_type   NVARCHAR(2)   NOT NULL,
        open_price    DECIMAL(18,4) NULL,
        high_price    DECIMAL(18,4) NULL,
        low_price     DECIMAL(18,4) NULL,
        close_price   DECIMAL(18,4) NULL,
        settle_price  DECIMAL(18,4) NULL,
        contracts     BIGINT        NULL,
        open_interest BIGINT        NULL,
        change_in_oi  BIGINT        NULL,
        created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_options_fo_eod UNIQUE
            (trade_date, symbol, expiry_date, strike, option_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_fo_eod_sym_dt ON options_fo_eod (symbol, trade_date)",
    "CREATE INDEX IF NOT EXISTS IX_options_fo_eod_expiry ON options_fo_eod (symbol, expiry_date, trade_date)",

    """
    IF OBJECT_ID('options_spot_eod', 'U') IS NULL
    CREATE TABLE options_spot_eod (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        trade_date    DATE          NOT NULL,
        symbol        NVARCHAR(50)  NOT NULL,
        open_price    DECIMAL(18,4) NULL,
        high_price    DECIMAL(18,4) NULL,
        low_price     DECIMAL(18,4) NULL,
        close_price   DECIMAL(18,4) NULL,
        volume        BIGINT        NULL,
        created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_options_spot_eod UNIQUE (trade_date, symbol)
    )
    """,

    """
    IF OBJECT_ID('options_vix_history', 'U') IS NULL
    CREATE TABLE options_vix_history (
        trade_date    DATE          NOT NULL PRIMARY KEY,
        open_price    DECIMAL(10,4) NULL,
        high_price    DECIMAL(10,4) NULL,
        low_price     DECIMAL(10,4) NULL,
        close_price   DECIMAL(10,4) NOT NULL,
        percentile_1y DECIMAL(6,2)  NULL,
        created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME()
    )
    """,

    """
    IF OBJECT_ID('options_fii_data', 'U') IS NULL
    CREATE TABLE options_fii_data (
        id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        trade_date        DATE         NOT NULL,
        client_type       NVARCHAR(20) NOT NULL,
        future_long       BIGINT       NULL,
        future_short      BIGINT       NULL,
        option_call_long  BIGINT       NULL,
        option_call_short BIGINT       NULL,
        option_put_long   BIGINT       NULL,
        option_put_short  BIGINT       NULL,
        created_at        DATETIME2(0) NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_options_fii_data UNIQUE (trade_date, client_type)
    )
    """,

    """
    IF OBJECT_ID('options_iv_history', 'U') IS NULL
    CREATE TABLE options_iv_history (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        trade_date    DATE          NOT NULL,
        symbol        NVARCHAR(50)  NOT NULL,
        expiry_date   DATE          NOT NULL,
        strike        DECIMAL(18,4) NOT NULL,
        option_type   NVARCHAR(2)   NOT NULL,
        spot          DECIMAL(18,4) NULL,
        market_price  DECIMAL(18,4) NULL,
        iv            DECIMAL(10,6) NULL,
        converged     BIT           NOT NULL DEFAULT 0,
        atm_iv        DECIMAL(10,6) NULL,
        iv_rank       DECIMAL(6,2)  NULL,
        iv_percentile DECIMAL(6,2)  NULL,
        created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_options_iv_history UNIQUE
            (trade_date, symbol, expiry_date, strike, option_type)
    )
    """,

    # ---------------- Reference data ----------------
    """
    IF OBJECT_ID('options_lot_sizes', 'U') IS NULL
    CREATE TABLE options_lot_sizes (
        symbol       NVARCHAR(50) NOT NULL,
        effective_from DATE       NOT NULL,
        lot_size     INT          NOT NULL,
        CONSTRAINT PK_options_lot_sizes PRIMARY KEY (symbol, effective_from)
    )
    """,

    """
    IF OBJECT_ID('options_expiry_calendar', 'U') IS NULL
    CREATE TABLE options_expiry_calendar (
        symbol       NVARCHAR(50) NOT NULL,
        expiry_date  DATE         NOT NULL,
        is_monthly   BIT          NOT NULL DEFAULT 0,
        CONSTRAINT PK_options_expiry_calendar PRIMARY KEY (symbol, expiry_date)
    )
    """,

    """
    IF OBJECT_ID('options_events_calendar', 'U') IS NULL
    CREATE TABLE options_events_calendar (
        id           BIGINT IDENTITY(1,1) PRIMARY KEY,
        event_date   DATE         NOT NULL,
        event_type   NVARCHAR(50) NOT NULL,
        description  NVARCHAR(500) NULL,
        impact       NVARCHAR(20) NOT NULL DEFAULT 'MEDIUM'
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_events_date ON options_events_calendar (event_date)",

    # ---------------- Suggestions ----------------
    """
    IF OBJECT_ID('options_suggestions', 'U') IS NULL
    CREATE TABLE options_suggestions (
        suggestion_id          NVARCHAR(40) NOT NULL PRIMARY KEY,
        trade_name             NVARCHAR(80) NULL,
        generated_on           DATETIME2(0) NOT NULL,
        strategy               NVARCHAR(40) NOT NULL,
        strategy_type          NVARCHAR(20) NOT NULL,  -- WRITING / BUYING / NONE
        underlying             NVARCHAR(50) NOT NULL,
        expiry_date            DATE         NULL,
        dte                    INT          NULL,
        spot_at_generation     DECIMAL(18,4) NULL,
        confidence_score       INT          NULL,
        conditions_json        NVARCHAR(MAX) NULL,
        status                 NVARCHAR(20) NOT NULL DEFAULT 'PENDING',
        net_credit_suggested   DECIMAL(18,4) NULL,
        max_profit             DECIMAL(18,4) NULL,
        max_loss               DECIMAL(18,4) NULL,
        upper_breakeven        DECIMAL(18,4) NULL,
        lower_breakeven        DECIMAL(18,4) NULL,
        stop_loss_level        DECIMAL(18,4) NULL,
        probability_of_profit  DECIMAL(6,2)  NULL,
        estimated_charges_total DECIMAL(18,4) NULL,
        estimated_net_pnl      DECIMAL(18,4) NULL,
        execution_window       NVARCHAR(80) NULL,
        plain_english          NVARCHAR(MAX) NULL,
        no_suggestion_reason   NVARCHAR(MAX) NULL
    )
    """,
    # Migration: add expiry_type column if not present (safe on existing DBs)
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'expiry_type'
    )
    ALTER TABLE options_suggestions ADD expiry_type NVARCHAR(10) NULL
    """,
    # Migration: add data_date (NSE bhav date used for analysis)
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'data_date'
    )
    ALTER TABLE options_suggestions ADD data_date DATE NULL
    """,
    # Migration: add entry_date (intended execution date = next trading day after data_date)
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'entry_date'
    )
    ALTER TABLE options_suggestions ADD entry_date DATE NULL
    """,
    # Migration: actual trade_date of the spot EOD row used (may lag data_date by a day)
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'spot_data_date'
    )
    ALTER TABLE options_suggestions ADD spot_data_date DATE NULL
    """,
    # Migration: actual trade_date of the FII row used
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'fii_data_date'
    )
    ALTER TABLE options_suggestions ADD fii_data_date DATE NULL
    """,
    # Migration: trade_date of the most recent VIX row used
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'vix_data_date'
    )
    ALTER TABLE options_suggestions ADD vix_data_date DATE NULL
    """,
    # Migration: edge_score (0–100, display + ranking only — issue #10)
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'edge_score'
    )
    ALTER TABLE options_suggestions ADD edge_score DECIMAL(6,2) NULL
    """,
    # Migration: credit_grade tag for credit strategies — "weak"/"good"/"strong"/NULL
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'credit_grade'
    )
    ALTER TABLE options_suggestions ADD credit_grade NVARCHAR(10) NULL
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_suggestions_date ON options_suggestions (generated_on DESC)",
    "CREATE INDEX IF NOT EXISTS IX_options_suggestions_status ON options_suggestions (status, generated_on DESC)",

    """
    IF OBJECT_ID('options_suggestion_legs', 'U') IS NULL
    CREATE TABLE options_suggestion_legs (
        id                   BIGINT IDENTITY(1,1) PRIMARY KEY,
        suggestion_id        NVARCHAR(40) NOT NULL,
        leg_order            INT          NOT NULL,
        hedge_pair_leg       INT          NULL,
        symbol               NVARCHAR(50) NOT NULL,
        expiry_date          DATE         NOT NULL,
        strike               DECIMAL(18,4) NOT NULL,
        option_type          NVARCHAR(2)  NOT NULL,
        action               NVARCHAR(4)  NOT NULL,  -- BUY / SELL
        lots                 INT          NOT NULL,
        lot_size             INT          NOT NULL,
        suggested_price      DECIMAL(18,4) NOT NULL,
        suggested_price_low  DECIMAL(18,4) NULL,
        suggested_price_high DECIMAL(18,4) NULL,
        leg_purpose_note     NVARCHAR(500) NULL,
        CONSTRAINT UX_options_suggestion_legs UNIQUE (suggestion_id, leg_order)
    )
    """,

    # ---------------- Trades ----------------
    """
    IF OBJECT_ID('options_trades', 'U') IS NULL
    CREATE TABLE options_trades (
        trade_id               NVARCHAR(40) NOT NULL PRIMARY KEY,
        suggestion_id          NVARCHAR(40) NOT NULL,
        trade_name             NVARCHAR(80) NULL,
        executed_on            DATETIME2(0) NOT NULL,
        position_type          NVARCHAR(30) NOT NULL,  -- FULL_AS_SUGGESTED / PAIRED_PARTIAL / NAKED / MIXED
        net_credit_actual      DECIMAL(18,4) NULL,
        actual_max_profit      DECIMAL(18,4) NULL,
        actual_max_loss        DECIMAL(18,4) NULL,
        actual_upper_breakeven DECIMAL(18,4) NULL,
        actual_lower_breakeven DECIMAL(18,4) NULL,
        actual_stop_loss_level DECIMAL(18,4) NULL,
        spot_at_execution      DECIMAL(18,4) NULL,
        status                 NVARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
        daily_status           NVARCHAR(20) NULL,
        exit_instruction       NVARCHAR(500) NULL,
        broken_state_json      NVARCHAR(MAX) NULL,
        gross_pnl              DECIMAL(18,4) NULL,
        total_charges          DECIMAL(18,4) NULL,
        net_pnl                DECIMAL(18,4) NULL,
        closed_on              DATETIME2(0) NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_trades_status ON options_trades (status, executed_on DESC)",
    "CREATE INDEX IF NOT EXISTS IX_options_trades_sugg ON options_trades (suggestion_id)",

    """
    IF OBJECT_ID('options_trade_legs', 'U') IS NULL
    CREATE TABLE options_trade_legs (
        id                 BIGINT IDENTITY(1,1) PRIMARY KEY,
        trade_id           NVARCHAR(40) NOT NULL,
        suggestion_leg_id  BIGINT       NOT NULL,
        leg_order          INT          NOT NULL,
        executed           BIT          NOT NULL DEFAULT 0,
        fill_price         DECIMAL(18,4) NULL,
        fill_time          DATETIME2(0) NULL,
        not_filled_reason  NVARCHAR(200) NULL,
        exit_price         DECIMAL(18,4) NULL,
        exit_time          DATETIME2(0) NULL,
        leg_pnl            DECIMAL(18,4) NULL,
        leg_charges        DECIMAL(18,4) NULL,
        CONSTRAINT UX_options_trade_legs UNIQUE (trade_id, leg_order)
    )
    """,

    # Add lots_actual column if it doesn't exist yet (migration-safe)
    """
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='options_trade_legs' AND COLUMN_NAME='lots_actual'
    )
    ALTER TABLE options_trade_legs ADD lots_actual INT NULL
    """,

    # Add spot_at_execution column if it doesn't exist yet (migration-safe)
    """
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='options_trades' AND COLUMN_NAME='spot_at_execution'
    )
    ALTER TABLE options_trades ADD spot_at_execution DECIMAL(18,4) NULL
    """,

    """
    IF OBJECT_ID('options_resuggestions', 'U') IS NULL
    CREATE TABLE options_resuggestions (
        id                       BIGINT IDENTITY(1,1) PRIMARY KEY,
        original_suggestion_id   NVARCHAR(40) NOT NULL,
        generated_on             DATETIME2(0) NOT NULL,
        revised_legs_json        NVARCHAR(MAX) NOT NULL,
        status                   NVARCHAR(20) NOT NULL DEFAULT 'PENDING',
        combined_economics_json  NVARCHAR(MAX) NULL,
        CONSTRAINT UX_options_resuggestions UNIQUE (original_suggestion_id)  -- max 1 per orig
    )
    """,

    # ---------------- Simulation ----------------
    """
    IF OBJECT_ID('options_simulations', 'U') IS NULL
    CREATE TABLE options_simulations (
        id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        suggestion_id     NVARCHAR(40) NOT NULL,
        started_on        DATE         NOT NULL,
        completed_on      DATE         NULL,
        overall_quality   NVARCHAR(20) NOT NULL DEFAULT 'PENDING',  -- FULL_VALID / ADJUSTED / VOID / PENDING
        sim_net_credit    DECIMAL(18,4) NULL,
        sim_final_pnl     DECIMAL(18,4) NULL,
        sim_charges       DECIMAL(18,4) NULL,
        sim_net_pnl       DECIMAL(18,4) NULL,
        notes             NVARCHAR(MAX) NULL,
        CONSTRAINT UX_options_simulations UNIQUE (suggestion_id)
    )
    """,

    """
    IF OBJECT_ID('options_simulation_legs', 'U') IS NULL
    CREATE TABLE options_simulation_legs (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        suggestion_id   NVARCHAR(40) NOT NULL,
        leg_order       INT          NOT NULL,
        leg_symbol      NVARCHAR(50) NOT NULL,
        sim_date        DATE         NOT NULL,
        suggested_price DECIMAL(18,4) NULL,
        sim_entry_price DECIMAL(18,4) NULL,
        open_price      DECIMAL(18,4) NULL,
        high_price      DECIMAL(18,4) NULL,
        low_price       DECIMAL(18,4) NULL,
        settle_price    DECIMAL(18,4) NULL,
        quality         NVARCHAR(20) NOT NULL,
        adjustment_note NVARCHAR(500) NULL,
        day_pnl         DECIMAL(18,4) NULL,
        cumulative_pnl  DECIMAL(18,4) NULL,
        is_expiry_day   BIT          NOT NULL DEFAULT 0,
        final_settle    DECIMAL(18,4) NULL,
        CONSTRAINT UX_options_simulation_legs UNIQUE (suggestion_id, leg_order, sim_date)
    )
    """,

    # ---------------- Logging / jobs / config / notifications ----------------
    """
    IF OBJECT_ID('options_system_logs', 'U') IS NULL
    CREATE TABLE options_system_logs (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        logged_at     DATETIME2(0) NOT NULL,
        level         NVARCHAR(10) NOT NULL,
        module        NVARCHAR(80) NULL,
        job_id        NVARCHAR(80) NULL,
        message       NVARCHAR(MAX) NOT NULL,
        exception     NVARCHAR(MAX) NULL,
        context_json  NVARCHAR(MAX) NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_system_logs_lvl ON options_system_logs (level, logged_at DESC)",
    "CREATE INDEX IF NOT EXISTS IX_options_system_logs_job ON options_system_logs (job_id, logged_at DESC)",

    """
    IF OBJECT_ID('options_job_log', 'U') IS NULL
    CREATE TABLE options_job_log (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        job_id          NVARCHAR(80) NOT NULL,
        job_name        NVARCHAR(80) NOT NULL,
        started_at      DATETIME2(0) NOT NULL,
        finished_at     DATETIME2(0) NULL,
        status          NVARCHAR(20) NOT NULL,
        error_message   NVARCHAR(MAX) NULL,
        rows_processed  INT NULL,
        CONSTRAINT UX_options_job_log_jobid UNIQUE (job_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_job_log_started ON options_job_log (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS IX_options_job_log_name ON options_job_log (job_name, started_at DESC)",

    """
    IF OBJECT_ID('options_config', 'U') IS NULL
    CREATE TABLE options_config (
        config_key     NVARCHAR(100) NOT NULL PRIMARY KEY,
        config_value   NVARCHAR(MAX) NULL,
        default_value  NVARCHAR(MAX) NULL,
        category       NVARCHAR(50)  NULL,
        description    NVARCHAR(500) NULL,
        is_locked      BIT           NOT NULL DEFAULT 0,
        last_modified  DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        modified_by    NVARCHAR(50)  NULL
    )
    """,

    """
    IF OBJECT_ID('options_notifications', 'U') IS NULL
    CREATE TABLE options_notifications (
        id                     BIGINT IDENTITY(1,1) PRIMARY KEY,
        created_at             DATETIME2(0) NOT NULL,
        notif_type             NVARCHAR(40) NOT NULL,
        severity               NVARCHAR(20) NOT NULL,
        title                  NVARCHAR(200) NOT NULL,
        body                   NVARCHAR(MAX) NULL,
        related_suggestion_id  NVARCHAR(40) NULL,
        related_trade_id       NVARCHAR(40) NULL,
        read_at                DATETIME2(0) NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_notifications_unread ON options_notifications (read_at, created_at DESC)",

    # ---------------- Runtime kill switches (Phase 4) ----------------
    # Single-row key/value table that the live components poll. Designed so
    # the operator can disable WS streaming, alert categories, or trade
    # execution without restarting the container. Bool flags are stored as
    # BIT (0/1). last_modified + modified_by give us a basic audit trail.
    """
    IF OBJECT_ID('options_runtime_flags', 'U') IS NULL
    CREATE TABLE options_runtime_flags (
        flag_key       NVARCHAR(50)  NOT NULL PRIMARY KEY,
        flag_value     NVARCHAR(200) NOT NULL,  -- stringified; int/bool/text
        flag_type      NVARCHAR(10)  NOT NULL,  -- 'bool' | 'int' | 'text'
        description    NVARCHAR(500) NULL,
        last_modified  DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        modified_by    NVARCHAR(50)  NULL
    )
    """,

    # ---------------- Intraday close snapshot (Phase 2b.1) ----------------
    # Captured at 15:35 IST — 5 minutes after equity F&O market close — by
    # reading live LTP for every leg of every ACTIVE trade. The 19:35 IST
    # drift verifier compares each row to the corresponding settled close
    # in `options_fo_eod` (loaded by the 18:30 fo_bhav_download job) and
    # fires a WARNING notification if any leg has drifted by more than
    # `STRATEGY_CONFIG["intraday_close_drift_pct"]` (default 5%).
    #
    # `source` records which tier produced the LTP (LIVE/EOD/MIXED) so a
    # snapshot row taken when the provider had already fallen back to EOD
    # can be excluded from drift comparison (it would always match by
    # construction). `freshness_ms` is the WS/REST tick age at capture.
    """
    IF OBJECT_ID('options_intraday_close_snapshot', 'U') IS NULL
    CREATE TABLE options_intraday_close_snapshot (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        snapshot_date DATE          NOT NULL,
        captured_at   DATETIME2(0)  NOT NULL,
        trade_id      NVARCHAR(40)  NOT NULL,
        leg_order     INT           NOT NULL,
        symbol        NVARCHAR(50)  NOT NULL,
        expiry_date   DATE          NOT NULL,
        strike        DECIMAL(18,4) NOT NULL,
        option_type   NVARCHAR(2)   NOT NULL,
        ltp           DECIMAL(18,4) NULL,
        source        NVARCHAR(10)  NULL,      -- 'LIVE' | 'EOD' | 'MIXED'
        provider      NVARCHAR(40)  NULL,
        freshness_ms  INT           NULL,
        CONSTRAINT UX_options_intraday_close_snapshot UNIQUE
            (snapshot_date, trade_id, leg_order)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_intraday_close_snapshot_date "
    "ON options_intraday_close_snapshot (snapshot_date)",

    # ---------------- Provenance markers (Phase 2c) ----------------
    # Goal: every row that downstream code reasons about must declare WHICH
    # data tier produced it (EOD vs LIVE), WHICH adapter served the data
    # (nse_eod / zerodha), WHEN the underlying data is from, and WHY the
    # row was written (EOD batch / 09:35 validator / WS regen / manual).
    # All columns are nullable so legacy rows and unmigrated writers
    # continue to work — code reading these fields must treat NULL as
    # "unknown".
    #
    # Column dictionary
    # -----------------
    # data_source             enum text:  'EOD' | 'LIVE' | 'MIXED'
    # provider                str: e.g. 'nse_eod', 'zerodha'
    # data_as_of              ts of underlying data (tick time / EOD date)
    # trigger_type            enum text:  'EOD_RUN' | 'INTRADAY_VALIDATOR'
    #                                     | 'WS_REGEN' | 'MANUAL'
    # trigger_reason          free text, e.g. 'VIX 18.4->19.7'
    # market_state_at_gen     enum text:  'PRE_OPEN' | 'OPEN_VOLATILE'
    #                                     | 'OPEN_STABLE' | 'CLOSE_AUCTION'
    #                                     | 'POST_CLOSE'
    # live_data_freshness_ms  int — only set when data_source='LIVE'
    # engine_version          short git SHA / version tag
    # validator_status        enum text:  'NOT_VALIDATED' | 'STILL_GOOD_0935'
    #                                     | 'STALE_0935' | 'STALE_INTRADAY'
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'data_source')
    ALTER TABLE options_suggestions ADD data_source NVARCHAR(10) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'provider')
    ALTER TABLE options_suggestions ADD provider NVARCHAR(40) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'data_as_of')
    ALTER TABLE options_suggestions ADD data_as_of DATETIME2(0) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'trigger_type')
    ALTER TABLE options_suggestions ADD trigger_type NVARCHAR(30) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'trigger_reason')
    ALTER TABLE options_suggestions ADD trigger_reason NVARCHAR(500) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'market_state_at_gen')
    ALTER TABLE options_suggestions ADD market_state_at_gen NVARCHAR(20) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'live_data_freshness_ms')
    ALTER TABLE options_suggestions ADD live_data_freshness_ms INT NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'engine_version')
    ALTER TABLE options_suggestions ADD engine_version NVARCHAR(20) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'validator_status')
    ALTER TABLE options_suggestions ADD validator_status NVARCHAR(30) NULL
    """,

    # leg_price_basis: which price tier was used for THIS leg's credit calc.
    # Values: 'SETTLED_CLOSE' | 'LIVE_BID_ASK_MID' | 'LIVE_LTP' | 'LIVE_SYNTHETIC'
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestion_legs') AND name = 'leg_price_basis')
    ALTER TABLE options_suggestion_legs ADD leg_price_basis NVARCHAR(30) NULL
    """,

    # Trade-side execution provenance — what tier priced the leg at click time
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'execution_data_source')
    ALTER TABLE options_trades ADD execution_data_source NVARCHAR(10) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'execution_provider')
    ALTER TABLE options_trades ADD execution_provider NVARCHAR(40) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'execution_freshness_ms')
    ALTER TABLE options_trades ADD execution_freshness_ms INT NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'gate_passed')
    ALTER TABLE options_trades ADD gate_passed BIT NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'time_from_suggestion_sec')
    ALTER TABLE options_trades ADD time_from_suggestion_sec INT NULL
    """,

    # Live risk monitor — per-trade alert silencing. When set in the future,
    # the live SL/Target alerter suppresses notifications for this trade
    # until the timestamp passes (e.g. user wants to ride out an SL).
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'alerts_silenced_until')
    ALTER TABLE options_trades ADD alerts_silenced_until DATETIME2(0) NULL
    """,

    # Phase 3 — Trailing SL (#4). Persisted PnL floor that ratchets up as
    # the trade reaches profit milestones. NULL = no trailing floor active;
    # NOT NULL = if current_pnl drops below this rupee value, fire SL_TRIGGER.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'trailing_pnl_floor')
    ALTER TABLE options_trades ADD trailing_pnl_floor DECIMAL(18,4) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_trades') AND name = 'trailing_step_idx')
    ALTER TABLE options_trades ADD trailing_step_idx INT NOT NULL DEFAULT 0
    """,

    # Notification provenance — links each alert back to its tick / cycle
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_notifications') AND name = 'source_event_id')
    ALTER TABLE options_notifications ADD source_event_id NVARCHAR(80) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_notifications') AND name = 'provider')
    ALTER TABLE options_notifications ADD provider NVARCHAR(40) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_notifications') AND name = 'tick_age_ms')
    ALTER TABLE options_notifications ADD tick_age_ms INT NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_notifications') AND name = 'flag_state_at_dispatch')
    ALTER TABLE options_notifications ADD flag_state_at_dispatch NVARCHAR(MAX) NULL
    """,

    # OI change momentum signal — Σ(ΔPut OI) / Σ(ΔCall OI).
    # EOD mode: day-over-day from bhav change_in_oi.
    # Live mode: live_oi − eod_oi per strike computed at suggestion time.
    # NULL for legacy rows (pre-implementation) and when data is unavailable.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'oi_pcr_change')
    ALTER TABLE options_suggestions ADD oi_pcr_change FLOAT NULL
    """,

    # ---------------- Time-series chain aggregate (5-min) ----------------
    # Boundary-aligned 5-min snapshots produced by lifecycle.chain_aggregator
    # from the Zerodha WebSocket tick stream. Used at live-suggestion time to
    # compute trajectory metrics (slope/persistence/acceleration) on OI PCR,
    # ATM bid-ask spread, and volume.
    # Idempotent on (snapshot_at, symbol, expiry_date).
    """
    IF OBJECT_ID('options_chain_5min', 'U') IS NULL
    CREATE TABLE options_chain_5min (
        id                    BIGINT IDENTITY(1,1) PRIMARY KEY,
        snapshot_at           DATETIME2(0)  NOT NULL,
        symbol                NVARCHAR(50)  NOT NULL,
        expiry_date           DATE          NOT NULL,
        spot                  DECIMAL(18,4) NULL,
        atm_strike            DECIMAL(18,4) NULL,
        sum_call_oi           BIGINT        NULL,
        sum_put_oi            BIGINT        NULL,
        sum_call_oi_delta     BIGINT        NULL,
        sum_put_oi_delta      BIGINT        NULL,
        sum_call_volume       BIGINT        NULL,
        sum_put_volume        BIGINT        NULL,
        atm_call_mid          DECIMAL(18,4) NULL,
        atm_put_mid           DECIMAL(18,4) NULL,
        atm_call_spread_bps   DECIMAL(18,4) NULL,
        atm_put_spread_bps    DECIMAL(18,4) NULL,
        sample_count          INT           NULL,
        created_at            DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_options_chain_5min UNIQUE (snapshot_at, symbol, expiry_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_chain_5min_sym_exp ON options_chain_5min (symbol, expiry_date, snapshot_at)",

    # ---------------- Time-series ATM IV (5-min) ----------------
    # Per (symbol, expiry) ATM IV computed from the WS tick stream's ATM
    # call/put mids at each 5-min boundary. Source for atm_iv_slope_5min
    # and atm_iv_persistence trajectory fields.
    """
    IF OBJECT_ID('options_atm_iv_5min', 'U') IS NULL
    CREATE TABLE options_atm_iv_5min (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        snapshot_at   DATETIME2(0)  NOT NULL,
        symbol        NVARCHAR(50)  NOT NULL,
        expiry_date   DATE          NOT NULL,
        atm_strike    DECIMAL(18,4) NULL,
        spot          DECIMAL(18,4) NULL,
        dte           INT           NULL,
        atm_iv        DECIMAL(18,6) NULL,
        created_at    DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_options_atm_iv_5min UNIQUE (snapshot_at, symbol, expiry_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_atm_iv_5min_sym_exp ON options_atm_iv_5min (symbol, expiry_date, snapshot_at)",

    # ---------------- Expected-move calibration (review item #10) -----------
    # One row per settled (suggestion, expiry).  Populated by the
    # `lifecycle/em_calibration_recorder` hook the day fo_bhav lands the
    # expiry's settlement close.  Used by `engine/em_calibration` to compute
    # the realised/expected median per (underlying, dte_band) and surface a
    # warning chip on subsequent suggestions when calibration drifts.
    """
    IF OBJECT_ID('options_em_calibration', 'U') IS NULL
    CREATE TABLE options_em_calibration (
        id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        suggestion_id   NVARCHAR(40)  NOT NULL,
        underlying      NVARCHAR(50)  NOT NULL,
        generated_on    DATE          NOT NULL,
        expiry_date     DATE          NOT NULL,
        dte_at_entry    INT           NOT NULL,
        dte_band        NVARCHAR(10)  NOT NULL,  -- '0-7' | '8-21' | '22+'
        spot_at_entry   DECIMAL(18,4) NOT NULL,
        spot_at_expiry  DECIMAL(18,4) NOT NULL,
        atm_iv_at_entry DECIMAL(10,6) NOT NULL,
        expected_move   DECIMAL(18,4) NOT NULL,  -- spot×iv×√(dte/365), points
        realised_move   DECIMAL(18,4) NOT NULL,  -- |spot_at_expiry - spot_at_entry|
        realised_ratio  DECIMAL(10,4) NOT NULL,  -- realised / expected
        created_at      DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UX_options_em_calibration UNIQUE (suggestion_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_options_em_calibration_lookup "
    "ON options_em_calibration (underlying, dte_band, generated_on DESC)",

    # Migration: surface the calibration warning on the suggestion row so the
    # dashboard can render a chip without recomputing on every page load.
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.columns
        WHERE object_id = OBJECT_ID('options_suggestions') AND name = 'em_calibration_warning'
    )
    ALTER TABLE options_suggestions ADD em_calibration_warning NVARCHAR(500) NULL
    """,
]


# ---------------------------------------------------------------------------
# SQL Server doesn't support `CREATE INDEX IF NOT EXISTS`. We translate it
# at execution time into a guarded `CREATE INDEX` block.
# ---------------------------------------------------------------------------

def _normalize_ddl(stmt: str) -> str:
    s = stmt.strip()
    if s.upper().startswith("CREATE INDEX IF NOT EXISTS"):
        # CREATE INDEX IF NOT EXISTS <name> ON <table> (<cols>)
        rest = s[len("CREATE INDEX IF NOT EXISTS"):].strip()
        # rest = "<name> ON <table> (<cols>)"
        idx_name, _, after = rest.partition(" ")
        return (
            f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = '{idx_name}') "
            f"CREATE INDEX {idx_name} {after}"
        )
    return s


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def create_database_if_missing() -> None:
    """Connect to `master` and create the target DB if it doesn't exist."""
    if not DATABASE_CONFIG.get("create_if_missing", True):
        return
    db_name = DATABASE_CONFIG["database"]
    master = SQLServerConnection(database="master")
    try:
        # autocommit needed for CREATE DATABASE
        master.connect(override_database="master")
        assert master.connection is not None
        master.connection.autocommit = True
        cur = master.connection.cursor()
        try:
            cur.execute("SELECT database_id FROM sys.databases WHERE name = ?", db_name)
            row = cur.fetchone()
            if row is None:
                logger.info("Creating database [%s] ...", db_name)
                cur.execute(f"CREATE DATABASE [{db_name}]")
                logger.info("Database [%s] created.", db_name)
            else:
                logger.info("Database [%s] already exists.", db_name)
        finally:
            cur.close()
    except pyodbc.Error as exc:
        logger.error("Failed to ensure database exists: %s", exc)
        raise
    finally:
        master.close()


def create_all_tables(db: SQLServerConnection) -> None:
    """Run every DDL statement in order. Caller commits."""
    for raw in _TABLE_DDL:
        sql = _normalize_ddl(raw)
        cur = db.execute(sql)
        cur.close()
    logger.info("All tables ensured (%d DDL statements executed).", len(_TABLE_DDL))


def list_tables() -> List[str]:
    """Return the canonical list of options-prefixed tables (for tests)."""
    return [
        "options_fo_eod",
        "options_spot_eod",
        "options_vix_history",
        "options_fii_data",
        "options_iv_history",
        "options_lot_sizes",
        "options_expiry_calendar",
        "options_events_calendar",
        "options_suggestions",
        "options_suggestion_legs",
        "options_trades",
        "options_trade_legs",
        "options_resuggestions",
        "options_simulations",
        "options_simulation_legs",
        "options_system_logs",
        "options_job_log",
        "options_config",
        "options_notifications",
        "options_runtime_flags",
        "options_intraday_close_snapshot",
    ]
