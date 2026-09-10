"""
lifecycle/archive_export.py
=============================

Build a chunk .bak of pending *_Archive rows on the VM (Fri before shutdown).
Laptop merges into cumulative OptionsAdvisorDB_Archive; ACK then deletes the
matching hot rows, truncates VM *_Archive, and drops the export .bak.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from database.archive_repo import archive_table_row_counts, ensure_archive_tables
from database.connection import SQLServerConnection
from lifecycle.sql_backup import (
    backup_database,
    bak_filename,
    host_backup_dir,
    hot_backup_marker_path,
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
        if pending_manifest_path().is_file():
            logger.info(
                "PENDING.json still present; leaving VM export files until laptop ACK"
            )
            return 0
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
    # Keep older export chunks until laptop ACK. Pruning here would delete last
    # week's .bak while the laptop was off.

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


def _parse_iso(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _load_pending_manifest() -> dict | None:
    path = pending_manifest_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid PENDING.json: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("PENDING.json must be a JSON object")
    return data


def _pending_row_counts_match_live(pending: dict, live: dict[str, int]) -> bool:
    expected = pending.get("row_counts")
    if not isinstance(expected, dict):
        return False
    if set(expected) != set(live):
        return False
    for table, n in live.items():
        try:
            if int(expected.get(table, -1)) != int(n):
                return False
        except (TypeError, ValueError):
            return False
    return True


def require_hot_backup_after_export(pending: dict) -> None:
    """Refuse ACK unless db_backup finished after this week's archive export."""
    exported_raw = str(pending.get("exported_at") or "").strip()
    if not exported_raw:
        raise RuntimeError("PENDING.json missing exported_at; refusing to delete hot rows")
    marker = hot_backup_marker_path()
    if not marker.is_file():
        raise RuntimeError(
            "hot backup not confirmed (missing LAST_HOT_BACKUP.json); "
            "re-run db_backup before ACK"
        )
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid LAST_HOT_BACKUP.json: {exc}") from exc
    backup_raw = str((marker_data or {}).get("completed_at") or "").strip()
    if not backup_raw:
        raise RuntimeError("LAST_HOT_BACKUP.json missing completed_at; refusing ACK")
    try:
        exported_at = _parse_iso(exported_raw)
        backup_at = _parse_iso(backup_raw)
    except ValueError as exc:
        raise RuntimeError(f"cannot parse backup/export timestamps: {exc}") from exc
    if backup_at.tzinfo is None and exported_at.tzinfo is not None:
        exported_at = exported_at.replace(tzinfo=None)
    elif exported_at.tzinfo is None and backup_at.tzinfo is not None:
        backup_at = backup_at.replace(tzinfo=None)
    if backup_at < exported_at:
        raise RuntimeError(
            "hot backup is older than this archive export; re-run db_backup before ACK"
        )


def acknowledge_export(db: SQLServerConnection) -> int:
    """After laptop merge: delete mirrored hot rows, then clear VM archive export."""
    from database.archive_repo import (
        archive_table_row_counts,
        delete_hot_rows_present_in_archive,
        truncate_all_archive_tables,
    )

    pending = _load_pending_manifest()
    counts = archive_table_row_counts(db)
    archived = sum(counts.values())
    if pending is None:
        if archived <= 0:
            _remove_pending_export_files()
            logger.info("archive ACK: nothing pending")
            return 0
        raise RuntimeError(
            "*_Archive has rows but PENDING.json is missing; refusing ACK"
        )

    require_hot_backup_after_export(pending)
    if not _pending_row_counts_match_live(pending, counts):
        raise RuntimeError(
            "*_Archive row counts changed since export; "
            "waiting for archive_export to rebuild PENDING before deleting hot rows"
        )
    n_hot = delete_hot_rows_present_in_archive(db)
    n_arch = truncate_all_archive_tables(db)
    db.commit()
    from lifecycle.sql_backup import shrink_transaction_log_quietly

    shrink_transaction_log_quietly(db)
    _remove_pending_export_files()
    logger.info(
        "archive export acknowledged: deleted %d hot rows, cleared %d archive rows",
        n_hot, n_arch,
    )
    return n_hot
