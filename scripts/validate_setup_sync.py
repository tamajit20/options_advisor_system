#!/usr/bin/env python3
"""Verify bootstrap files stay in sync with schema and archive registry."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRED_SCRIPTS = [
    "readmefirst.txt",
    "deploy/azure/setup-new-environment.ps1",
    "deploy/azure/setup-laptop.ps1",
    "deploy/azure/Test-EnvironmentSetup.ps1",
    "deploy/azure/setup-manifest.json",
    "deploy/azure/MAINTAIN-SETUP.md",
    "deploy/archive-export.sh",
    "deploy/archive-truncate-vm.sh",
    "scripts/merge_archive_into_local.py",
    "database/archive_registry.py",
    "database/archive_repo.py",
    "lifecycle/archive_orchestrator.py",
    "lifecycle/archive_export.py",
]

LOG_TABLES = frozenset({
    "options_system_logs",
    "options_job_log",
    "options_zerodha_execution_jobs",
})

NEVER_ARCHIVE = frozenset({
    "options_config",
    "options_runtime_flags",
    "options_lot_sizes",
    "options_expiry_calendar",
    "options_events_calendar",
    "options_trade_mtm_snapshot",
})


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    for rel in REQUIRED_SCRIPTS:
        if not (ROOT / rel).is_file():
            errors.append(f"missing required file: {rel}")

    manifest = ROOT / "deploy/azure/setup-manifest.json"
    if manifest.is_file():
        try:
            json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON: {manifest}: {exc}")

    readme = ROOT / "readmefirst.txt"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        for name in (
            "setup-new-environment.ps1",
            "Test-EnvironmentSetup.ps1",
            "validate_setup_sync.py",
            "MAINTAIN-SETUP.md",
        ):
            if name not in text:
                warnings.append(f"readmefirst.txt does not mention {name}")

    from database.schema import list_tables
    from database.archive_registry import ARCHIVE_TABLE_SPECS

    known = set(list_tables())
    archived_hot = {s.hot_table for s in ARCHIVE_TABLE_SPECS}

    for spec in ARCHIVE_TABLE_SPECS:
        if spec.hot_table not in known:
            errors.append(f"archive_registry hot table not in list_tables(): {spec.hot_table}")
        if spec.child_of and spec.child_of not in archived_hot:
            errors.append(f"archive child parent not in registry: {spec.child_of}")

    for table in archived_hot:
        if table not in known:
            errors.append(f"list_tables() missing archived table: {table}")

    historical = known - LOG_TABLES - NEVER_ARCHIVE
    not_archived = historical - archived_hot
    for table in sorted(not_archived):
        warnings.append(f"historical table not in archive_registry (intentional?): {table}")

    for table in sorted(archived_hot & LOG_TABLES):
        errors.append(f"log table must not be archived: {table}")

    from config import RETENTION_CONFIG, SCHEDULER_CONFIG

    for spec in ARCHIVE_TABLE_SPECS:
        if spec.retention_key not in RETENTION_CONFIG:
            errors.append(f"RETENTION_CONFIG missing key: {spec.retention_key}")
    if "hot_archive_keep_days" not in RETENTION_CONFIG:
        errors.append("RETENTION_CONFIG missing hot_archive_keep_days")

    for job in ("weekly_archive", "weekly_log_cleanup", "archive_export"):
        if job not in SCHEDULER_CONFIG.get("jobs", {}):
            errors.append(f"SCHEDULER_CONFIG missing job: {job}")

    print("SETUP SYNC VALIDATION")
    print("=" * 50)
    for w in warnings:
        print(f"  WARN: {w}")
    for e in errors:
        print(f"  FAIL: {e}")
    if not warnings and not errors:
        print("  OK: all checks passed")
    print("=" * 50)
    print(f"errors={len(errors)} warnings={len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
