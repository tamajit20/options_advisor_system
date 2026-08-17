"""Tests for arb settings schema and config_loader."""

from __future__ import annotations

from unittest.mock import MagicMock

from arb.config_loader import get_arb_settings, set_arb_settings
from arb.settings_schema import default_arb_settings, merge_arb_settings, validate_arb_settings


def test_default_arb_settings_has_expected_keys():
    d = default_arb_settings()
    assert "enabled" in d
    assert "min_gap_store_pct" in d
    assert "min_duration_store_sec" in d


def test_validate_arb_settings_clamps_values():
    raw = {
        "tick_staleness_sec": 100,
        "leg_stale_close_sec": 0.1,
        "min_gap_store_pct": -1,
        "min_duration_store_sec": 99999,
        "universe": "invalid",
    }
    v = validate_arb_settings(raw)
    assert v["tick_staleness_sec"] == 30.0
    assert v["leg_stale_close_sec"] >= v["tick_staleness_sec"]
    assert v["min_gap_store_pct"] == 0.0
    assert v["min_duration_store_sec"] == 3600
    assert v["universe"] == default_arb_settings()["universe"]


def test_merge_arb_settings_partial():
    merged = merge_arb_settings({"min_gap_store_pct": 0.75})
    assert merged["min_gap_store_pct"] == 0.75
    assert merged["enabled"] == default_arb_settings()["enabled"]


def test_set_arb_settings_updates_cache(mocker):
    db = MagicMock()
    repo = MagicMock()
    mocker.patch("database.arb_models.ArbConfigRepo", return_value=repo)
    payload = {**default_arb_settings(), "min_gap_store_pct": 0.3}
    out = set_arb_settings(db, payload)
    assert out["min_gap_store_pct"] == 0.3
    repo.set_settings.assert_called_once()
    cached = get_arb_settings(db)
    assert cached["min_gap_store_pct"] == 0.3
