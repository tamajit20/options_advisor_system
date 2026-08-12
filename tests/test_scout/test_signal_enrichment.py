"""Tests for scout signal enrichment and live quotes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scout.live_quotes import latest_equity_ltps
from scout.signal_enrichment import (
    build_exit_plan,
    enrich_signal,
    evaluate_signal_status,
    scout_trade_mtm,
)


def _sample_signal(**overrides):
    base = {
        "id": 1,
        "symbol": "RELIANCE",
        "action": "BUY",
        "ltp": 100.0,
        "invalidation": 98.0,
        "signal_type": "OR_BREAK_UP",
        "reason": "OR break with RS",
        "triggered_at": datetime(2026, 7, 30, 10, 0, 0),
        "meta": {
            "stock_pct_from_open": 0.5,
            "nifty_pct_from_open": 0.2,
            "or_high": 101,
            "or_low": 99,
        },
    }
    base.update(overrides)
    return base


def test_enrich_signal_adds_entry_band_and_conditions():
    now = datetime(2026, 7, 30, 10, 5, 0)
    out = enrich_signal(_sample_signal(), live_ltp=100.1, now=now)
    assert out["entry_min"] < 100.0 < out["entry_max"]
    assert out["validity_status"] == "ACTIVE"
    assert out["is_actionable"] is True
    assert len(out["conditions"]) >= 3
    assert isinstance(out["conditions"][0], dict)
    assert "label" in out["conditions"][0]
    assert "value" in out["conditions"][0]
    assert out["live_ltp"] == 100.1
    dash = out.get("dashboard") or {}
    assert dash.get("setup_code") == "OR ↑"
    assert dash.get("prices", {}).get("trigger") == 100.0
    assert len(dash.get("stats") or []) >= 2
    assert dash.get("gates") is not None


def test_expired_signal_status():
    triggered = datetime(2026, 7, 30, 9, 0, 0)
    now = triggered + timedelta(minutes=45)
    sig = _sample_signal(triggered_at=triggered)
    assert evaluate_signal_status(sig, live_ltp=100.0, now=now) == "EXPIRED"


def test_invalidated_buy_below_stop():
    sig = _sample_signal(action="BUY", invalidation=98.0)
    assert evaluate_signal_status(sig, live_ltp=97.5, now=datetime(2026, 7, 30, 10, 5, 0)) == "INVALIDATED"


def test_out_of_range_when_price_chases_too_far():
    sig = _sample_signal(ltp=100.0, action="BUY")
    assert evaluate_signal_status(sig, live_ltp=105.0, now=datetime(2026, 7, 30, 10, 5, 0)) == "OUT_OF_RANGE"


def test_scout_trade_mtm_buy():
    mtm = scout_trade_mtm({"action": "BUY", "entry_price": 100, "quantity": 10}, 102.0)
    assert mtm["mtm"] == 20.0
    assert mtm["mtm_pct"] == 2.0


def test_build_exit_plan_buy_or_break():
    sig = _sample_signal(action="BUY", ltp=100.0, invalidation=98.0)
    now = datetime(2026, 7, 30, 11, 0, 0)
    plan = build_exit_plan(sig, entry_price=100.0, live_ltp=101.0, now=now)
    assert plan["stop_price"] == 98.0
    assert plan["target_price"] == 103.0  # 1.5R with risk=2
    assert plan["structural_target"] == 103.0  # or_high + span = 101 + 2
    assert plan["square_off_by"] == "15:15 IST"
    assert plan["dashboard"]["target_dist"]["rs"] == 2.0
    assert plan["dashboard"]["stop_dist"]["rs"] == 3.0
    assert any(c["id"] == "exit_time" for c in plan["conditions"])


def test_build_exit_plan_sell_uses_negative_target():
    sig = _sample_signal(
        action="SELL",
        ltp=100.0,
        invalidation=102.0,
        signal_type="OR_BREAK_DOWN",
    )
    plan = build_exit_plan(sig, entry_price=100.0, now=datetime(2026, 7, 30, 11, 0, 0))
    assert plan["target_price"] == 97.0
    assert plan["stop_side"] == "above"


def test_latest_equity_ltps_from_ws_status(tmp_path, monkeypatch):
    snap = {
        "recent_events": [
            {"ts": "2026-07-30T10:00:00", "topic": "tick", "symbol": "RELIANCE", "last_price": 2500.5},
            {"ts": "2026-07-30T10:00:01", "topic": "tick", "symbol": "NIFTY", "last_price": 24000},
            {
                "ts": "2026-07-30T10:00:02",
                "topic": "tick",
                "symbol": "RELIANCE",
                "option_type": "CE",
                "strike": 2500,
                "last_price": 50,
            },
        ],
    }
    path = tmp_path / "ws_status.json"
    path.write_text(json.dumps(snap), encoding="utf-8")

    def _fake_path():
        return path

    monkeypatch.setattr("scout.live_quotes._snapshot_path", _fake_path)
    quotes = latest_equity_ltps(["RELIANCE"])
    assert quotes["RELIANCE"]["ltp"] == 2500.5
