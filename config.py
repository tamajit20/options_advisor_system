"""
options_advisor_system / config.py
==================================

SINGLE SOURCE OF TRUTH for all configurable values.

Rules (enforced by code review):
    1. NO hardcoded values, URLs, thresholds, time windows, or charge rates may
       appear anywhere else in the codebase. Every such value lives here.
    2. Runtime overrides come from the DB table `options_config` (UI-editable).
       Use `database.config_repo.get(key, default=...)` to read overrides.
    3. The config dictionaries below define DEFAULTS. Sensitive infrastructure
       values (DB credentials, SMTP password) are loaded from environment
       variables (typically populated by `.env.docker`).

Boundary: this module imports ONLY from the standard library.
"""

from __future__ import annotations

import os
from datetime import time as _time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_CONFIG = {
    "server":             _env("OPT_DB_SERVER", "TAMAJITLAPTOP\\SQLEXPRESS"),
    "database":           _env("OPT_DB_NAME",   "OptionsAdvisorDB"),
    "username":           _env("OPT_DB_USER",   ""),     # blank → Windows auth
    "password":           _env("OPT_DB_PASSWORD", ""),
    "connection_timeout": _env_int("OPT_DB_TIMEOUT", 30),
    # If True, `python main.py --init-db` issues `CREATE DATABASE` if missing.
    "create_if_missing":  _env_bool("OPT_DB_CREATE_IF_MISSING", True),
}


# ---------------------------------------------------------------------------
# Scheduler (all times IST)
# ---------------------------------------------------------------------------
SCHEDULER_CONFIG = {
    "timezone": "Asia/Kolkata",
    "jobs": {
        # Data downloads (post-market)
        "fo_bhav_download":   {"hour": 18, "minute": 30, "enabled": True},
        "spot_bhav_download": {"hour": 18, "minute": 35, "enabled": True},
        "vix_download":       {"hour": 18, "minute": 40, "enabled": True},
        "fii_download":       {"hour": 18, "minute": 45, "enabled": True},
        # Calculations
        "iv_calculation":     {"hour": 19, "minute":  0, "enabled": True},
        # Suggestions + lifecycle
        "suggestion_engine":  {"hour": 19, "minute": 30, "enabled": True},
        "simulation_update":  {"hour": 19, "minute": 45, "enabled": True},
        "exit_engine":        {"hour": 19, "minute": 50, "enabled": True},
        # Maintenance (Sunday 02:00)
        "weekly_cleanup":     {"day_of_week": "sun", "hour": 2,  "minute":  0, "enabled": True},
        # Events calendar sync — Monday 07:00 before market open
        "events_seed":        {"day_of_week": "mon", "hour": 7,  "minute":  0, "enabled": True},
    },
    # Each job also gets a max wallclock budget (seconds) — enforced by orchestrator
    "job_timeout_seconds": {
        "fo_bhav_download":   600,
        "spot_bhav_download": 300,
        "vix_download":       180,
        "fii_download":       300,
        "iv_calculation":     900,
        "suggestion_engine":  600,
        "simulation_update":  600,
        "exit_engine":        300,
        "weekly_cleanup":     1800,
    },
}


# ---------------------------------------------------------------------------
# NSE Data sources
# ---------------------------------------------------------------------------
NSE_CONFIG = {
    # F&O bhav copy (zip containing CSV) — yyyymmdd
    "fo_bhav_url":    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip",
    # Cash market bhav copy (zip containing CSV) — yyyymmdd
    "spot_bhav_url":  "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip",
    # VIX history (full archive CSV)
    "vix_archive_url": "https://www.niftyindices.com/IndexConstituent/IndiaVIX_Historical_Data.csv",
    # NSE participant-wise OI (yyyymmdd)
    "fii_oi_url":      "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv",
    # Warm-up endpoint to obtain cookies before downloading archives
    "warmup_url":      "https://www.nseindia.com",
    "request_timeout": 30,
    "max_retries":     3,
    "retry_backoff_seconds": 5,
    "headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com",
    },
}


# ---------------------------------------------------------------------------
# Strategy / Confidence engine
# ---------------------------------------------------------------------------
STRATEGY_CONFIG = {
    # Risk-free rate for Black-Scholes IV
    "risk_free_rate": 0.065,

    # IV Rank gates
    "iv_rank_writing_min":   50.0,   # >50% → consider writing path
    "iv_rank_buying_max":    30.0,   # <30% → consider buying path
    "iv_rank_butterfly_min": 70.0,   # very-high IV → prefer iron butterfly over condor
    "iv_rank_naked_long_max": 20.0,  # very-low IV → naked long preferred over debit spread
    # 30–50 mid-zone → debit spreads (BULL_CALL_SPREAD / BEAR_PUT_SPREAD), no sideways play

    # PCR conviction bands (for picking strong vs mild directional plays)
    "pcr_strong_bullish_below": 0.55,   # heavy call buying = strong bullish
    "pcr_strong_bearish_above": 1.55,   # heavy put buying = strong bearish

    # PCR neutral band
    "pcr_neutral_low":  0.7,
    "pcr_neutral_high": 1.3,

    # DTE band
    "dte_min": 7,
    "dte_max": 21,

    # Confidence — tiered gating
    # Hard gates (Event + DTE): any FAIL = no suggestion regardless of score
    # Soft gates (IV Rank, VIX, PCR, OI Walls, Trend): SOFT_FAIL if condition
    #   not met; trade proceeds if at least soft_gate_min_pass of 5 pass.
    "confidence_min_pass_count": 7,     # legacy — no longer used by engine
    "soft_gate_min_pass": 4,            # need ≥4 of 5 soft gates to pass

    # IV calc bisection params
    "iv_bisection_low":  0.001,
    "iv_bisection_high": 5.0,
    "iv_bisection_tol":  1e-4,
    "iv_bisection_max_iter": 100,

    # Underlyings to consider (in priority order)
    "underlyings": ["NIFTY", "BANKNIFTY", "FINNIFTY"],

    # Lot sizes — overridden by data/lot_sizes.csv if present
    "default_lot_sizes": {
        "NIFTY":      75,
        "BANKNIFTY":  35,
        "FINNIFTY":   65,
    },

    # Net credit must be at least this fraction of spread width to be viable
    "min_credit_to_width_ratio": 0.005,   # 0.5%

    # Take-profit threshold for exit engine (fraction of max profit)
    "take_profit_fraction": 0.80,

    # VIX regime thresholds (% change vs prior close)
    "vix_rising_threshold":  5.0,
    "vix_spiking_threshold": 10.0,
}


# ---------------------------------------------------------------------------
# Zerodha — charges calculator
# ---------------------------------------------------------------------------
ZERODHA_CONFIG = {
    "brokerage_per_order_inr": 20.0,            # flat ₹20 per leg per order
    "stt_sell_premium_pct":     0.0005,         # 0.05% of sell-side premium
    "stt_itm_expiry_intrinsic_pct": 0.00125,    # 0.125% on ITM expiry intrinsic value
    "exchange_txn_pct":         0.000530,       # 0.053% × turnover (both sides)
    "sebi_charges_pct":         0.000001,       # 0.0001% × turnover
    "stamp_duty_buy_pct":       0.00003,        # 0.003% × buy-side premium
    "gst_pct":                  0.18,           # 18% on (brokerage + exchange + SEBI)
}


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
SIMULATION_CONFIG = {
    # Day-1 entry classification
    "adjusted_max_gap_pct": 10.0,   # ≤10% gap from suggested → ADJUSTED; >10% → VOID
}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASHBOARD_CONFIG = {
    "host":         _env("OPT_DASHBOARD_HOST", "0.0.0.0"),
    "port":         _env_int("OPT_DASHBOARD_PORT", 5001),
    "debug":        _env_bool("OPT_DASHBOARD_DEBUG", False),
    "log_page_size": 200,
    "history_page_size": 50,
    # Note dismissal — a contextual note is hidden after this many displays
    "note_hide_after_views": 5,
    # Color scheme tokens (also exposed to CSS via /api/theme)
    "theme": {
        "primary":     "#0F766E",  # dark teal/emerald
        "primary_dim": "#0B5E58",
        "accent":      "#F59E0B",  # amber
        "surface":     "#1F2937",
        "surface_alt": "#111827",
        "text":        "#F3F4F6",
        "text_dim":    "#9CA3AF",
        "ok":          "#10B981",
        "warn":        "#F59E0B",
        "err":         "#EF4444",
        "info":        "#06B6D4",
    },
}


# ---------------------------------------------------------------------------
# Alerts / Notifications
# ---------------------------------------------------------------------------
ALERTS_CONFIG = {
    "email_enabled": _env_bool("OPT_EMAIL_ENABLED", False),
    "smtp_host":     _env("OPT_SMTP_HOST", ""),
    "smtp_port":     _env_int("OPT_SMTP_PORT", 587),
    "smtp_user":     _env("OPT_SMTP_USER", ""),
    "smtp_password": _env("OPT_SMTP_PASSWORD", ""),
    "smtp_from":     _env("OPT_SMTP_FROM", ""),
    "smtp_to":       [a.strip() for a in _env("OPT_SMTP_TO", "").split(",") if a.strip()],
    "smtp_use_tls":  _env_bool("OPT_SMTP_USE_TLS", True),

    # Severity levels that trigger email (lower severities only go to dashboard)
    "email_severities": ["CRITICAL", "ERROR"],
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING_CONFIG = {
    # Levels: DEBUG / INFO / WARNING / ERROR / CRITICAL
    "console_level": _env("OPT_LOG_CONSOLE_LEVEL", "INFO"),
    "file_level":    _env("OPT_LOG_FILE_LEVEL", "INFO"),
    "db_level":      _env("OPT_LOG_DB_LEVEL", "INFO"),
    "log_dir":       _env("OPT_LOG_DIR", "logs"),
    "log_file_name": "options_advisor.log",
    "max_bytes":     10 * 1024 * 1024,
    "backup_count":  5,
    "format":        "%(asctime)s %(levelname)s %(name)s: %(message)s",
}


# ---------------------------------------------------------------------------
# Retention (weekly cleanup)
# ---------------------------------------------------------------------------
RETENTION_CONFIG = {
    "fo_bhav_keep_days":          730,   # 2 years of F&O EOD
    "spot_bhav_keep_days":        730,
    "vix_keep_days":              3650,  # 10 years (cheap, useful for IV%ile)
    "fii_keep_days":              730,
    "iv_history_keep_days":       730,
    "suggestions_keep_days":      1825,  # 5 years (audit)
    "trades_keep_days":           1825,
    "simulations_keep_days":      730,
    "system_logs_keep_days":      90,
    "job_log_keep_days":          90,
    "notifications_keep_days":    180,
}


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------
PATHS = {
    "data_dir":     _env("OPT_DATA_DIR", "data"),
    "logs_dir":     _env("OPT_LOGS_DIR", "logs"),
    "archive_dir":  _env("OPT_ARCHIVE_DIR", "archive"),
}


# ---------------------------------------------------------------------------
# High-impact events calendar
# ---------------------------------------------------------------------------
# Manually maintained list of HIGH-impact market events.
# The events_seeder job syncs this list to options_events_calendar on startup
# and weekly — so adding an event here is enough to get it into the gate.
#
# Sources to verify against each year:
#   RBI MPC schedule : https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx
#   Union Budget     : Ministry of Finance press releases
#   F&O Expiry       : already in options_expiry_calendar via fo_bhav_download
#
# impact must be 'HIGH' for the confidence gate to block suggestions.
# event_type codes: RBI_MPC, UNION_BUDGET, US_FOMC, GDP_RELEASE, CPI_RELEASE
EVENTS_CONFIG: list[dict] = [
    # ── 2026 RBI MPC Policy Decisions ──────────────────────────────────────
    {"date": "2026-02-07", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Feb 2026)",      "impact": "HIGH"},
    {"date": "2026-04-09", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Apr 2026)",      "impact": "HIGH"},
    {"date": "2026-06-06", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Jun 2026)",      "impact": "HIGH"},
    {"date": "2026-08-07", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Aug 2026)",      "impact": "HIGH"},
    {"date": "2026-10-09", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Oct 2026)",      "impact": "HIGH"},
    {"date": "2026-12-04", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Dec 2026)",      "impact": "HIGH"},

    # ── 2026 Union Budget ───────────────────────────────────────────────────
    {"date": "2026-02-01", "event_type": "UNION_BUDGET",  "description": "Union Budget 2026-27 presentation",       "impact": "HIGH"},

    # ── 2026 US Fed FOMC Decisions (affect Indian VIX / FII flows) ─────────
    {"date": "2026-01-29", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Jan 2026)",    "impact": "HIGH"},
    {"date": "2026-03-19", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Mar 2026)",    "impact": "HIGH"},
    {"date": "2026-05-07", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (May 2026)",    "impact": "HIGH"},
    {"date": "2026-06-18", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Jun 2026)",    "impact": "HIGH"},
    {"date": "2026-07-30", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Jul 2026)",    "impact": "HIGH"},
    {"date": "2026-09-17", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Sep 2026)",    "impact": "HIGH"},
    {"date": "2026-11-05", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Nov 2026)",    "impact": "HIGH"},
    {"date": "2026-12-17", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Dec 2026)",    "impact": "HIGH"},

    # ── 2026 India GDP (quarterly, released ~2 months after quarter end) ───
    {"date": "2026-02-28", "event_type": "GDP_RELEASE",   "description": "India GDP Q3 FY26 (Oct-Dec 2025)",        "impact": "HIGH"},
    {"date": "2026-05-29", "event_type": "GDP_RELEASE",   "description": "India GDP Q4 FY26 (Jan-Mar 2026)",        "impact": "HIGH"},
    {"date": "2026-08-28", "event_type": "GDP_RELEASE",   "description": "India GDP Q1 FY27 (Apr-Jun 2026)",        "impact": "HIGH"},
    {"date": "2026-11-27", "event_type": "GDP_RELEASE",   "description": "India GDP Q2 FY27 (Jul-Sep 2026)",        "impact": "HIGH"},
]

# ---------------------------------------------------------------------------
# Sanity check on import (cheap, fail-fast)
# ---------------------------------------------------------------------------
def _validate() -> None:
    assert STRATEGY_CONFIG["dte_min"] < STRATEGY_CONFIG["dte_max"], "dte_min must be < dte_max"
    assert 0.0 < STRATEGY_CONFIG["risk_free_rate"] < 1.0, "risk_free_rate out of range"
    assert STRATEGY_CONFIG["iv_rank_buying_max"] < STRATEGY_CONFIG["iv_rank_writing_min"], \
        "iv_rank gates overlap"
    assert 0.0 <= ZERODHA_CONFIG["gst_pct"] < 1.0
    assert DASHBOARD_CONFIG["port"] != 5000, "5001 is the dedicated options port"


_validate()
