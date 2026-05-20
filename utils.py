"""
utils.py — small shared helpers (no external dependencies, no DB, no I/O).

Boundary: imported by everyone. Do NOT import from any project module here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Current naive IST datetime (no tzinfo).

    Uses explicit ZoneInfo("Asia/Kolkata") so the result is always correct
    regardless of the host OS timezone — on Windows dev boxes, Docker
    containers, or any server where TZ may not be set to Asia/Kolkata.
    The returned datetime is *naive* for backwards-compatibility with the
    database layer (all options_* tables store naive IST).
    """
    return datetime.now(tz=_IST).replace(tzinfo=None)


def today_ist() -> date:
    """Current IST date, independent of the host OS timezone."""
    return datetime.now(tz=_IST).date()


def parse_ddmmyyyy(s: str) -> date:
    """Parse '30042026' → date(2026, 4, 30)."""
    return datetime.strptime(s, "%d%m%Y").date()


def fmt_yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def fmt_ddmmyyyy(d: date) -> str:
    return d.strftime("%d%m%Y")


def parse_nse_expiry(s: str) -> date:
    """Parse 'DD-MMM-YYYY' / 'DD-MMM-YY' / 'YYYY-MM-DD' / 'DDMONYYYY'."""
    s = s.strip().upper()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d%b%Y", "%d%b%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unparseable expiry date: {s!r}")


def safe_float(v, default: Optional[float] = None) -> Optional[float]:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def safe_int(v, default: Optional[int] = None) -> Optional[int]:
    if v is None or v == "":
        return default
    try:
        return int(float(v))   # NSE files sometimes have decimals
    except (TypeError, ValueError):
        return default


def days_between(a: date, b: date) -> int:
    """Calendar days from a to b (b - a)."""
    return (b - a).days


# ---------------------------------------------------------------------------
# Phase 2c provenance helpers
# ---------------------------------------------------------------------------
ENGINE_VERSION = "v1"  # bump when business-logic-affecting changes ship


def market_state_at(now: datetime) -> str:
    """Classify a moment of the trading day for provenance stamping.

    NSE cash equity hours run 09:15\u201315:30 IST. The 09:15\u201309:30 window is
    notoriously volatile; the closing-auction window is 15:00\u201315:30
    (we tag the last 15 min specifically). All times are IST \u2014 callers
    pass `now_ist()`.

    Returns one of:
        'PRE_OPEN'        \u2014 before 09:15
        'OPEN_VOLATILE'   \u2014 09:15\u201309:30
        'OPEN_STABLE'     \u2014 09:30\u201315:00
        'CLOSE_AUCTION'   \u2014 15:00\u201315:30
        'POST_CLOSE'      \u2014 after 15:30
    """
    t = now.time()
    if t < datetime(now.year, now.month, now.day, 9, 15).time():
        return "PRE_OPEN"
    if t < datetime(now.year, now.month, now.day, 9, 30).time():
        return "OPEN_VOLATILE"
    if t < datetime(now.year, now.month, now.day, 15, 0).time():
        return "OPEN_STABLE"
    if t <= datetime(now.year, now.month, now.day, 15, 30).time():
        return "CLOSE_AUCTION"
    return "POST_CLOSE"
