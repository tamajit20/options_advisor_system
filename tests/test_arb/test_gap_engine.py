"""Tests for arb/gap_engine.py gap math and episode FSM."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from arb.gap_engine import ArbGapEngine, compute_gap, _LegSnapshot
from providers.base import DataSource, LiveQuote


def test_compute_gap_nse_high():
    gap_abs, gap_pct, direction = compute_gap(101.0, 100.0)
    assert gap_abs == 1.0
    assert gap_pct == 1.0
    assert direction == "NSE_HIGH"


def test_compute_gap_bse_high():
    gap_abs, gap_pct, direction = compute_gap(100.0, 101.0)
    assert gap_abs == -1.0
    assert gap_pct == pytest.approx(1.0)
    assert direction == "BSE_HIGH"


def test_compute_gap_flat():
    gap_abs, gap_pct, direction = compute_gap(100.0, 100.0)
    assert gap_abs == 0.0
    assert direction == "FLAT"


def _quote(symbol, exchange, ltp, ts):
    return LiveQuote(
        symbol=symbol,
        expiry=None,
        strike=None,
        option_type=None,
        last_price=ltp,
        bid=ltp - 0.05,
        ask=ltp + 0.05,
        bid_qty=10,
        ask_qty=12,
        exchange=exchange,
        timestamp=ts,
        source=DataSource.LIVE,
        provider="test",
    )


def test_gap_engine_opens_episode_on_paired_ticks(mocker):
    mocker.patch("arb.gap_engine.ARB_CONFIG", {
        "enabled": True,
        "tick_staleness_sec": 3,
        "leg_stale_close_sec": 5,
        "db_flush_interval_sec": 60,
    })
    db = MagicMock()
    engine = ArbGapEngine(db=db, event_bus=MagicMock(), clock=lambda: datetime(2026, 8, 14, 10, 0, 0))
    now = datetime(2026, 8, 14, 10, 0, 0)

    engine._on_tick(_quote("RELIANCE", "NSE", 101.0, now))
    engine._on_tick(_quote("RELIANCE", "BSE", 100.0, now))

    live = engine.live_gaps()
    assert len(live) == 1
    assert live[0]["symbol"] == "RELIANCE"
    assert live[0]["gap_pct"] == 1.0
    assert live[0]["direction"] == "NSE_HIGH"


def test_gap_engine_closes_on_zero_gap(mocker):
    mocker.patch("arb.gap_engine.ARB_CONFIG", {
        "enabled": True,
        "tick_staleness_sec": 3,
        "leg_stale_close_sec": 5,
        "db_flush_interval_sec": 60,
    })
    db = MagicMock()
    t0 = datetime(2026, 8, 14, 10, 0, 0)
    t1 = t0 + timedelta(seconds=1)
    clock = MagicMock(side_effect=[t0, t0, t1, t1])
    engine = ArbGapEngine(db=db, event_bus=MagicMock(), clock=clock)
    engine._on_tick(_quote("TCS", "NSE", 100.5, t0))
    engine._on_tick(_quote("TCS", "BSE", 100.0, t0))
    assert len(engine.live_gaps()) == 1

    engine._on_tick(_quote("TCS", "NSE", 100.0, t1))
    engine._on_tick(_quote("TCS", "BSE", 100.0, t1))
    assert len(engine.live_gaps()) == 0


def test_gap_engine_direction_flip_starts_new_episode(mocker):
    mocker.patch("arb.gap_engine.ARB_CONFIG", {
        "enabled": True,
        "tick_staleness_sec": 3,
        "leg_stale_close_sec": 5,
        "db_flush_interval_sec": 60,
    })
    db = MagicMock()
    t0 = datetime(2026, 8, 14, 10, 0, 0)
    engine = ArbGapEngine(db=db, event_bus=MagicMock(), clock=lambda: t0)
    engine._on_tick(_quote("INFY", "NSE", 101.0, t0))
    engine._on_tick(_quote("INFY", "BSE", 100.0, t0))
    assert engine.live_gaps()[0]["direction"] == "NSE_HIGH"

    engine._on_tick(_quote("INFY", "NSE", 99.0, t0))
    engine._on_tick(_quote("INFY", "BSE", 100.0, t0))
    assert engine.live_gaps()[0]["direction"] == "BSE_HIGH"


def test_gap_engine_skips_db_when_below_min_gap_store_pct(mocker):
    mocker.patch("arb.gap_engine.ARB_CONFIG", {
        "enabled": True,
        "tick_staleness_sec": 3,
        "leg_stale_close_sec": 5,
        "db_flush_interval_sec": 60,
        "min_gap_store_pct": 0.5,
        "min_duration_store_sec": 0,
    })
    db = MagicMock()
    t0 = datetime(2026, 8, 14, 10, 0, 0)
    t1 = t0 + timedelta(seconds=2)
    clock = MagicMock(side_effect=[t0, t0, t1, t1])
    engine = ArbGapEngine(db=db, event_bus=MagicMock(), clock=clock)
    engine._gap_repo = MagicMock()
    engine._gap_repo.insert_open.return_value = 99

    # 0.4% gap — live yes, DB no (min store 0.5%)
    engine._on_tick(_quote("BPCL", "NSE", 100.4, t0))
    engine._on_tick(_quote("BPCL", "BSE", 100.0, t0))
    assert len(engine.live_gaps()) == 1
    engine._flush_pending()
    engine._gap_repo.insert_open.assert_not_called()

    # Close — still no DB row (max 0.5% < threshold if we used 0.5 min... 0.5% is exactly 0.5)
    engine._on_tick(_quote("BPCL", "NSE", 100.0, t1))
    engine._on_tick(_quote("BPCL", "BSE", 100.0, t1))
    engine._flush_pending()
    engine._gap_repo.insert_open.assert_not_called()


def test_gap_engine_persists_when_gap_meets_min_store_pct(mocker):
    mocker.patch("arb.gap_engine.ARB_CONFIG", {
        "enabled": True,
        "tick_staleness_sec": 3,
        "leg_stale_close_sec": 5,
        "db_flush_interval_sec": 60,
        "min_gap_store_pct": 0.5,
        "min_duration_store_sec": 0,
    })
    db = MagicMock()
    t0 = datetime(2026, 8, 14, 10, 0, 0)
    engine = ArbGapEngine(db=db, event_bus=MagicMock(), clock=lambda: t0)
    engine._gap_repo = MagicMock()
    engine._gap_repo.insert_open.return_value = 42

    engine._on_tick(_quote("RELIANCE", "NSE", 101.0, t0))
    engine._on_tick(_quote("RELIANCE", "BSE", 100.0, t0))
    engine._flush_pending()
    engine._gap_repo.insert_open.assert_called_once()
