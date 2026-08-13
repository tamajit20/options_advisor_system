"""Tests for scout liquidity filter."""

from __future__ import annotations

from datetime import datetime, timedelta

from scout.candles import Candle
from scout.filters import passes_liquidity


def _c(i: int, vol: float, close: float = 100.0) -> Candle:
    return Candle(
        ts=datetime(2026, 4, 7, 9, 15) + timedelta(minutes=i),
        open=close, high=close + 0.2, low=close - 0.2, close=close, volume=vol,
    )


def _cfg(**kw):
    base = {
        "liquidity_filter_enabled": True,
        "min_bar_volume": 500,
        "min_volume_vs_avg": 0.8,
        "min_turnover_inr": 50_000,
        "liquidity_lookback_bars": 10,
    }
    base.update(kw)
    return base


def test_liquidity_rejects_thin_bar():
    candles = [_c(i, 800) for i in range(10)]
    candles.append(_c(10, 100))
    ok, msg = passes_liquidity(candles, 100.0, cfg=_cfg())
    assert ok is False
    assert "volume" in msg


def test_liquidity_rejects_low_vs_average():
    candles = [_c(i, 2000) for i in range(10)]
    candles.append(_c(10, 900))
    ok, msg = passes_liquidity(candles, 100.0, cfg=_cfg(min_volume_vs_avg=0.8))
    assert ok is False
    assert "avg" in msg


def test_liquidity_rejects_low_turnover():
    candles = [_c(i, 800) for i in range(10)]
    candles.append(_c(10, 900, close=50.0))
    ok, msg = passes_liquidity(candles, 50.0, cfg=_cfg(min_turnover_inr=50_000))
    assert ok is False
    assert "turnover" in msg


def test_liquidity_allows_active_bar():
    candles = [_c(i, 800) for i in range(10)]
    candles.append(_c(10, 3000))
    ok, _ = passes_liquidity(candles, 100.0, cfg=_cfg())
    assert ok is True


def test_liquidity_disabled_skips_checks():
    ok, _ = passes_liquidity([], 100.0, cfg=_cfg(liquidity_filter_enabled=False))
    assert ok is True


def test_liquidity_rejects_empty_candles():
    ok, msg = passes_liquidity([], 100.0, cfg=_cfg())
    assert ok is False
    assert "no candles" in msg
