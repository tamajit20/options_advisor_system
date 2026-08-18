"""Tests for database.config_overlay — Config page → live config dicts."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config import (
    STRATEGY_CONFIG,
    STRATEGY_CONFIG_DEFAULTS,
    ZERODHA_CONFIG,
    ZERODHA_CONFIG_DEFAULTS,
)
from database.config_overlay import (
    apply_strategy_overrides,
    catalog_items,
    coerce_value,
    restore_file_defaults,
)


def test_coerce_take_profit_fraction():
    assert coerce_value("take_profit_fraction", "0.6") == pytest.approx(0.6)


def test_coerce_rejects_unknown_key():
    with pytest.raises(KeyError):
        coerce_value("not_a_key", 1)


def test_coerce_namespaced_charge():
    assert coerce_value("zerodha_charges.gst_pct", "0.18") == pytest.approx(0.18)


def test_overlay_applies_db_row(monkeypatch):
    restore_file_defaults()
    file_default = STRATEGY_CONFIG_DEFAULTS["take_profit_fraction"]
    db = MagicMock()
    db.fetch_all.return_value = [{
        "config_key": "take_profit_fraction",
        "config_value": "0.55",
        "last_modified": None,
        "modified_by": "test",
        "is_locked": 0,
    }]
    try:
        n = apply_strategy_overrides(db)
        assert n == 1
        assert STRATEGY_CONFIG["take_profit_fraction"] == pytest.approx(0.55)
    finally:
        restore_file_defaults()
        assert STRATEGY_CONFIG["take_profit_fraction"] == file_default


def test_overlay_applies_namespaced_charge():
    restore_file_defaults()
    file_default = ZERODHA_CONFIG_DEFAULTS["gst_pct"]
    db = MagicMock()
    db.fetch_all.return_value = [{
        "config_key": "zerodha_charges.gst_pct",
        "config_value": "0.05",
        "last_modified": None,
        "modified_by": "test",
        "is_locked": 0,
    }]
    try:
        n = apply_strategy_overrides(db)
        assert n == 1
        assert ZERODHA_CONFIG["gst_pct"] == pytest.approx(0.05)
    finally:
        restore_file_defaults()
        assert ZERODHA_CONFIG["gst_pct"] == file_default


def test_catalog_includes_pnl_keys():
    db = MagicMock()
    db.fetch_all.return_value = []
    items = {row["key"]: row for row in catalog_items(db)}
    assert "long_premium_target_base" in items
    assert items["long_premium_target_base"]["group"] == "pnl"
    assert items["strategy_sl_limits"]["type"] == "json"
    assert items["take_profit_fraction"]["overridden"] is False


def test_catalog_covers_strategy_and_namespaces():
    db = MagicMock()
    db.fetch_all.return_value = []
    items = {row["key"]: row for row in catalog_items(db)}
    missing = [k for k in STRATEGY_CONFIG_DEFAULTS if k not in items]
    assert missing == []
    assert "scheduler.timezone" in items
    assert items["scheduler.timezone"]["needs_restart"] is True
    assert "scheduler.eod_pipeline_skip_steps" in items
    assert items["scheduler.eod_pipeline_skip_steps"]["needs_restart"] is False
    assert "zerodha_charges.gst_pct" in items
    assert items["zerodha_charges.gst_pct"]["group"] == "charges"
    assert "events.calendar" in items
    assert items["events.calendar"]["type"] == "json"
    assert "alerts.telegram_enabled" in items
    assert items["alerts.telegram_enabled"]["needs_restart"] is True
    assert "zerodha_api.enabled" in items
    assert "providers.active" in items
    secret_keys = {
        k for k in items
        if "password" in k or "bot_token" in k or k.endswith("api_key")
        or "api_secret" in k or "access_token" in k
    }
    assert not secret_keys
    assert "dashboard.api_key" not in items
    assert "alerts.smtp_password" not in items
