"""Tests for scout/profit_gate.py and cost-aware exits."""

from __future__ import annotations

from datetime import datetime

from scout.profit_gate import entry_profit_block_reason, signal_type_allowed
from scout.signal_enrichment import build_exit_plan, evaluate_exit_alerts


def _or_signal(**overrides):
    base = {
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "ltp": 100.0,
        "invalidation": 98.0,
        "meta": {"or_high": 101.0, "or_low": 99.0},
    }
    base.update(overrides)
    return base


def test_signal_type_allowed_respects_settings():
    s = {"auto_enter_signal_types": ["OR_BREAK_UP"]}
    assert signal_type_allowed(s, "OR_BREAK_UP")
    assert not signal_type_allowed(s, "PULLBACK_UP")


def test_entry_profit_gate_blocks_tiny_edge():
    # Tight stop → 2R target too small after charges
    sig = _or_signal(invalidation=99.5, ltp=100.0, meta={"or_high": 100.2, "or_low": 99.5})
    reason = entry_profit_block_reason(
        signal=sig,
        entry=100.0,
        qty=1,
        settings={"min_net_profit_inr": 100.0, "min_target_r": 2.0},
    )
    assert reason is not None
    assert "expected net" in reason


def test_entry_profit_gate_allows_wide_or_break():
    sig = _or_signal()
    reason = entry_profit_block_reason(
        signal=sig,
        entry=100.0,
        qty=20,
        settings={"min_net_profit_inr": 50.0, "min_target_r": 2.0},
    )
    assert reason is None


def test_breakeven_stop_after_1r():
    sig = _or_signal()
    plan = build_exit_plan(sig, entry_price=100.0, now=datetime(2026, 7, 30, 11, 0, 0))
    # At 1R (102), stop should move to breakeven — LTP 99.5 triggers stop
    alerts = evaluate_exit_alerts(
        action="BUY",
        live_ltp=99.5,
        exit_plan=plan,
        entry_price=100.0,
        peak_price=102.5,
        settings={"breakeven_at_r": 1.0, "trail_stop_r_fraction": 0.5},
    )
    assert alerts["flags"]["stop_hit"] is True
    assert alerts["flags"]["breakeven_armed"] is True
