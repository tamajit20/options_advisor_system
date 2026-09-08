"""Batched deletes for log/alert tables so weekly cleanup stays under ODBC timeout."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DELETE_BATCH_SIZE = 5000

_LOG_DELETE_INDEXES = (
    (
        "IX_options_system_logs_logged_at",
        "options_system_logs",
        "logged_at",
    ),
    (
        "IX_options_notifications_created",
        "options_notifications",
        "created_at",
    ),
    (
        "IX_options_zerodha_jobs_created",
        "options_zerodha_execution_jobs",
        "created_at",
    ),
)


def _ident(name: str) -> str:
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def ensure_log_delete_indexes(db: SQLServerConnection) -> None:
    """Seek-friendly indexes for ``WHERE created/logged_at < cutoff``.

    Built here (not at app bootstrap) so a large first CREATE INDEX uses the
    job's raised query timeout instead of the 60s default.
    """
    for idx, table, col in _LOG_DELETE_INDEXES:
        idx = _ident(idx)
        table = _ident(table)
        col = _ident(col)
        try:
            db.execute(
                f"""
                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes WHERE name = N'{idx}'
                      AND object_id = OBJECT_ID(N'dbo.{table}')
                )
                CREATE INDEX {idx} ON {table} ({col})
                """
            ).close()
            db.commit()
        except Exception:
            logger.warning(
                "retention: could not ensure index %s on %s", idx, table,
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass


def delete_older_than(
    db: SQLServerConnection,
    table: str,
    column: str,
    cutoff: date,
    *,
    batch_size: int = DELETE_BATCH_SIZE,
) -> int:
    """Delete rows older than ``cutoff`` in TOP(N) batches.

    A single DELETE of weeks of ``options_system_logs`` exceeds the 60s
    pyodbc query timeout (HYT00). Each batch is committed so a later
    timeout still keeps progress.
    """
    table = _ident(table)
    column = _ident(column)
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())
    total = 0
    sql = f"DELETE TOP ({int(batch_size)}) FROM {table} WHERE {column} < ?"
    while True:
        cur = db.execute(sql, [cutoff_dt])
        n = int(cur.rowcount or 0)
        cur.close()
        if n < 0:
            n = 0
        total += n
        db.commit()
        if n < batch_size:
            break
    return total
