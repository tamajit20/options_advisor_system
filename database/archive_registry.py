"""
Registry of hot tables eligible for move → *_Archive (not log tables).

Each spec drives DDL bootstrap, weekly archive moves, and laptop merge keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class ArchiveTableSpec:
    hot_table: str
    date_column: str
    retention_key: str
    pk_columns: Sequence[str]
    date_type: str = "date"  # "date" | "datetime"
    child_of: Optional[str] = None
    parent_key: Optional[str] = None
    extra_where: Optional[str] = None


ARCHIVE_RETENTION_KEY = "hot_archive_keep_days"


ARCHIVE_TABLE_SPECS: List[ArchiveTableSpec] = [
    ArchiveTableSpec(
        "options_fo_eod", "trade_date", ARCHIVE_RETENTION_KEY,
        ("trade_date", "symbol", "expiry_date", "strike", "option_type"),
    ),
    ArchiveTableSpec(
        "options_spot_eod", "trade_date", ARCHIVE_RETENTION_KEY,
        ("trade_date", "symbol"),
    ),
    ArchiveTableSpec(
        "options_vix_history", "trade_date", ARCHIVE_RETENTION_KEY,
        ("trade_date",),
    ),
    ArchiveTableSpec(
        "options_fii_data", "trade_date", ARCHIVE_RETENTION_KEY,
        ("trade_date", "client_type"),
    ),
    ArchiveTableSpec(
        "options_iv_history", "trade_date", ARCHIVE_RETENTION_KEY,
        ("trade_date", "symbol", "expiry_date", "strike", "option_type"),
    ),
    ArchiveTableSpec(
        "options_suggestion_legs", "suggestion_id", ARCHIVE_RETENTION_KEY,
        ("id",),
        child_of="options_suggestions",
        parent_key="suggestion_id",
    ),
    ArchiveTableSpec(
        "options_suggestions", "generated_on", ARCHIVE_RETENTION_KEY,
        ("suggestion_id",),
        date_type="datetime",
    ),
    ArchiveTableSpec(
        "options_trade_legs", "trade_id", ARCHIVE_RETENTION_KEY,
        ("id",),
        child_of="options_trades",
        parent_key="trade_id",
    ),
    ArchiveTableSpec(
        "options_trades", "closed_on", ARCHIVE_RETENTION_KEY,
        ("trade_id",),
        date_type="datetime",
        extra_where="status <> 'ACTIVE'",
    ),
    ArchiveTableSpec(
        "options_simulation_legs", "suggestion_id", ARCHIVE_RETENTION_KEY,
        ("id",),
        child_of="options_simulations",
        parent_key="suggestion_id",
    ),
    ArchiveTableSpec(
        "options_simulations", "completed_on", ARCHIVE_RETENTION_KEY,
        ("suggestion_id",),
        date_type="date",
        extra_where="completed_on IS NOT NULL",
    ),
    ArchiveTableSpec(
        "options_resuggestions", "generated_on", ARCHIVE_RETENTION_KEY,
        ("id",),
        date_type="datetime",
    ),
    ArchiveTableSpec(
        "options_notifications", "created_at", ARCHIVE_RETENTION_KEY,
        ("id",),
        date_type="datetime",
    ),
    ArchiveTableSpec(
        "options_chain_5min", "snapshot_at", ARCHIVE_RETENTION_KEY,
        ("snapshot_at", "symbol", "expiry_date"),
        date_type="datetime",
    ),
    ArchiveTableSpec(
        "options_atm_iv_5min", "snapshot_at", ARCHIVE_RETENTION_KEY,
        ("snapshot_at", "symbol", "expiry_date"),
        date_type="datetime",
    ),
    ArchiveTableSpec(
        "options_trade_mtm_snapshot_history", "archived_at", ARCHIVE_RETENTION_KEY,
        ("id",),
        date_type="datetime",
    ),
    ArchiveTableSpec(
        "options_intraday_close_snapshot", "snapshot_date", ARCHIVE_RETENTION_KEY,
        ("snapshot_date", "trade_id", "leg_order"),
    ),
    ArchiveTableSpec(
        "options_trade_level_events", "event_at", ARCHIVE_RETENTION_KEY,
        ("id",),
        date_type="datetime",
    ),
    ArchiveTableSpec(
        "options_em_calibration", "created_at", ARCHIVE_RETENTION_KEY,
        ("id",),
        date_type="datetime",
    ),
]


def archive_table_name(hot_table: str) -> str:
    return f"{hot_table}_Archive"


def ordered_specs() -> List[ArchiveTableSpec]:
    """Parents before children."""
    roots = [s for s in ARCHIVE_TABLE_SPECS if not s.child_of]
    children = [s for s in ARCHIVE_TABLE_SPECS if s.child_of]
    return roots + children


def roots_first_for_export() -> List[ArchiveTableSpec]:
    """Children before parents on truncate/delete from hot (reverse insert order)."""
    return list(reversed(ordered_specs()))
