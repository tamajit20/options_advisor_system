"""Pattern detection — returns scout signal dicts or None."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from scout.candles import (
    Candle,
    bearish_bar,
    bullish_bar,
    higher_lows,
    last_n,
    lower_highs,
    range_high_low,
)
from scout.filters import passes_anti_chase, relative_strength_ok, _cfg
from scout.utils import pct_change


@dataclass
class ScoutSignal:
    action: str  # BUY | SELL
    signal_type: str
    reason: str
    ltp: float
    invalidation: Optional[float]
    strength: str  # WEAK | MEDIUM | HIGH
    meta: dict

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "signal_type": self.signal_type,
            "reason": self.reason,
            "ltp": self.ltp,
            "invalidation": self.invalidation,
            "strength": self.strength,
            "meta": self.meta,
        }


def _strength(base: str, rs_ok: bool, volume_ok: bool, *, strong: bool = False) -> str:
    score = int(rs_ok) + int(volume_ok) + int(strong)
    if score >= 3:
        return "HIGH"
    if score >= 2:
        return "MEDIUM"
    return base if base in ("WEAK", "MEDIUM", "HIGH") else "WEAK"


def detect_opening_range_break(
    candles: Sequence[Candle],
    *,
    open_px: float,
    day_high: float,
    day_low: float,
    stock_pct: float,
    bench_pct: float,
    cfg: Optional[dict] = None,
) -> Optional[ScoutSignal]:
    c = _cfg(cfg)
    or_bars = int(c.get("or_minutes", 15))
    if len(candles) < or_bars + 2:
        return None
    or_slice = list(candles[:or_bars])
    or_high, or_low = range_high_low(or_slice)
    if or_high <= or_low:
        return None
    last = candles[-1]
    ltp = last.close

    ok, why = passes_anti_chase(open_px=open_px, ltp=ltp, day_high=day_high, day_low=day_low, cfg=c)
    if not ok:
        return None

    if ltp > or_high and last.close > last.open:
        rs = relative_strength_ok(stock_pct, bench_pct, "BUY", cfg=c)
        if not rs:
            return None
        vol_ok = last.volume > 0
        return ScoutSignal(
            action="BUY",
            signal_type="OR_BREAK_UP",
            reason=f"Price broke above {or_bars}m opening range high (₹{or_high:.2f})",
            ltp=ltp,
            invalidation=or_low,
            strength=_strength("WEAK", rs, vol_ok),
            meta={"or_high": or_high, "or_low": or_low},
        )

    if ltp < or_low and last.close < last.open:
        rs = relative_strength_ok(stock_pct, bench_pct, "SELL", cfg=c)
        if not rs:
            return None
        vol_ok = last.volume > 0
        return ScoutSignal(
            action="SELL",
            signal_type="OR_BREAK_DOWN",
            reason=f"Price broke below {or_bars}m opening range low (₹{or_low:.2f})",
            ltp=ltp,
            invalidation=or_high,
            strength=_strength("WEAK", rs, vol_ok),
            meta={"or_high": or_high, "or_low": or_low},
        )
    return None


def detect_compression_break(
    candles: Sequence[Candle],
    *,
    open_px: float,
    day_high: float,
    day_low: float,
    stock_pct: float,
    bench_pct: float,
    cfg: Optional[dict] = None,
) -> Optional[ScoutSignal]:
    c = _cfg(cfg)
    n = int(c.get("compression_bars", 10))
    max_range_pct = float(c.get("compression_range_pct", 0.35))
    if len(candles) < n + 2:
        return None
    box = last_n(candles, n + 1)[:-1]
    last = candles[-1]
    box_high, box_low = range_high_low(box)
    mid = (box_high + box_low) / 2.0
    if mid <= 0:
        return None
    box_range_pct = (box_high - box_low) / mid * 100.0
    if box_range_pct > max_range_pct:
        return None

    ltp = last.close
    ok, _ = passes_anti_chase(open_px=open_px, ltp=ltp, day_high=day_high, day_low=day_low, cfg=c)
    if not ok:
        return None

    vol_ok = last.volume > 0 and (
        sum(c.volume for c in box) / max(len(box), 1) * 1.2 <= last.volume
    )
    tight = box_range_pct <= max_range_pct * 0.75

    if ltp > box_high and bullish_bar(last):
        rs = relative_strength_ok(stock_pct, bench_pct, "BUY", cfg=c)
        if not rs:
            return None
        return ScoutSignal(
            action="BUY",
            signal_type="RANGE_BREAK_UP",
            reason=f"Tight {n}m range break up (range {box_range_pct:.2f}%)",
            ltp=ltp,
            invalidation=box_low,
            strength=_strength("WEAK", rs, vol_ok, strong=tight),
            meta={"box_high": box_high, "box_low": box_low, "range_pct": box_range_pct},
        )

    if ltp < box_low and bearish_bar(last):
        rs = relative_strength_ok(stock_pct, bench_pct, "SELL", cfg=c)
        if not rs:
            return None
        return ScoutSignal(
            action="SELL",
            signal_type="RANGE_BREAK_DOWN",
            reason=f"Tight {n}m range break down (range {box_range_pct:.2f}%)",
            ltp=ltp,
            invalidation=box_high,
            strength=_strength("WEAK", rs, vol_ok, strong=tight),
            meta={"box_high": box_high, "box_low": box_low, "range_pct": box_range_pct},
        )
    return None


def detect_pullback(
    candles: Sequence[Candle],
    *,
    open_px: float,
    day_high: float,
    day_low: float,
    stock_pct: float,
    bench_pct: float,
    cfg: Optional[dict] = None,
) -> Optional[ScoutSignal]:
    c = _cfg(cfg)
    if len(candles) < 8:
        return None
    last = candles[-1]
    ltp = last.close
    ok, _ = passes_anti_chase(open_px=open_px, ltp=ltp, day_high=day_high, day_low=day_low, cfg=c)
    if not ok:
        return None

    up_ctx = higher_lows(candles[-6:-1], count=3)
    dn_ctx = lower_highs(candles[-6:-1], count=3)

    if up_ctx and bullish_bar(last) and last.low <= candles[-2].low * 1.002:
        rs = relative_strength_ok(stock_pct, bench_pct, "BUY", cfg=c)
        if not rs:
            return None
        inv = min(c.low for c in candles[-4:])
        return ScoutSignal(
            action="BUY",
            signal_type="PULLBACK_UP",
            reason="Uptrend pullback — bullish 1m reversal candle",
            ltp=ltp,
            invalidation=inv,
            strength=_strength("WEAK", rs, last.volume > 0),
            meta={"move_from_open_pct": pct_change(open_px, ltp)},
        )

    if dn_ctx and bearish_bar(last) and last.high >= candles[-2].high * 0.998:
        rs = relative_strength_ok(stock_pct, bench_pct, "SELL", cfg=c)
        if not rs:
            return None
        inv = max(c.high for c in candles[-4:])
        return ScoutSignal(
            action="SELL",
            signal_type="PULLBACK_DOWN",
            reason="Downtrend pullback — bearish 1m reversal candle",
            ltp=ltp,
            invalidation=inv,
            strength=_strength("WEAK", rs, last.volume > 0),
            meta={"move_from_open_pct": pct_change(open_px, ltp)},
        )
    return None


def detect_signals(
    candles: Sequence[Candle],
    *,
    open_px: float,
    day_high: float,
    day_low: float,
    stock_pct: float,
    bench_pct: float,
    cfg: Optional[dict] = None,
) -> List[ScoutSignal]:
    """Run all v1 detectors; return at most one signal (highest priority first)."""
    detectors = (
        detect_opening_range_break,
        detect_compression_break,
        detect_pullback,
    )
    for fn in detectors:
        sig = fn(
            candles,
            open_px=open_px,
            day_high=day_high,
            day_low=day_low,
            stock_pct=stock_pct,
            bench_pct=bench_pct,
            cfg=cfg,
        )
        if sig:
            return [sig]
    return []
