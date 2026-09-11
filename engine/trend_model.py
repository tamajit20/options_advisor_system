"""
engine/trend_model.py
=====================

Structural (SMA crossover + ADX), session (intraday), and short-horizon
return overlays merged for strategy selection.

Effective labels: BULLISH | BEARISH | SIDEWAYS | MIXED.

MIXED means SMA structure and the recent tape disagree. That is a sit-out,
not a SIDEWAYS Iron Condor and not a directional spread. A 5–10 day drop
does not mean the next week keeps falling (bounce is likely); the reverse
is true of a 5–10 day rally.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Sequence

from config import STRATEGY_CONFIG

MIXED_TREND = "MIXED"
_DIRECTIONAL = frozenset({"BULLISH", "BEARISH"})


def mixed_trend_sitout_reason() -> str:
    """NoSuggestion / StrategyVeto copy when SMA and recent tape disagree."""
    return (
        "Mixed trend: SMA structure and the recent tape disagree — "
        "no directional edge. A 5–10 day drop can bounce (and a rally can fade). "
        "Sitting out; not treating this as BEARISH, BULLISH, or SIDEWAYS."
    )


def _opposing_directions(a: Optional[str], b: Optional[str]) -> bool:
    return a in _DIRECTIONAL and b in _DIRECTIONAL and a != b


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


def session_intraday_trend(
    *,
    spot_now: float,
    session_bar: Optional[dict],
) -> Optional[str]:
    """Same-day trend from session open vs spot. None when there is no bar."""
    if spot_now <= 0 or not session_bar:
        return None
    try:
        open_px = float(session_bar.get("open_price") or 0) or None
    except (TypeError, ValueError):
        open_px = None
    if not open_px or open_px <= 0:
        return None
    open_min = float(STRATEGY_CONFIG.get("trend_session_open_pct_min", 0.35))
    m = _pct_change(spot_now, open_px)
    if m is None:
        return None
    if abs(m) >= open_min:
        return "BULLISH" if m > 0 else "BEARISH"
    return "SIDEWAYS"


def session_trend(
    *,
    spot_now: float,
    session_bar: Optional[dict],
    spot_history: Sequence[dict],
    as_of: date,
) -> Optional[str]:
    """Live tape label: same-day open first, then N-day close (display / diagnostics)."""
    if spot_now <= 0:
        return None

    intra = session_intraday_trend(spot_now=spot_now, session_bar=session_bar)
    if intra in _DIRECTIONAL:
        return intra

    nday_min = float(STRATEGY_CONFIG.get("trend_session_nday_pct_min", 0.60))
    lookback = int(STRATEGY_CONFIG.get("trend_session_lookback_days", 5))

    hist = filter_spot_history(spot_history, as_of)
    closes = [float(r["close_price"]) for r in hist if r.get("close_price")]
    if not closes:
        return intra

    if len(closes) >= lookback:
        m = _pct_change(spot_now, closes[-lookback])
        if m is not None and abs(m) >= nday_min:
            return "BULLISH" if m > 0 else "BEARISH"

    return intra if intra is not None else "SIDEWAYS"


def resolve_trend(
    structural: str,
    session: Optional[str],
    *,
    live_mode: bool,
) -> str:
    """Merge structural SMA with *same-day* session tape.

    ``session`` must be the intraday (open vs spot) label, not the 5–10 day
    tape. A multi-day dump does not lift chop SMA to BEARISH.
    """
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
        return MIXED_TREND
    return session


def apply_return_override(
    effective: str,
    structural: str,
    return_trend: Optional[str],
) -> str:
    """Apply short-horizon return rules on top of structural + session merge.

    Agreeing SMA + tape keep the directional label. Opposite labels become
    MIXED (sit out). Structural SIDEWAYS is never lifted to BEARISH/BULLISH
    from a 5–10 day return — that chase is disabled even if a saved config
    overlay still has ``trend_return_override_structural`` True.
    """
    if return_trend is None:
        return effective

    if STRATEGY_CONFIG.get("trend_return_confirm_structural", True):
        if _opposing_directions(structural, return_trend):
            return MIXED_TREND
        if _opposing_directions(effective, return_trend):
            return MIXED_TREND

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
    # Effective trend uses same-day open vs spot only — not the 5-day session
    # lookback, which would re-introduce "dump last week ⇒ BEARISH today".
    intraday = (
        session_intraday_trend(spot_now=spot_now, session_bar=session_bar)
        if live_mode
        else None
    )

    return_pct = short_horizon_return_pct(
        spot_history=hist,
        as_of=as_of,
        spot_now=spot_now,
    )
    return_trend = short_horizon_trend_from_return(return_pct)

    effective = resolve_trend(structural, intraday, live_mode=live_mode)
    effective = apply_return_override(effective, structural, return_trend)

    return effective, structural, session, return_pct, return_trend
