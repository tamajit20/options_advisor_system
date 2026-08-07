"""1-minute OHLCV bar utilities for scout pattern detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class Candle:
    ts: object  # datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def bars_from_kite(rows: Sequence[dict]) -> List[Candle]:
    out: List[Candle] = []
    for r in rows:
        out.append(Candle(
            ts=r.get("date"),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r.get("volume") or 0),
        ))
    return out


def last_n(candles: Sequence[Candle], n: int) -> List[Candle]:
    if n <= 0:
        return []
    return list(candles[-n:])


def range_high_low(candles: Sequence[Candle]) -> tuple[float, float]:
    if not candles:
        return 0.0, 0.0
    return max(c.high for c in candles), min(c.low for c in candles)


def higher_lows(candles: Sequence[Candle], count: int = 3) -> bool:
    if len(candles) < count:
        return False
    tail = list(candles[-count:])
    for i in range(1, len(tail)):
        if tail[i].low < tail[i - 1].low * 0.999:
            return False
    return True


def lower_highs(candles: Sequence[Candle], count: int = 3) -> bool:
    if len(candles) < count:
        return False
    tail = list(candles[-count:])
    for i in range(1, len(tail)):
        if tail[i].high > tail[i - 1].high * 1.001:
            return False
    return True


def bullish_bar(c: Candle) -> bool:
    return c.close > c.open


def bearish_bar(c: Candle) -> bool:
    return c.close < c.open
