"""
lifecycle/archive_export.py
=============================

Build a chunk .bak of pending *_Archive rows on the VM (Fri before shutdown).
Laptop merges into cumulative OptionsAdvisorDB_Archive; VM truncates after ACK.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from database.archive_registry import ARCHIVE_TABLE_SPECS, archive_table_name
from database.archive_repo import archive_table_row_counts, ensure_archive_tables
from database.connection import SQLServerConnection
from utils import now_ist

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def pending_manifest_path() -> Path:
    return _repo_root() / "backups" / "archive" / "PENDING.json"


def run_archive_export(db: SQLServerConnection) -> int:
    """Create export .bak if any *_Archive rows exist; write PENDING.json."""
    from config import ARCHIVE_EXPORT_CONFIG

    if not ARCHIVE_EXPORT_CONFIG.get("enabled", True):
        logger.info("archive_export disabled in config")
        return 0

    ensure_archive_tables(db)
    counts = archive_table_row_counts(db)
    total = sum(counts.values())
    if total <= 0:
        logger.info("archive_export: no pending *_Archive rows")
        _clear_pending_manifest()
        return 0

    root = _repo_root()
    script = root / "deploy" / "archive-export.sh"
    if not script.is_file():
        raise FileNotFoundError(f"missing {script}")

    subprocess.run(
        ["bash", str(script)],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
        timeout=int(ARCHIVE_EXPORT_CONFIG.get("export_timeout_seconds", 900)),
    )

    manifest = pending_manifest_path()
    if not manifest.is_file():
        raise RuntimeError("archive export finished but PENDING.json missing")

    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["row_counts"] = counts
    data["total_rows"] = total
    data["exported_at"] = now_ist().isoformat()
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info(
        "archive_export ready: %s (%d rows across %d tables)",
        data.get("bak_file"), total, sum(1 for v in counts.values() if v),
    )
    return 1


def _clear_pending_manifest() -> None:
    p = pending_manifest_path()
    if p.is_file():
        p.unlink()


def acknowledge_export(db: SQLServerConnection) -> int:
    """Truncate VM *_Archive after laptop merge succeeded."""
    from database.archive_repo import truncate_all_archive_tables

    n = truncate_all_archive_tables(db)
    db.commit()
    _clear_pending_manifest()
    logger.info("archive export acknowledged: cleared %d archive rows on VM", n)
    return n
