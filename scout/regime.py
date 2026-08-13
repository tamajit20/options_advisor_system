"""Market regime gates — index trend, prior-day levels, failed breakouts."""

from __future__ import annotations

from typing import Optional, Tuple

from config import SCOUT_CONFIG


def _cfg(cfg: Optional[dict] = None) -> dict:
    return cfg if cfg is not None else SCOUT_CONFIG


def index_trend_allows(side: str, bench_pct: float, cfg: Optional[dict] = None) -> Tuple[bool, str]:
    """Hard index gate — skip longs in a weak Nifty, shorts in a strong Nifty."""
    c = _cfg(cfg)
    if not c.get("index_trend_filter_enabled", True):
        return True, ""
    side_u = str(side or "").upper()
    min_pct = float(c.get("index_trend_min_pct", -0.20))
    max_pct = float(c.get("index_trend_max_pct", 0.20))
    if side_u == "BUY" and float(bench_pct) < min_pct:
        return False, (
            f"Nifty weak ({bench_pct:.2f}% from open < {min_pct:.2f}% — skip long)"
        )
    if side_u == "SELL" and float(bench_pct) > max_pct:
        return False, (
            f"Nifty strong ({bench_pct:.2f}% from open > {max_pct:.2f}% — skip short)"
        )
    return True, ""


def pdh_pdl_allows(
    action: str,
    ltp: float,
    pdh: Optional[float],
    pdl: Optional[float],
    cfg: Optional[dict] = None,
) -> Tuple[bool, str]:
    """Reject breakouts into prior-day resistance/support."""
    c = _cfg(cfg)
    if not c.get("pdh_pdl_filter_enabled", True):
        return True, ""
    buf = float(c.get("pdh_pdl_buffer_pct", 0.15)) / 100.0
    px = float(ltp or 0)
    if px <= 0:
        return True, ""
    action_u = str(action or "").upper()
    if action_u == "BUY" and pdh is not None and float(pdh) > 0:
        level = float(pdh)
        if px >= level * (1.0 - buf):
            return False, f"long into prior day high ₹{level:.2f}"
    if action_u == "SELL" and pdl is not None and float(pdl) > 0:
        level = float(pdl)
        if px <= level * (1.0 + buf):
            return False, f"short into prior day low ₹{level:.2f}"
    return True, ""


def failed_breakout_detected(signal: dict, ltp: float) -> bool:
    """Price reclaimed inside the broken range — treat as failed breakout."""
    if ltp is None or float(ltp) <= 0:
        return False
    px = float(ltp)
    action = str(signal.get("action") or "").upper()
    st = str(signal.get("signal_type") or "").upper()
    meta = dict(signal.get("meta") or {})
    try:
        if st == "OR_BREAK_UP" and action == "BUY":
            return px < float(meta["or_high"])
        if st == "OR_BREAK_DOWN" and action == "SELL":
            return px > float(meta["or_low"])
        if st in ("RANGE_BREAK_UP", "COMPRESSION_BREAK_UP") and action == "BUY":
            return px < float(meta["box_high"])
        if st in ("RANGE_BREAK_DOWN", "COMPRESSION_BREAK_DOWN") and action == "SELL":
            return px > float(meta["box_low"])
    except (KeyError, TypeError, ValueError):
        pass
    return False


def signal_passes_regime(
    signal,
    *,
    bench_pct: float,
    pdh: Optional[float],
    pdl: Optional[float],
    cfg: Optional[dict] = None,
) -> Tuple[bool, str]:
    """Apply index + PDH/PDL gates to a detected pattern."""
    ok, msg = index_trend_allows(signal.action, bench_pct, cfg)
    if not ok:
        return False, msg
    ok, msg = pdh_pdl_allows(signal.action, signal.ltp, pdh, pdl, cfg)
    if not ok:
        return False, msg
    return True, ""


def live_benchmark_pct(spot_lookup, meta: Optional[dict]) -> float:
    """Fresh Nifty % from open when WS quote available; else signal-time snapshot."""
    meta = meta or {}
    try:
        if spot_lookup is not None:
            ltp = spot_lookup("NIFTY")
            nifty_open = meta.get("nifty_open")
            if ltp and nifty_open and float(nifty_open) > 0:
                return (float(ltp) - float(nifty_open)) / float(nifty_open) * 100.0
    except (TypeError, ValueError):
        pass
    try:
        return float(meta.get("nifty_pct_from_open") or 0)
    except (TypeError, ValueError):
        return 0.0
