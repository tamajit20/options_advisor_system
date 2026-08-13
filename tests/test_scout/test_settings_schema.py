"""Tests for scout settings schema and helpers."""

from __future__ import annotations

from datetime import datetime, time
from unittest.mock import patch

from scout.settings_schema import (
    compute_trade_quantity,
    default_scout_settings,
    format_square_off_time,
    in_trading_window,
    merge_scout_settings,
    square_off_datetime,
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
    assert "HIGH" in d["auto_enter_strengths"]
    assert d["auto_close_poll_seconds"] == 10


def test_validate_clamps_auto_close_poll_seconds():
    assert validate_scout_settings({"auto_close_poll_seconds": 1})["auto_close_poll_seconds"] == 5
    assert validate_scout_settings({"auto_close_poll_seconds": 999})["auto_close_poll_seconds"] == 120


def test_validate_regime_and_liquidity_settings():
    out = validate_scout_settings({
        "index_trend_min_pct": -10,
        "index_trend_max_pct": 10,
        "pdh_pdl_buffer_pct": 5,
        "min_bar_volume": -100,
        "min_turnover_inr": 99_999_999,
        "entry_pending_max_minutes": 999,
    })
    assert out["index_trend_min_pct"] == -5.0
    assert out["index_trend_max_pct"] == 5.0
    assert out["pdh_pdl_buffer_pct"] == 1.0
    assert out["min_bar_volume"] == 0.0
    assert out["min_turnover_inr"] == 50_000_000.0
    assert out["entry_pending_max_minutes"] == 120


def test_default_settings_has_regime_filters():
    d = default_scout_settings()
    assert d["index_trend_filter_enabled"] is True
    assert d["liquidity_filter_enabled"] is True
    assert d["entry_pending_max_minutes"] == 15


def test_effective_pattern_config_includes_regime_keys():
    from scout.settings_schema import effective_pattern_config

    cfg = effective_pattern_config({
        "index_trend_min_pct": -0.5,
        "min_bar_volume": 1000,
        "liquidity_filter_enabled": False,
    })
    assert cfg["index_trend_min_pct"] == -0.5
    assert cfg["min_bar_volume"] == 1000
    assert cfg["liquidity_filter_enabled"] is False


def test_default_settings_has_wallet_limits():
    d = default_scout_settings()
    assert d["wallet_utilization_pct"] == 90.0
    assert d["wallet_reserve_inr"] == 2000.0


def test_validate_clamps_wallet():
    out = validate_scout_settings({"wallet_utilization_pct": 120, "wallet_reserve_inr": -100})
    assert out["wallet_utilization_pct"] == 100.0
    assert out["wallet_reserve_inr"] == 0.0


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


def test_default_settings_has_zerodha_execution_fields():
    d = default_scout_settings()
    assert d["zerodha_execute_orders"] is False
    assert d["square_off_time"] == "15:10"
    assert d["square_off_warn_minutes"] == 5


def test_validate_persists_zerodha_execution_settings():
    out = validate_scout_settings({
        "zerodha_execute_orders": True,
        "square_off_time": "15:08",
        "square_off_warn_minutes": 3,
    })
    assert out["zerodha_execute_orders"] is True
    assert out["square_off_time"] == "15:08"
    assert out["square_off_warn_minutes"] == 3


def test_square_off_datetime_from_settings():
    day = datetime(2026, 8, 12, 11, 0, 0)
    dt = square_off_datetime(day, {"square_off_time": "15:10"})
    assert dt.hour == 15 and dt.minute == 10
    assert format_square_off_time({"square_off_time": "15:10"}) == "15:10 IST"


def test_merge_scout_settings_keeps_zerodha_flag():
    merged = merge_scout_settings({"zerodha_execute_orders": True})
    assert merged["zerodha_execute_orders"] is True
    assert merged["square_off_time"] == "15:10"
