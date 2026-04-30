"""
utils.py — small shared helpers (no external dependencies, no DB, no I/O).

Boundary: imported by everyone. Do NOT import from any project module here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

_IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Current IST time as a *naive* datetime (we store naive datetimes in DB)."""
    return datetime.now(_IST).replace(tzinfo=None)


def today_ist() -> date:
    return now_ist().date()


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
