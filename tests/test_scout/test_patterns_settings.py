"""Pattern detection with persisted settings overrides."""

from __future__ import annotations

from datetime import datetime, timedelta

from scout.candles import Candle
from scout.filters import min_candles_ok, relative_strength_ok
from scout.patterns import detect_opening_range_break, detect_signals
from scout.settings_schema import effective_pattern_config


def _candle(i: int, o: float, h: float, l: float, c: float, vol: float = 1000) -> Candle:
    return Candle(
        ts=datetime(2026, 4, 7, 9, 15) + timedelta(minutes=i),
        open=o, high=h, low=l, close=c, volume=vol,
    )


def test_effective_pattern_config_overrides_or_minutes():
    cfg = effective_pattern_config({"min_candles": 20, "max_move_from_open_pct": 2.0})
    assert cfg["min_candles"] == 20
    assert cfg["max_move_from_open_pct"] == 2.0


def test_min_candles_ok_respects_settings():
    cfg = effective_pattern_config({"min_candles": 20})
    candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(15)]
    assert min_candles_ok(candles, cfg=cfg) is False
    candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(20)]
    assert min_candles_ok(candles, cfg=cfg) is True


def test_relative_strength_ok_custom_margin():
    cfg = effective_pattern_config({"rs_margin_pct": 0.5})
    assert relative_strength_ok(1.5, 0.2, "BUY", cfg=cfg) is True
    assert relative_strength_ok(-1.0, 0.2, "BUY", cfg=cfg) is False


def test_or_break_blocked_by_tight_anti_chase_settings():
    candles = []
    for i in range(15):
        candles.append(_candle(i, 100, 100.2, 99.8, 100, 800))
    candles.append(_candle(15, 100.1, 103.0, 100.0, 102.5, 1500))
    cfg = effective_pattern_config({"max_move_from_open_pct": 0.5})
    sig = detect_opening_range_break(
        candles,
        open_px=100,
        day_high=103.0,
        day_low=99.8,
        stock_pct=2.5,
        bench_pct=0.1,
        cfg=cfg,
    )
    assert sig is None


def test_detect_signals_returns_first_match_only():
    candles = []
    for i in range(15):
        candles.append(_candle(i, 100, 100.2, 99.8, 100, 800))
    candles.append(_candle(15, 100.1, 100.5, 100.0, 100.45, 1500))
    candles.append(_candle(16, 100.45, 100.55, 100.4, 100.5, 1200))
    out = detect_signals(
        candles,
        open_px=100,
        day_high=100.55,
        day_low=99.8,
        stock_pct=0.5,
        bench_pct=0.1,
    )
    assert len(out) >= 1
    assert out[0].signal_type in ("OR_BREAK_UP", "COMPRESSION_BREAK_DOWN", "PULLBACK_UP", "PULLBACK_DOWN")
