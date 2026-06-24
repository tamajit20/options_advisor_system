"""
lifecycle/data_backfill.py
==========================

Helpers for EOD download / calc jobs: discover missing weekdays in a
lookback window and run per-date workers with holiday-tolerant logging.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Callable, Iterable, List, Optional, TypeVar

from config import SCHEDULER_CONFIG
from database.connection import SQLServerConnection
from exceptions import NoDataError
from utils import today_ist

logger = logging.getLogger(__name__)

T = TypeVar("T")


def backfill_lookback_days() -> int:
    return int(SCHEDULER_CONFIG.get("data_backfill_lookback_days", 30))


def weekdays_in_range(start: date, end: date) -> List[date]:
    """Inclusive Mon–Fri dates between *start* and *end*."""
    if end < start:
        return []
    out: List[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def dates_to_process(
    *,
    has_date: Callable[[date], bool],
    end: Optional[date] = None,
    lookback_days: Optional[int] = None,
    always_refresh_end: bool = True,
) -> List[date]:
    """Weekdays in the lookback window missing from DB, plus *end* when requested.

    *always_refresh_end* re-fetches the latest session even when a row already
    exists (scheduled post-market refresh).
    """
    end = end or today_ist()
    lookback = lookback_days if lookback_days is not None else backfill_lookback_days()
    start = end - timedelta(days=lookback)
    pending = {d for d in weekdays_in_range(start, end) if not has_date(d)}
    if always_refresh_end and end.weekday() < 5:
        pending.add(end)
    return sorted(pending)


def run_dates_backfill(
    dates: Iterable[date],
    worker: Callable[[date], int],
    *,
    label: str,
    fail_if_today_missing: bool = True,
    today: Optional[date] = None,
) -> int:
    """Run *worker* for each date; skip holidays (NoDataError) except for today."""
    today = today or today_ist()
    total = 0
    date_list = list(dates)
    if not date_list:
        logger.info("%s: nothing to backfill in lookback window", label)
        return 0

    logger.info("%s: processing %d date(s): %s … %s",
                label, len(date_list), date_list[0], date_list[-1])
    for d in date_list:
        try:
            n = worker(d)
            total += int(n or 0)
            logger.info("%s %s: %d rows", label, d, n)
        except NoDataError as exc:
            if d == today and fail_if_today_missing:
                raise
            logger.info("%s %s: no data (%s)", label, d, exc)

    return total


def run_or_backfill(
    db: SQLServerConnection,
    trade_date: Optional[date],
    *,
    label: str,
    has_date: Callable[[date], bool],
    single_date_fn: Callable[[SQLServerConnection, date], int],
    always_refresh_end: bool = True,
) -> int:
    """Single-date mode when *trade_date* set; otherwise gap-fill + refresh today."""
    if trade_date is not None:
        return single_date_fn(db, trade_date)

    dates = dates_to_process(
        has_date=has_date,
        always_refresh_end=always_refresh_end,
    )
    return run_dates_backfill(
        dates,
        lambda d: single_date_fn(db, d),
        label=label,
    )
