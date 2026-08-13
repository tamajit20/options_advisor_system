"""Tests for scout pattern detection (no DB, no Zerodha)."""

from __future__ import annotations

from datetime import datetime, timedelta

from scout.candles import Candle
from scout.filters import passes_anti_chase
from scout.patterns import detect_compression_break, detect_opening_range_break


def _candle(i: int, o: float, h: float, l: float, c: float, vol: float = 1000) -> Candle:
    return Candle(
        ts=datetime(2026, 4, 7, 9, 15) + timedelta(minutes=i),
        open=o, high=h, low=l, close=c, volume=vol,
    )


def test_anti_chase_rejects_large_move():
    ok, _ = passes_anti_chase(open_px=100, ltp=102, day_high=102, day_low=99)
    assert ok is False


def test_or_break_up_signal():
    candles = []
    for i in range(15):
        candles.append(_candle(i, 100, 100.2, 99.8, 100, 800))
    candles.append(_candle(15, 100.1, 100.5, 100.0, 100.45, 2500))
    candles.append(_candle(16, 100.45, 100.55, 100.4, 100.5, 2500))
    sig = detect_opening_range_break(
        candles,
        open_px=100,
        day_high=100.55,
        day_low=99.8,
        stock_pct=0.5,
        bench_pct=0.1,
    )
    assert sig is not None
    assert sig.action == "BUY"
    assert sig.signal_type == "OR_BREAK_UP"


def test_compression_break_down():
    candles = []
    base = 200.0
    for i in range(11):
        candles.append(_candle(i, base, base + 0.1, base - 0.1, base, 900))
    candles.append(_candle(11, base, base + 0.05, base - 0.35, base - 0.3, 2000))
    sig = detect_compression_break(
        candles,
        open_px=base,
        day_high=base + 0.1,
        day_low=base - 0.35,
        stock_pct=-0.15,
        bench_pct=0.0,
    )
    assert sig is not None
    assert sig.action == "SELL"
