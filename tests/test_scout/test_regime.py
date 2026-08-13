"""Tests for scout/regime.py — index trend, PDH/PDL, failed breakout."""

from __future__ import annotations

import pytest

from scout.patterns import ScoutSignal
from scout.regime import (
    failed_breakout_detected,
    index_trend_allows,
    live_benchmark_pct,
    pdh_pdl_allows,
    signal_passes_regime,
)


@pytest.mark.parametrize(
    "side,bench,min_pct,max_pct,expected",
    [
        ("BUY", -0.5, -0.20, 0.20, False),
        ("BUY", -0.20, -0.20, 0.20, True),
        ("BUY", 0.10, -0.20, 0.20, True),
        ("SELL", 0.5, -0.20, 0.20, False),
        ("SELL", 0.20, -0.20, 0.20, True),
        ("SELL", -0.10, -0.20, 0.20, True),
    ],
)
def test_index_trend_boundaries(side, bench, min_pct, max_pct, expected):
    cfg = {"index_trend_filter_enabled": True, "index_trend_min_pct": min_pct, "index_trend_max_pct": max_pct}
    ok, _ = index_trend_allows(side, bench, cfg)
    assert ok is expected


def test_index_trend_disabled_passes_weak_nifty():
    ok, msg = index_trend_allows("BUY", -2.0, {"index_trend_filter_enabled": False})
    assert ok is True
    assert msg == ""


def test_pdh_allows_long_below_buffer():
    ok, _ = pdh_pdl_allows("BUY", 99.0, pdh=100.0, pdl=95.0, cfg={"pdh_pdl_buffer_pct": 0.15})
    assert ok is True


def test_pdl_blocks_short_near_prior_low():
    ok, msg = pdh_pdl_allows("SELL", 100.0, pdh=105.0, pdl=100.0, cfg={"pdh_pdl_buffer_pct": 0.15})
    assert ok is False
    assert "prior day low" in msg


def test_pdh_pdl_disabled():
    ok, _ = pdh_pdl_allows("BUY", 100.0, pdh=100.0, pdl=95.0, cfg={"pdh_pdl_filter_enabled": False})
    assert ok is True


@pytest.mark.parametrize(
    "signal_type,action,meta,ltp,expected",
    [
        ("OR_BREAK_UP", "BUY", {"or_high": 101.0, "or_low": 99.0}, 100.5, True),
        ("OR_BREAK_UP", "BUY", {"or_high": 101.0, "or_low": 99.0}, 101.0, False),
        ("OR_BREAK_DOWN", "SELL", {"or_high": 101.0, "or_low": 99.0}, 99.5, True),
        ("OR_BREAK_DOWN", "SELL", {"or_high": 101.0, "or_low": 99.0}, 99.0, False),
        ("RANGE_BREAK_UP", "BUY", {"box_high": 102.0, "box_low": 100.0}, 101.5, True),
        ("RANGE_BREAK_DOWN", "SELL", {"box_high": 102.0, "box_low": 100.0}, 100.5, True),
        ("PULLBACK_UP", "BUY", {"or_high": 101.0}, 100.0, False),
    ],
)
def test_failed_breakout_patterns(signal_type, action, meta, ltp, expected):
    sig = {"action": action, "signal_type": signal_type, "meta": meta}
    assert failed_breakout_detected(sig, ltp) is expected


def test_signal_passes_regime_blocks_weak_index():
    sig = ScoutSignal(
        action="BUY", signal_type="OR_BREAK_UP", reason="t", ltp=100.0,
        invalidation=99.0, strength="HIGH", meta={},
    )
    ok, msg = signal_passes_regime(
        sig, bench_pct=-0.8, pdh=110.0, pdl=90.0,
        cfg={"index_trend_filter_enabled": True, "index_trend_min_pct": -0.20},
    )
    assert ok is False
    assert "Nifty weak" in msg


def test_signal_passes_regime_blocks_pdh():
    sig = ScoutSignal(
        action="BUY", signal_type="OR_BREAK_UP", reason="t", ltp=100.0,
        invalidation=99.0, strength="HIGH", meta={},
    )
    ok, msg = signal_passes_regime(
        sig, bench_pct=0.2, pdh=100.0, pdl=90.0,
        cfg={"pdh_pdl_filter_enabled": True, "pdh_pdl_buffer_pct": 0.15},
    )
    assert ok is False
    assert "prior day high" in msg


def test_live_benchmark_pct_from_nifty_quote():
    meta = {"nifty_open": 24000.0, "nifty_pct_from_open": 0.5}
    pct = live_benchmark_pct(lambda s: 23880.0 if s == "NIFTY" else None, meta)
    assert pct == pytest.approx(-0.5, abs=0.01)


def test_live_benchmark_pct_falls_back_to_meta():
    meta = {"nifty_pct_from_open": 0.35}
    pct = live_benchmark_pct(lambda s: None, meta)
    assert pct == 0.35


def test_pdh_blocks_long_at_buffer_boundary():
    cfg = {"pdh_pdl_filter_enabled": True, "pdh_pdl_buffer_pct": 0.15}
    ok, msg = pdh_pdl_allows("BUY", 99.90, pdh=100.0, pdl=95.0, cfg=cfg)
    assert ok is False
    assert "prior day high" in msg


def test_pdh_pdl_allows_when_levels_missing():
    ok, msg = pdh_pdl_allows("BUY", 100.0, pdh=None, pdl=None)
    assert ok is True
    assert msg == ""


def test_signal_passes_regime_all_clear():
    sig = ScoutSignal(
        action="SELL", signal_type="OR_BREAK_DOWN", reason="t", ltp=99.0,
        invalidation=101.0, strength="HIGH", meta={},
    )
    ok, msg = signal_passes_regime(
        sig, bench_pct=0.1, pdh=110.0, pdl=90.0,
        cfg={
            "index_trend_filter_enabled": True,
            "index_trend_max_pct": 0.20,
            "pdh_pdl_filter_enabled": True,
            "pdh_pdl_buffer_pct": 0.15,
        },
    )
    assert ok is True
    assert msg == ""
