"""Integration tests — patterns blocked by regime and liquidity filters."""

from __future__ import annotations

from datetime import datetime, timedelta

from scout.candles import Candle
from scout.patterns import detect_opening_range_break, detect_signals


def _candle(i: int, o: float, h: float, l: float, c: float, vol: float = 2500) -> Candle:
    return Candle(
        ts=datetime(2026, 4, 7, 9, 15) + timedelta(minutes=i),
        open=o, high=h, low=l, close=c, volume=vol,
    )


def _or_break_candles():
    candles = []
    for i in range(15):
        candles.append(_candle(i, 100, 100.2, 99.8, 100, 800))
    candles.append(_candle(15, 100.1, 100.5, 100.0, 100.45, 2500))
    candles.append(_candle(16, 100.45, 100.55, 100.4, 100.5, 2500))
    return candles


def test_detect_signals_blocked_by_weak_nifty():
    out = detect_signals(
        _or_break_candles(),
        open_px=100,
        day_high=100.55,
        day_low=99.8,
        stock_pct=0.5,
        bench_pct=-0.6,
        pdh=110.0,
        pdl=90.0,
        cfg={"index_trend_filter_enabled": True, "index_trend_min_pct": -0.20},
    )
    assert out == []


def test_detect_signals_blocked_by_pdh():
    out = detect_signals(
        _or_break_candles(),
        open_px=100,
        day_high=100.55,
        day_low=99.8,
        stock_pct=0.5,
        bench_pct=0.1,
        pdh=100.5,
        pdl=90.0,
        cfg={"pdh_pdl_filter_enabled": True, "pdh_pdl_buffer_pct": 0.15},
    )
    assert out == []


def test_detect_signals_passes_with_clear_regime():
    out = detect_signals(
        _or_break_candles(),
        open_px=100,
        day_high=100.55,
        day_low=99.8,
        stock_pct=0.5,
        bench_pct=0.1,
        pdh=110.0,
        pdl=90.0,
        cfg={
            "index_trend_filter_enabled": True,
            "pdh_pdl_filter_enabled": True,
            "liquidity_filter_enabled": True,
            "min_turnover_inr": 50_000,
        },
    )
    assert len(out) == 1
    assert out[0].signal_type == "OR_BREAK_UP"


def test_or_break_blocked_by_liquidity():
    candles = []
    for i in range(15):
        candles.append(_candle(i, 100, 100.2, 99.8, 100, 800))
    candles.append(_candle(15, 100.1, 100.5, 100.0, 100.45, 100))
    candles.append(_candle(16, 100.45, 100.55, 100.4, 100.5, 100))
    sig = detect_opening_range_break(
        candles,
        open_px=100,
        day_high=100.55,
        day_low=99.8,
        stock_pct=0.5,
        bench_pct=0.1,
        cfg={
            "liquidity_filter_enabled": True,
            "min_bar_volume": 500,
            "min_volume_vs_avg": 0.8,
            "min_turnover_inr": 50_000,
        },
    )
    assert sig is None


def test_detect_signals_blocked_by_strong_nifty_short():
    candles = []
    for i in range(15):
        candles.append(_candle(i, 100, 100.2, 99.8, 100, 800))
    candles.append(_candle(15, 99.9, 100.0, 99.5, 99.6, 2500))
    candles.append(_candle(16, 99.6, 99.65, 99.4, 99.45, 2500))
    out = detect_signals(
        candles,
        open_px=100,
        day_high=100.2,
        day_low=99.4,
        stock_pct=-0.5,
        bench_pct=0.6,
        pdh=110.0,
        pdl=90.0,
        cfg={"index_trend_filter_enabled": True, "index_trend_max_pct": 0.20},
    )
    assert out == []


def test_detect_signals_blocked_by_pdl_for_short():
    candles = []
    for i in range(15):
        candles.append(_candle(i, 100, 100.2, 99.8, 100, 800))
    candles.append(_candle(15, 99.9, 100.0, 99.5, 99.6, 2500))
    candles.append(_candle(16, 99.6, 99.65, 99.4, 99.45, 2500))
    out = detect_signals(
        candles,
        open_px=100,
        day_high=100.2,
        day_low=99.4,
        stock_pct=-0.5,
        bench_pct=-0.1,
        pdh=110.0,
        pdl=99.5,
        cfg={"pdh_pdl_filter_enabled": True, "pdh_pdl_buffer_pct": 0.15},
    )
    assert out == []
