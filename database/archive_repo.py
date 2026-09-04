"""
Move aged rows from hot tables into matching *_Archive tables.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import List, Optional

from config import RETENTION_CONFIG
from database.archive_registry import (
    ARCHIVE_TABLE_SPECS,
    ArchiveTableSpec,
    archive_table_name,
    ordered_specs,
    roots_first_for_export,
)
from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)


def _cutoff_for_spec(spec: ArchiveTableSpec, today: date) -> datetime | date:
    from datetime import timedelta

    days = int(RETENTION_CONFIG.get(spec.retention_key, 365))
    d = today - timedelta(days=days)
    if spec.date_type == "datetime":
        return datetime.combine(d, datetime.min.time())
    return d


def ensure_archive_tables(db: SQLServerConnection) -> None:
    for spec in ARCHIVE_TABLE_SPECS:
        hot = spec.hot_table
        arch = archive_table_name(hot)
        db.execute(
            f"""
            IF OBJECT_ID(N'{arch}', N'U') IS NULL
            BEGIN
                SELECT * INTO {arch} FROM {hot} WHERE 1 = 0;
                ALTER TABLE {arch} ADD archived_at DATETIME2(0) NOT NULL
                    CONSTRAINT DF_{arch}_archived_at DEFAULT SYSDATETIME();
                ALTER TABLE {arch} ADD archive_batch_id NVARCHAR(40) NULL;
            END
            """
        ).close()


def _pk_match_sql(spec: ArchiveTableSpec, alias_src: str, alias_tgt: str) -> str:
    parts = [f"{alias_tgt}.{c} = {alias_src}.{c}" for c in spec.pk_columns]
    return " AND ".join(parts)


def _move_child_by_parent(
    db: SQLServerConnection,
    spec: ArchiveTableSpec,
    batch_id: str,
    parent_hot: str,
    parent_date_col: str,
    parent_cutoff: datetime | date,
    parent_extra: Optional[str],
) -> int:
    arch = archive_table_name(spec.hot_table)
    pk = _pk_match_sql(spec, "s", "t")
    extra = f" AND ({parent_extra})" if parent_extra else ""
    parent_sub = (
        f"SELECT {spec.parent_key} FROM {parent_hot} "
        f"WHERE {parent_date_col} < ?{extra}"
    )
    ins = db.execute(
        f"""
        INSERT INTO {arch}
        SELECT s.*, SYSDATETIME(), ?
        FROM {spec.hot_table} s
        WHERE s.{spec.parent_key} IN ({parent_sub})
          AND NOT EXISTS (
            SELECT 1 FROM {arch} t WHERE {pk}
          )
        """,
        [batch_id, parent_cutoff],
    )
    n_ins = ins.rowcount or 0
    ins.close()
    de = db.execute(
        f"DELETE FROM {spec.hot_table} WHERE {spec.parent_key} IN ({parent_sub})",
        [parent_cutoff],
    )
    n_del = de.rowcount or 0
    de.close()
    return max(n_ins, n_del)


def move_spec(
    db: SQLServerConnection,
    spec: ArchiveTableSpec,
    batch_id: str,
    today: date,
) -> int:
    if spec.child_of:
        parent = next(s for s in ARCHIVE_TABLE_SPECS if s.hot_table == spec.child_of)
        cutoff = _cutoff_for_spec(parent, today)
        return _move_child_by_parent(
            db, spec, batch_id,
            parent.hot_table, parent.date_column, cutoff, parent.extra_where,
        )

    cutoff = _cutoff_for_spec(spec, today)
    hot = spec.hot_table
    arch = archive_table_name(hot)
    pk = _pk_match_sql(spec, "s", "t")
    extra = f" AND ({spec.extra_where})" if spec.extra_where else ""
    ins = db.execute(
        f"""
        INSERT INTO {arch}
        SELECT s.*, SYSDATETIME(), ?
        FROM {hot} s
        WHERE s.{spec.date_column} < ?{extra}
          AND NOT EXISTS (SELECT 1 FROM {arch} t WHERE {pk})
        """,
        [batch_id, cutoff],
    )
    n_ins = ins.rowcount or 0
    ins.close()
    de = db.execute(
        f"DELETE FROM {hot} WHERE {spec.date_column} < ?{extra}",
        [cutoff],
    )
    n_del = de.rowcount or 0
    de.close()
    logger.info("archive %s: moved ~%d rows (cutoff %s)", hot, n_del, cutoff)
    return n_del


def run_weekly_archive(db: SQLServerConnection, today: date) -> int:
    ensure_archive_tables(db)
    batch_id = uuid.uuid4().hex[:12]
    total = 0
    for spec in ordered_specs():
        try:
            total += move_spec(db, spec, batch_id, today)
        except Exception:
            logger.exception("archive move failed for %s", spec.hot_table)
            raise
    return total


def archive_table_row_counts(db: SQLServerConnection) -> dict[str, int]:
    out: dict[str, int] = {}
    for spec in ARCHIVE_TABLE_SPECS:
        arch = archive_table_name(spec.hot_table)
        row = db.fetch_one(f"SELECT COUNT(*) AS n FROM {arch}")
        out[arch] = int(row["n"]) if row else 0
    return out


def truncate_all_archive_tables(db: SQLServerConnection) -> int:
    n = 0
    for spec in roots_first_for_export():
        arch = archive_table_name(spec.hot_table)
        cur = db.execute(f"DELETE FROM {arch}")
        n += cur.rowcount or 0
        cur.close()
    return n
