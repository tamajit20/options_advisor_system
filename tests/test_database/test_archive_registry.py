"""Tests for database/archive_registry.py — unified hot archive window."""
from __future__ import annotations

from database import schema as sc
from database.archive_registry import (
    ARCHIVE_RETENTION_KEY,
    ARCHIVE_TABLE_SPECS,
    archive_table_name,
    ordered_specs,
    roots_first_for_export,
)
from config import RETENTION_CONFIG


class TestArchiveRegistry:
    def test_all_specs_use_unified_retention_key(self):
        keys = {s.retention_key for s in ARCHIVE_TABLE_SPECS}
        assert keys == {ARCHIVE_RETENTION_KEY}

    def test_broker_orders_spec(self):
        spec = next(s for s in ARCHIVE_TABLE_SPECS if s.hot_table == "options_broker_orders")
        assert spec.date_column == "created_at"
        assert spec.date_type == "datetime"
        assert spec.pk_columns == ("id",)
        assert spec.child_of is None
        assert spec.retention_key == ARCHIVE_RETENTION_KEY

    def test_broker_orders_archive_table_name(self):
        assert archive_table_name("options_broker_orders") == "options_broker_orders_Archive"

    def test_broker_orders_in_schema_and_registry(self):
        assert "options_broker_orders" in sc.list_tables()
        hot = {s.hot_table for s in ARCHIVE_TABLE_SPECS}
        assert "options_broker_orders" in hot

    def test_ordered_specs_parents_before_children(self):
        seen_parents: set[str] = set()
        for spec in ordered_specs():
            if spec.child_of:
                assert spec.child_of in seen_parents, (
                    f"{spec.hot_table} child before parent {spec.child_of}"
                )
            else:
                seen_parents.add(spec.hot_table)

    def test_roots_first_is_reverse_of_ordered(self):
        assert roots_first_for_export() == list(reversed(ordered_specs()))

    def test_retention_config_has_hot_archive_and_broker_alias(self):
        assert RETENTION_CONFIG["hot_archive_keep_days"] == 365
        assert RETENTION_CONFIG["broker_orders_keep_days"] == 365
        assert RETENTION_CONFIG["broker_orders_keep_days"] == RETENTION_CONFIG["hot_archive_keep_days"]

    def test_every_spec_retention_key_in_config(self):
        for spec in ARCHIVE_TABLE_SPECS:
            assert spec.retention_key in RETENTION_CONFIG
