"""
lifecycle/eod_session.py
========================

Context for EOD pipeline runs (evening vs morning catchup).

Morning catchup (09:00 IST, VM boot 08:55) targets the **prior trading
session** bhav — never today's file, which does not exist pre-market.
Mon morning → Fri session; Tue → Mon; etc.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from typing import Iterator, Optional

from utils import previous_trading_day, today_ist

_session: ContextVar[Optional["EodSession"]] = ContextVar("eod_session", default=None)


@dataclass(frozen=True)
class EodSession:
    bhav_end_date: date
    morning_catchup: bool


def current_session() -> Optional[EodSession]:
    return _session.get()


def effective_bhav_end_date() -> date:
    """Last bhav session to download/process in the active pipeline run."""
    sess = current_session()
    if sess is not None:
        return sess.bhav_end_date
    return today_ist()


def is_morning_catchup() -> bool:
    sess = current_session()
    return bool(sess and sess.morning_catchup)


def bhav_unavailable_reason(*, dataset: str = "") -> str:
    """User-facing reason when a bhav / EOD download finds no file."""
    del dataset  # reserved for dataset-specific wording later
    if is_morning_catchup():
        end = effective_bhav_end_date()
        return (
            f"prior trading session ({end.isoformat()}) not published on NSE yet "
            f"(morning pre-market run — today's bhav is not expected)"
        )
    return "market holiday or NSE has not published the file yet"


def vix_unavailable_reason() -> str:
    if is_morning_catchup():
        end = effective_bhav_end_date()
        return (
            f"prior session ({end.isoformat()}) VIX not in NSE archive yet "
            f"(morning pre-market run)"
        )
    return "NSE index-close archive and live sources had no match"


def upstream_missing_reason(upstream: str) -> str:
    target = effective_bhav_end_date()
    if is_morning_catchup():
        return (
            f"Upstream '{upstream}' has no prior-session data for {target} "
            f"(morning catchup — today's bhav is not expected yet)"
        )
    return (
        f"Upstream '{upstream}' has no data for {target} — "
        "market holiday or source file not yet published"
    )


@contextmanager
def eod_pipeline_session(*, morning_catchup: bool = False) -> Iterator[EodSession]:
    today = today_ist()
    end = previous_trading_day(today) if morning_catchup else today
    token = EodSession(bhav_end_date=end, morning_catchup=morning_catchup)
    reset = _session.set(token)
    try:
        yield token
    finally:
        _session.reset(reset)
