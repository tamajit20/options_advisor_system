"""Tests for scout/auto_enter_status.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from scout.auto_enter_status import evaluate_auto_enter_status
from scout.settings_schema import default_scout_settings


def _signal(**kw):
    base = {
        "id": 42,
        "symbol": "RELIANCE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "strength": "HIGH",
        "ltp": 100.0,
        "invalidation": 98.0,
        "meta": {"or_high": 101.0, "or_low": 99.0},
    }
    base.update(kw)
    return base


def _enriched(**kw):
    base = {
        "validity_status": "ACTIVE",
        "entry_min": 99.8,
        "entry_max": 100.2,
        "live_ltp": 100.0,
    }
    base.update(kw)
    return base


def test_auto_enter_status_ready_when_all_pass():
    trade_repo = MagicMock()
    trade_repo.count_trades_opened_today.return_value = 1
    trade_repo.symbol_has_trade_today.return_value = False
    trade_repo.open_trades.return_value = []

    settings = default_scout_settings()
    settings["auto_execute_signals"] = True
    settings["min_net_profit_inr"] = 10.0
    settings["trade_window_start"] = "00:00"
    settings["trade_window_end"] = "23:59"

    out = evaluate_auto_enter_status(
        signal=_signal(),
        enriched=_enriched(),
        settings=settings,
        trade_repo=trade_repo,
        market_open=True,
    )
    assert out["enabled"] is True
    assert out["ready"] is True
    assert out["block_reason"] is None
    ids = [c["id"] for c in out["checks"]]
    assert "profit" in ids
    assert "pattern" in ids


def test_auto_enter_status_uses_limit_price_for_profit():
    """Profit gate must use conservative limit (entry_max for BUY), not LTP."""
    trade_repo = MagicMock()
    trade_repo.count_trades_opened_today.return_value = 0
    trade_repo.symbol_has_trade_today.return_value = False
    trade_repo.open_trades.return_value = []

    settings = default_scout_settings()
    settings["auto_execute_signals"] = True
    settings["min_net_profit_inr"] = 5000.0
    settings["min_risk_pct"] = 0.0
    settings["trade_window_start"] = "00:00"
    settings["trade_window_end"] = "23:59"

    out = evaluate_auto_enter_status(
        signal=_signal(),
        enriched=_enriched(live_ltp=100.0, entry_max=100.5),
        settings=settings,
        trade_repo=trade_repo,
        market_open=True,
    )
    profit = next(c for c in out["checks"] if c["id"] == "profit")
    assert profit["ok"] is False
    # Target derived from limit entry ₹100.50 (2.5R → ~106.75), not LTP-only ₹100.00 (~104.50)
    assert "106.75" in profit["detail"]


def test_auto_enter_status_blocks_weak_strength():
    trade_repo = MagicMock()
    trade_repo.count_trades_opened_today.return_value = 0
    trade_repo.symbol_has_trade_today.return_value = False
    trade_repo.open_trades.return_value = []

    settings = default_scout_settings()
    settings["auto_execute_signals"] = True

    out = evaluate_auto_enter_status(
        signal=_signal(strength="MEDIUM"),
        enriched=_enriched(),
        settings=settings,
        trade_repo=trade_repo,
        market_open=True,
    )
    strength = next(c for c in out["checks"] if c["id"] == "strength")
    assert strength["ok"] is False
    assert out["ready"] is False
