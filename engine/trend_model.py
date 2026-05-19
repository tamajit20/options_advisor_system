"""
engine/trend_model.py
=====================

Structural (SMA crossover + ADX), session (intraday), and short-horizon
return overrides merged for strategy selection.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Sequence

from config import STRATEGY_CONFIG


def has_real_ohlc(row: dict) -> bool:
    """True when high/low differ enough for ADX / range-based calcs."""
    try:
        h = float(row["high_price"])
        l = float(row["low_price"])
        c = float(row["close_price"])
    except (KeyError, TypeError, ValueError):
        return False
    if c <= 0:
        return False
    return (h - l) > max(c * 1e-6, 0.01)


def filter_spot_history(
    spot_history: Sequence[dict],
    as_of: date,
) -> List[dict]:
    """Rows with trade_date <= as_of, ascending."""
    out: List[dict] = []
    for r in spot_history:
        td = r.get("trade_date")
        if td is None:
            continue
        if hasattr(td, "date"):
            td = td.date() if callable(getattr(td, "date", None)) else td
        if td <= as_of:
            out.append(r)
    out.sort(key=lambda x: x["trade_date"])
    return out


def upsert_session_bar(
    spot_history: Sequence[dict],
    session_bar: dict,
) -> List[dict]:
    """Replace or append the bar for ``session_bar['trade_date']``."""
    td = session_bar.get("trade_date")
    if td is None:
        return list(spot_history)
    hist = [r for r in spot_history if r.get("trade_date") != td]
    hist.append(dict(session_bar))
    hist.sort(key=lambda x: x["trade_date"])
    return hist


def _pct_change(new: float, old: float) -> Optional[float]:
    if old is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def short_horizon_return_pct(
    *,
    spot_history: Sequence[dict],
    as_of: date,
    spot_now: float,
) -> Optional[float]:
    """Largest |%| move vs configured lookback closes (5d and optional 10d)."""
    if spot_now <= 0:
        return None
    hist = filter_spot_history(spot_history, as_of)
    closes = [float(r["close_price"]) for r in hist if r.get("close_price")]
    if not closes:
        return None

    lookbacks = [int(STRATEGY_CONFIG.get("trend_return_lookback_days", 5))]
    alt = int(STRATEGY_CONFIG.get("trend_return_lookback_days_alt", 10))
    if alt > 0 and alt not in lookbacks:
        lookbacks.append(alt)

    best: Optional[float] = None
    for lb in lookbacks:
        if len(closes) < lb:
            continue
        ref = closes[-lb]
        m = _pct_change(spot_now, ref)
        if m is None:
            continue
        if best is None or abs(m) > abs(best):
            best = m
    return best


def short_horizon_trend_from_return(return_pct: Optional[float]) -> Optional[str]:
    """Map N-day return % to BULLISH / BEARISH / SIDEWAYS, or None if unknown."""
    if return_pct is None:
        return None
    bull = float(STRATEGY_CONFIG.get("trend_return_bullish_pct", 1.5))
    bear = float(STRATEGY_CONFIG.get("trend_return_bearish_pct", -1.5))
    if return_pct >= bull:
        return "BULLISH"
    if return_pct <= bear:
        return "BEARISH"
    return "SIDEWAYS"


def session_trend(
    *,
    spot_now: float,
    session_bar: Optional[dict],
    spot_history: Sequence[dict],
    as_of: date,
) -> Optional[str]:
    """Short-horizon trend from session open + recent daily closes (live)."""
    if spot_now <= 0:
        return None

    open_min = float(STRATEGY_CONFIG.get("trend_session_open_pct_min", 0.35))
    nday_min = float(STRATEGY_CONFIG.get("trend_session_nday_pct_min", 0.60))
    lookback = int(STRATEGY_CONFIG.get("trend_session_lookback_days", 5))

    open_px: Optional[float] = None
    if session_bar:
        try:
            open_px = float(session_bar.get("open_price") or 0) or None
        except (TypeError, ValueError):
            open_px = None

    hist = filter_spot_history(spot_history, as_of)
    closes = [float(r["close_price"]) for r in hist if r.get("close_price")]
    if not closes:
        return None

    if open_px and open_px > 0:
        m = _pct_change(spot_now, open_px)
        if m is not None and abs(m) >= open_min:
            return "BULLISH" if m > 0 else "BEARISH"

    if len(closes) >= lookback:
        m = _pct_change(spot_now, closes[-lookback])
        if m is not None and abs(m) >= nday_min:
            return "BULLISH" if m > 0 else "BEARISH"

    return "SIDEWAYS"


def resolve_trend(
    structural: str,
    session: Optional[str],
    *,
    live_mode: bool,
) -> str:
    """Merge structural and session labels into the effective strategy trend."""
    if not live_mode or session is None:
        return structural
    if session == structural:
        return structural
    if not STRATEGY_CONFIG.get("trend_live_session_override", True):
        return structural
    if session not in ("BULLISH", "BEARISH"):
        return structural
    if structural == "SIDEWAYS":
        return session
    if STRATEGY_CONFIG.get("trend_session_confirm_structural", True):
        return "SIDEWAYS"
    return session


def apply_return_override(
    effective: str,
    structural: str,
    return_trend: Optional[str],
) -> str:
    """Apply short-horizon return rules on top of structural + session merge."""
    if return_trend is None or return_trend == "SIDEWAYS":
        return effective
    if not STRATEGY_CONFIG.get("trend_return_override_structural", True):
        return effective

    # A: structural SIDEWAYS + strong recent return → directional effective trend
    if structural == "SIDEWAYS" and return_trend in ("BULLISH", "BEARISH"):
        return return_trend

    # Conflict: structural direction disagrees with recent tape
    if STRATEGY_CONFIG.get("trend_return_confirm_structural", True):
        if structural == "BULLISH" and return_trend == "BEARISH":
            return "SIDEWAYS"
        if structural == "BEARISH" and return_trend == "BULLISH":
            return "SIDEWAYS"

    # Structural directional but return strongly opposes → neutralize credit bias
    if structural in ("BULLISH", "BEARISH") and return_trend != structural:
        if effective == structural:
            return "SIDEWAYS"

    return effective


def compute_trends(
    *,
    spot_history: Sequence[dict],
    as_of: date,
    spot_now: float,
    session_bar: Optional[dict],
    live_mode: bool,
) -> tuple[str, str, Optional[str], Optional[float], Optional[str]]:
    """Return (effective, structural, session, return_pct, return_trend)."""
    hist = filter_spot_history(spot_history, as_of)
    trend_hist = upsert_session_bar(hist, session_bar) if session_bar else hist
    from engine.indicators import trend as structural_trend_fn
    structural = structural_trend_fn(trend_hist)

    session = (
        session_trend(
            spot_now=spot_now,
            session_bar=session_bar,
            spot_history=hist,
            as_of=as_of,
        )
        if live_mode
        else None
    )

    return_pct = short_horizon_return_pct(
        spot_history=hist,
        as_of=as_of,
        spot_now=spot_now,
    )
    return_trend = short_horizon_trend_from_return(return_pct)

    effective = resolve_trend(structural, session, live_mode=live_mode)
    effective = apply_return_override(effective, structural, return_trend)

    return effective, structural, session, return_pct, return_trend
