"""Tests for scout settings schema and helpers."""

from __future__ import annotations

from datetime import datetime, time
from unittest.mock import patch

from scout.settings_schema import (
    compute_trade_quantity,
    default_scout_settings,
    in_trading_window,
    merge_scout_settings,
    strength_allowed,
    suggested_quantity,
    validate_scout_settings,
)


def test_default_settings_has_investment_sizing():
    d = default_scout_settings()
    assert d["use_investment_sizing"] is True
    assert d["investment_per_trade_inr"] == 20_000
    assert d["max_trades_per_day"] == 5
    assert d["dedupe_per_symbol"] is True
    assert "MEDIUM" in d["auto_enter_strengths"]


def test_validate_clamps_investment():
    out = validate_scout_settings({"investment_per_trade_inr": 50})
    assert out["investment_per_trade_inr"] == 1000.0


def test_merge_scout_settings_partial():
    merged = merge_scout_settings({"max_trades_per_day": 3})
    assert merged["max_trades_per_day"] == 3
    assert merged["investment_per_trade_inr"] == 20_000


def test_compute_trade_quantity_from_investment():
    settings = {"use_investment_sizing": True, "investment_per_trade_inr": 20_000}
    assert compute_trade_quantity(settings, 1000.0) == 20
    assert compute_trade_quantity(settings, 2500.0) == 8


def test_compute_trade_quantity_fixed():
    settings = {"use_investment_sizing": False, "auto_trade_quantity": 5}
    assert compute_trade_quantity(settings, 100.0) == 5


def test_strength_allowed():
    settings = {"auto_enter_strengths": ["MEDIUM", "HIGH"]}
    assert strength_allowed(settings, "MEDIUM")
    assert not strength_allowed(settings, "WEAK")


@patch("utils.now_ist")
def test_in_trading_window(mock_now):
    mock_now.return_value = datetime(2026, 8, 12, 10, 0, 0)
    settings = {"trade_window_start": "09:45", "trade_window_end": "14:30"}
    assert in_trading_window(settings) is True
    mock_now.return_value = datetime(2026, 8, 12, 15, 0, 0)
    assert in_trading_window(settings) is False


def test_suggested_quantity_fallback():
    settings = {"use_investment_sizing": False, "auto_trade_quantity": 2}
    assert suggested_quantity(settings, None) == 2


def test_suggested_quantity_investment_sizing():
    settings = {"use_investment_sizing": True, "investment_per_trade_inr": 20_000}
    assert suggested_quantity(settings, 2500.0) == 8


def test_validate_swaps_inverted_window():
    out = validate_scout_settings({
        "trade_window_start": "15:00",
        "trade_window_end": "09:00",
    })
    assert out["trade_window_start"] == "09:45"
    assert out["trade_window_end"] == "14:30"


def test_validate_filters_invalid_strengths():
    out = validate_scout_settings({
        "auto_enter_strengths": ["WEAK", "INVALID", "medium", "HIGH"],
    })
    assert out["auto_enter_strengths"] == ["WEAK", "MEDIUM", "HIGH"]


def test_effective_pattern_config_merges_settings():
    from scout.settings_schema import effective_pattern_config

    cfg = effective_pattern_config({"min_candles": 20, "entry_slippage_pct": 0.5})
    assert cfg["min_candles"] == 20
    assert cfg["entry_slippage_pct"] == 0.5
    assert "or_minutes" in cfg
