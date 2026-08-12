"""Tests for Scout trade execution audit."""

from __future__ import annotations

import json

from scout.trade_audit import (
    build_entry_audit,
    build_entry_execution,
    build_exit_execution,
    enrich_history_trade,
)


def test_build_entry_audit_auto_json():
    sig = {
        "action": "SELL",
        "signal_type": "OR_BREAK_DOWN",
        "strength": "WEAK",
        "ltp": 100.0,
        "invalidation": 102.0,
        "triggered_at": "2026-08-12 10:00:00",
        "meta": {"stock_pct_from_open": -0.5, "nifty_pct_from_open": 0.1},
    }
    notes = build_entry_audit(
        sig,
        entry_price=100.5,
        executed_at=__import__("datetime").datetime(2026, 8, 12, 10, 1, 0),
        mode="auto",
        source="auto_execute",
    )
    data = json.loads(notes)
    assert data["mode"] == "auto"
    assert data["source"] == "auto_execute"
    assert data["fill"] == 100.5


def test_build_entry_execution_legacy_auto_notes():
    trade = {
        "notes": "auto_execute",
        "entry_price": 2500.0,
        "executed_at": "2026-08-12 10:00:00",
        "signal_type": "PULLBACK_DOWN",
        "signal_strength": "WEAK",
        "signal_reason": "Pullback short",
    }
    entry = build_entry_execution(trade)
    assert entry["mode"] == "auto"
    assert entry["mode_label"] == "Auto-enter"


def test_build_exit_execution_target_hit():
    trade = {
        "action": "BUY",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "exit_reason": "target_hit",
        "closed_at": "2026-08-12 11:00:00",
        "executed_at": "2026-08-12 10:00:00",
        "signal_type": "OR_BREAK_UP",
        "invalidation": 98.0,
    }
    exit_exec = build_exit_execution(trade)
    assert exit_exec["mode"] == "auto"
    assert exit_exec["mode_label"] == "Auto-close"
    assert exit_exec["trigger_label"] == "Target hit"
    assert any(c["label"] == "Target" for c in exit_exec["conditions"])


def test_enrich_history_trade_includes_execution():
    trade = {
        "id": 9,
        "symbol": "TCS",
        "action": "SELL",
        "signal_type": "OR_BREAK_DOWN",
        "entry_price": 4000.0,
        "exit_price": 3990.0,
        "exit_reason": "manual",
        "notes": '{"mode":"manual","source":"manual","fill":4000.0,"validity":"ACTIVE"}',
        "executed_at": "2026-08-12 10:00:00",
        "closed_at": "2026-08-12 11:00:00",
        "pnl": 10.0,
        "invalidation": 4010.0,
        "signal_ltp": 4000.0,
    }
    out = enrich_history_trade(trade)
    assert "execution" in out
    assert out["execution"]["entry"]["mode"] == "manual"
    assert out["execution"]["exit"]["mode"] == "manual"
