"""Tests for basis/basis_engine.py basis math and episode FSM."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from basis.basis_engine import BasisEngine, compute_basis
from providers.base import DataSource, LiveQuote


def test_compute_basis_contango():
    exp = date(2026, 9, 25)
    basis_abs, basis_pct, ann_pct, direction = compute_basis(100.0, 101.0, exp, as_of=date(2026, 8, 17))
    assert basis_abs == 1.0
    assert basis_pct == 1.0
    assert direction == "CONTANGO"
    dte = (exp - date(2026, 8, 17)).days
    assert ann_pct == pytest.approx(basis_pct * 365 / max(dte, 1), rel=1e-4)


def test_compute_basis_backwardation():
    exp = date(2026, 9, 25)
    basis_abs, basis_pct, ann_pct, direction = compute_basis(100.0, 99.0, exp, as_of=date(2026, 8, 17))
    assert basis_abs == -1.0
    assert basis_pct == -1.0
    assert direction == "BACKWARDATION"
    assert ann_pct < 0


def test_compute_basis_annualized_uses_min_dte_one():
    exp = date(2026, 8, 17)
    _, basis_pct, ann_pct, _ = compute_basis(100.0, 100.5, exp, as_of=date(2026, 8, 17))
    assert ann_pct == pytest.approx(basis_pct * 365 / 1, rel=1e-4)


def test_compute_basis_flat():
    exp = date(2026, 9, 25)
    basis_abs, basis_pct, _, direction = compute_basis(100.0, 100.0, exp)
    assert basis_abs == 0.0
    assert basis_pct == 0.0
    assert direction == "FLAT"


def _quote(symbol, exchange, ltp, ts, expiry=None):
    return LiveQuote(
        symbol=symbol,
        expiry=expiry,
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


def test_basis_engine_opens_with_string_expiry_lookup(mocker):
    mocker.patch("basis.basis_engine.get_basis_settings", return_value={
        "enabled": True,
        "tick_staleness_sec": 3,
        "min_basis_store_pct": 0,
        "min_duration_store_sec": 0,
    })
    mocker.patch("basis.basis_engine.BASIS_CONFIG", {
        "enabled": True,
        "db_flush_interval_sec": 60,
        "leg_stale_close_sec": 5,
    })
    db = MagicMock()
    engine = BasisEngine(
        db=db,
        event_bus=MagicMock(),
        expiry_lookup=lambda _s: "2026-09-25",
        clock=lambda: datetime(2026, 8, 14, 10, 0, 0),
    )
    now = datetime(2026, 8, 14, 10, 0, 0)
    engine._on_tick(_quote("RELIANCE", "NSE", 100.0, now))
    engine._on_tick(_quote("RELIANCE", "NFO", 101.0, now))
    assert len(engine.live_basis()) == 1


def test_basis_engine_opens_episode_on_paired_ticks(mocker):
    mocker.patch("basis.basis_engine.get_basis_settings", return_value={
        "enabled": True,
        "tick_staleness_sec": 3,
        "min_basis_store_pct": 0,
        "min_duration_store_sec": 0,
    })
    mocker.patch("basis.basis_engine.BASIS_CONFIG", {
        "enabled": True,
        "db_flush_interval_sec": 60,
        "leg_stale_close_sec": 5,
    })
    db = MagicMock()
    exp = date(2026, 9, 25)
    engine = BasisEngine(
        db=db,
        event_bus=MagicMock(),
        expiry_lookup=lambda _s: exp,
        clock=lambda: datetime(2026, 8, 14, 10, 0, 0),
    )
    now = datetime(2026, 8, 14, 10, 0, 0)

    engine._on_tick(_quote("RELIANCE", "NSE", 100.0, now))
    engine._on_tick(_quote("RELIANCE", "NFO", 101.0, now, expiry=exp))

    live = engine.live_basis()
    assert len(live) == 1
    assert live[0]["symbol"] == "RELIANCE"
    assert live[0]["basis_pct"] == 1.0
    assert live[0]["direction"] == "CONTANGO"


def test_basis_engine_closes_on_flat_basis(mocker):
    mocker.patch("basis.basis_engine.get_basis_settings", return_value={
        "enabled": True,
        "tick_staleness_sec": 3,
        "min_basis_store_pct": 0,
        "min_duration_store_sec": 0,
    })
    mocker.patch("basis.basis_engine.BASIS_CONFIG", {
        "enabled": True,
        "db_flush_interval_sec": 60,
        "leg_stale_close_sec": 5,
    })
    db = MagicMock()
    exp = date(2026, 9, 25)
    t0 = datetime(2026, 8, 14, 10, 0, 0)
    t1 = t0 + timedelta(seconds=1)
    clock = MagicMock(side_effect=[t0, t0, t1, t1])
    engine = BasisEngine(db=db, event_bus=MagicMock(), expiry_lookup=lambda _s: exp, clock=clock)
    engine._on_tick(_quote("TCS", "NSE", 100.0, t0))
    engine._on_tick(_quote("TCS", "NFO", 100.5, t0, expiry=exp))
    assert len(engine.live_basis()) == 1

    engine._on_tick(_quote("TCS", "NSE", 100.0, t1))
    engine._on_tick(_quote("TCS", "NFO", 100.0, t1, expiry=exp))
    assert len(engine.live_basis()) == 0
