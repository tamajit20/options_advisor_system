"""Pytest wrapper for scripts/validate_setup_sync.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_validate_module():
    path = ROOT / "scripts" / "validate_setup_sync.py"
    spec = importlib.util.spec_from_file_location("validate_setup_sync", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestValidateSetupSync:
    def test_main_passes(self):
        mod = _load_validate_module()
        assert mod.main() == 0

    def test_broker_orders_not_log_table(self):
        mod = _load_validate_module()
        assert "options_broker_orders" not in mod.LOG_TABLES
        assert "options_system_logs" in mod.LOG_TABLES
        assert "options_job_log" in mod.LOG_TABLES

    def test_broker_orders_in_archive_registry(self):
        from database.archive_registry import ARCHIVE_TABLE_SPECS

        hot = {s.hot_table for s in ARCHIVE_TABLE_SPECS}
        assert "options_broker_orders" in hot

    def test_log_table_must_not_be_archived_rule(self):
        from database.archive_registry import ARCHIVE_TABLE_SPECS

        mod = _load_validate_module()
        archived_hot = {s.hot_table for s in ARCHIVE_TABLE_SPECS}
        overlap = archived_hot & mod.LOG_TABLES
        assert overlap == set(), f"log tables incorrectly archived: {overlap}"
