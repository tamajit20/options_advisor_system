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


def passes_liquidity(
    candles: list[Candle],
    ltp: float,
    cfg: Optional[dict] = None,
) -> tuple[bool, str]:
    """Min bar volume, vs recent average, and notional turnover on the signal bar."""
    c = _cfg(cfg)
    if not c.get("liquidity_filter_enabled", True):
        return True, ""
    if not candles:
        return False, "no candles for liquidity check"
    last = candles[-1]
    px = float(ltp or last.close or 0)
    if px <= 0:
        return False, "no price for liquidity check"

    min_vol = float(c.get("min_bar_volume", 500))
    min_vs_avg = float(c.get("min_volume_vs_avg", 0.8))
    min_turnover = float(c.get("min_turnover_inr", 200_000))
    lookback = max(3, int(c.get("liquidity_lookback_bars", 10)))

    bar_vol = float(last.volume or 0)
    if bar_vol < min_vol:
        return False, f"bar volume {bar_vol:.0f} < min {min_vol:.0f}"

    recent = candles[-lookback:]
    avg_vol = sum(float(x.volume or 0) for x in recent) / max(len(recent), 1)
    if avg_vol > 0 and bar_vol < avg_vol * min_vs_avg:
        return False, f"volume {bar_vol:.0f} below {lookback}m avg × {min_vs_avg}"

    turnover = bar_vol * px
    if turnover < min_turnover:
        return False, f"turnover ₹{turnover:,.0f} < min ₹{min_turnover:,.0f}"
    return True, ""
