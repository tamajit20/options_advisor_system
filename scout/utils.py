"""Scout-specific helpers (no imports from options engine/lifecycle)."""

from __future__ import annotations

from datetime import datetime, time
from typing import Tuple

from config import SCOUT_CONFIG
from utils import now_ist, today_ist


def market_hours() -> Tuple[time, time]:
    open_h, open_m = SCOUT_CONFIG["market_open"]
    close_h, close_m = SCOUT_CONFIG["market_close"]
    return time(open_h, open_m), time(close_h, close_m)


def is_market_open(now: datetime | None = None) -> bool:
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    open_t, close_t = market_hours()
    t = now.time()
    return open_t <= t <= close_t


def session_start_dt(now: datetime | None = None) -> datetime:
    """Today's session open (naive IST)."""
    now = now or now_ist()
    open_h, open_m = SCOUT_CONFIG["market_open"]
    return datetime.combine(today_ist(), time(open_h, open_m))


def pct_change(from_px: float, to_px: float) -> float:
    if not from_px:
        return 0.0
    return (to_px - from_px) / from_px * 100.0
