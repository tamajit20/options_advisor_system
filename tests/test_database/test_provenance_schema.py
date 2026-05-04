"""
tests/test_database/test_provenance_schema.py
=============================================

Phase 2c — provenance markers schema migration tests.

Verifies the ALTER TABLE statements that add provenance columns are present
in `_TABLE_DDL` (executed at startup so existing DBs auto-migrate).
"""

from __future__ import annotations

import re

from database import schema as sc


_DDL_TEXT = "\n".join(sc._TABLE_DDL)


def _has_alter(table: str, column: str) -> bool:
    """True if there's an idempotent ALTER TABLE adding `column` to `table`."""
    pattern = re.compile(
        rf"object_id = OBJECT_ID\('{table}'\) AND name = '{column}'.*?"
        rf"ALTER TABLE {table} ADD {column}\b",
        re.DOTALL | re.IGNORECASE,
    )
    return bool(pattern.search(_DDL_TEXT))


# ---------------------------------------------------------------------------
# options_suggestions — 9 provenance columns
# ---------------------------------------------------------------------------
def test_suggestions_has_data_source():
    assert _has_alter("options_suggestions", "data_source")


def test_suggestions_has_provider():
    assert _has_alter("options_suggestions", "provider")


def test_suggestions_has_data_as_of():
    assert _has_alter("options_suggestions", "data_as_of")


def test_suggestions_has_trigger_type():
    assert _has_alter("options_suggestions", "trigger_type")


def test_suggestions_has_trigger_reason():
    assert _has_alter("options_suggestions", "trigger_reason")


def test_suggestions_has_market_state_at_gen():
    assert _has_alter("options_suggestions", "market_state_at_gen")


def test_suggestions_has_live_data_freshness_ms():
    assert _has_alter("options_suggestions", "live_data_freshness_ms")


def test_suggestions_has_engine_version():
    assert _has_alter("options_suggestions", "engine_version")


def test_suggestions_has_validator_status():
    assert _has_alter("options_suggestions", "validator_status")


# ---------------------------------------------------------------------------
# options_suggestion_legs
# ---------------------------------------------------------------------------
def test_suggestion_legs_has_leg_price_basis():
    assert _has_alter("options_suggestion_legs", "leg_price_basis")


# ---------------------------------------------------------------------------
# options_trades — 5 execution provenance columns
# ---------------------------------------------------------------------------
def test_trades_has_execution_data_source():
    assert _has_alter("options_trades", "execution_data_source")


def test_trades_has_execution_provider():
    assert _has_alter("options_trades", "execution_provider")


def test_trades_has_execution_freshness_ms():
    assert _has_alter("options_trades", "execution_freshness_ms")


def test_trades_has_gate_passed():
    assert _has_alter("options_trades", "gate_passed")


def test_trades_has_time_from_suggestion_sec():
    assert _has_alter("options_trades", "time_from_suggestion_sec")


# ---------------------------------------------------------------------------
# options_notifications — 4 provenance columns
# ---------------------------------------------------------------------------
def test_notifications_has_source_event_id():
    assert _has_alter("options_notifications", "source_event_id")


def test_notifications_has_provider():
    assert _has_alter("options_notifications", "provider")


def test_notifications_has_tick_age_ms():
    assert _has_alter("options_notifications", "tick_age_ms")


def test_notifications_has_flag_state_at_dispatch():
    assert _has_alter("options_notifications", "flag_state_at_dispatch")


# ---------------------------------------------------------------------------
# Idempotence — every provenance ALTER must be guarded by a NOT EXISTS check
# so re-running create_all_tables() on an already-migrated DB is a no-op.
# ---------------------------------------------------------------------------
def test_every_provenance_alter_is_guarded():
    provenance_columns = [
        "data_source", "provider", "data_as_of", "trigger_type",
        "trigger_reason", "market_state_at_gen", "live_data_freshness_ms",
        "engine_version", "validator_status",
        "leg_price_basis",
        "execution_data_source", "execution_provider",
        "execution_freshness_ms", "gate_passed", "time_from_suggestion_sec",
        "source_event_id", "tick_age_ms", "flag_state_at_dispatch",
    ]
    # `provider` appears on both options_suggestions and options_notifications,
    # so we only require AT LEAST one guarded ALTER per name.
    for col in provenance_columns:
        guarded = re.search(
            rf"IF NOT EXISTS\s*\(SELECT 1 FROM sys\.columns.*?name = '{col}'\)\s*"
            rf"ALTER TABLE \w+ ADD {col}\b",
            _DDL_TEXT, re.DOTALL | re.IGNORECASE,
        )
        assert guarded, f"missing guarded ALTER for column {col!r}"
