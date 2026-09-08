"""
lifecycle/archive_export.py
=============================

Build a chunk .bak of pending *_Archive rows on the VM (Fri before shutdown).
Laptop merges into cumulative OptionsAdvisorDB_Archive; VM truncates after ACK.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from database.archive_repo import archive_table_row_counts, ensure_archive_tables
from database.connection import SQLServerConnection
from lifecycle.sql_backup import (
    backup_database,
    bak_filename,
    host_backup_dir,
    prepare_backup_dirs,
    prune_bak_files,
    sql_autocommit,
    sql_ident,
)
from utils import now_ist

logger = logging.getLogger(__name__)


def pending_manifest_path() -> Path:
    return host_backup_dir() / "archive" / "PENDING.json"


def run_archive_export(db: SQLServerConnection) -> int:
    """Create export .bak if any *_Archive rows exist; write PENDING.json."""
    from config import ARCHIVE_EXPORT_CONFIG, DATABASE_CONFIG

    if not ARCHIVE_EXPORT_CONFIG.get("enabled", True):
        logger.info("archive_export disabled in config")
        return 0

    ensure_archive_tables(db)
    counts = archive_table_row_counts(db)
    total = sum(counts.values())
    if total <= 0:
        logger.info("archive_export: no pending *_Archive rows")
        _remove_pending_export_files()
        return 0

    main_db = sql_ident(str(DATABASE_CONFIG.get("database") or "OptionsAdvisorDB"))
    export_db = sql_ident(
        str(ARCHIVE_EXPORT_CONFIG.get("export_db_name") or "OptionsAdvisorDB_ArchiveExport")
    )
    timeout = int(ARCHIVE_EXPORT_CONFIG.get("export_timeout_seconds", 900))
    stamp = now_ist().strftime("%Y%m%d-%H%M%S")
    bak_name = bak_filename(f"{export_db}-{stamp}.bak")

    prepare_backup_dirs()
    _build_export_database(db, main_db, export_db, counts, timeout=timeout)
    dest_dir = host_backup_dir() / "archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest_dir.chmod(0o777)
    except OSError:
        pass
    backup_database(
        db,
        export_db,
        bak_name,
        timeout=max(60, timeout - 30),
        dest_dir=dest_dir,
        sql_subdir="archive",
    )
    prune_bak_files(dest_dir, keep_names={bak_name})

    rel_bak = f"backups/archive/{bak_name}"
    manifest = pending_manifest_path()
    data = {
        "bak_file": rel_bak,
        "bak_name": bak_name,
        "export_db": export_db,
        "main_db": main_db,
        "stamp": stamp,
        "row_counts": counts,
        "total_rows": total,
        "exported_at": now_ist().isoformat(),
    }
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info(
        "archive_export ready: %s (%d rows across %d tables)",
        rel_bak, total, sum(1 for v in counts.values() if v),
    )
    return 1


def _build_export_database(
    db: SQLServerConnection,
    main_db: str,
    export_db: str,
    counts: dict[str, int],
    *,
    timeout: int,
) -> None:
    """Recreate export-DB archive tables from current *_Archive contents."""
    create_sql = (
        f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = N'{export_db}') "
        f"CREATE DATABASE [{export_db}];"
    )
    with sql_autocommit(db, timeout=timeout):
        db.execute(create_sql).close()
        for table in counts:
            table = sql_ident(table)
            db.execute(
                f"IF OBJECT_ID(N'[{export_db}].[dbo].[{table}]', N'U') IS NOT NULL "
                f"DROP TABLE [{export_db}].[dbo].[{table}];"
            ).close()
        copied = 0
        for table, n in counts.items():
            if n <= 0:
                continue
            table = sql_ident(table)
            logger.info("archive_export copy %s (%d rows)", table, n)
            db.execute(
                f"SELECT * INTO [{export_db}].[dbo].[{table}] "
                f"FROM [{main_db}].[dbo].[{table}];"
            ).close()
            copied += 1
        if copied <= 0:
            raise RuntimeError("archive_export: counts were positive but no tables copied")


def _clear_pending_manifest() -> None:
    p = pending_manifest_path()
    if p.is_file():
        p.unlink()


def _remove_pending_export_files() -> None:
    """Drop PENDING.json and leftover archive ``*.bak`` on the VM."""
    archive_dir = host_backup_dir() / "archive"
    prune_bak_files(archive_dir, keep_names=set())
    _clear_pending_manifest()


def acknowledge_export(db: SQLServerConnection) -> int:
    """Truncate VM *_Archive after laptop merge and delete the export .bak."""
    from database.archive_repo import truncate_all_archive_tables

    n = truncate_all_archive_tables(db)
    db.commit()
    _remove_pending_export_files()
    logger.info("archive export acknowledged: cleared %d archive rows on VM", n)
    return n
