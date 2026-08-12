"""Anti-chase and liquidity filters for scout signals."""

from __future__ import annotations

from typing import Optional

from config import SCOUT_CONFIG

from scout.candles import Candle
from scout.utils import pct_change


def _cfg(cfg: Optional[dict] = None) -> dict:
    return cfg if cfg is not None else SCOUT_CONFIG


def passes_anti_chase(
    *,
    open_px: float,
    ltp: float,
    day_high: float,
    day_low: float,
    cfg: Optional[dict] = None,
) -> tuple[bool, str]:
    """Reject signals when the move already happened (chasing)."""
    c = _cfg(cfg)
    max_move = float(c.get("max_move_from_open_pct", 1.2))
    move = abs(pct_change(open_px, ltp))
    if move > max_move:
        return False, f"already moved {move:.1f}% from open (max {max_move}%)"

    late_spike = float(c.get("late_spike_from_extreme_pct", 0.8))
    if open_px > 0 and day_high > 0 and ltp >= day_high * 0.999:
        recent_up = pct_change(open_px, ltp)
        if recent_up > late_spike:
            return False, "at day high after large up move"

    if open_px > 0 and day_low > 0 and ltp <= day_low * 1.001:
        recent_dn = pct_change(open_px, ltp)
        if recent_dn < -late_spike:
            return False, "at day low after large down move"

    return True, ""


def relative_strength_ok(
    stock_pct_from_open: float,
    benchmark_pct_from_open: float,
    side: str,
    cfg: Optional[dict] = None,
) -> bool:
    """Soft filter: long should not lag index badly; short should not lead a rally."""
    margin = float(_cfg(cfg).get("rs_margin_pct", 0.15))
    if side == "BUY":
        return stock_pct_from_open >= benchmark_pct_from_open - margin
    if side == "SELL":
        return stock_pct_from_open <= benchmark_pct_from_open + margin
    return True


def min_candles_ok(candles: list[Candle], cfg: Optional[dict] = None) -> bool:
    return len(candles) >= int(_cfg(cfg).get("min_candles", 12))
