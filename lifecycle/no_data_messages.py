"""
lifecycle/no_data_messages.py
===============================

Consistent NO_DATA / SKIPPED messages that include the latest date already
stored in the database.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Callable, Optional

from utils import previous_trading_day

if TYPE_CHECKING:
    from database.connection import SQLServerConnection

LatestDateFn = Callable[["SQLServerConnection"], Optional[date]]


def latest_available_suffix(latest: Optional[date]) -> str:
    if latest is not None:
        return f" Latest available in DB: {latest.isoformat()}."
    return " No data in DB yet."


def format_no_data_message(
    *,
    dataset: str,
    trade_date: date,
    reason: str,
    latest_available: Optional[date],
) -> str:
    """Build a user-facing NO_DATA message for download / calc jobs."""
    return (
        f"{dataset} not available for {trade_date} — {reason}."
        f"{latest_available_suffix(latest_available)}"
    )


def _fo_latest(db: "SQLServerConnection") -> Optional[date]:
    from database.models import FoEodRepo
    return FoEodRepo(db).latest_trade_date()


def _spot_latest(db: "SQLServerConnection") -> Optional[date]:
    from database.models import SpotEodRepo
    return SpotEodRepo(db).latest_trade_date()


def _vix_latest(db: "SQLServerConnection") -> Optional[date]:
    from database.models import VixRepo
    return VixRepo(db).latest_trade_date()


def _fii_latest(db: "SQLServerConnection") -> Optional[date]:
    from database.models import FiiRepo
    return FiiRepo(db).latest_trade_date()


def _iv_latest(db: "SQLServerConnection") -> Optional[date]:
    from database.models import IvHistoryRepo
    return IvHistoryRepo(db).latest_trade_date()


# Job names as stored in options_job_log → latest trade_date probe.
LATEST_DATE_BY_JOB: dict[str, LatestDateFn] = {
    "fo_bhav_download":   _fo_latest,
    "spot_bhav_download": _spot_latest,
    "vix_download":       _vix_latest,
    "fii_download":       _fii_latest,
    "iv_calculation":     _iv_latest,
}


def latest_trade_date_for_job(
    db: "SQLServerConnection",
    job_name: str,
) -> Optional[date]:
    probe = LATEST_DATE_BY_JOB.get(job_name)
    if probe is None:
        return None
    try:
        return probe(db)
    except Exception:
        return None


_NO_DATA_RE = re.compile(
    r"^(?P<dataset>.+?) not available for (?P<trade_date>\d{4}-\d{2}-\d{2})"
    r" — (?P<reason>.+?)\.?$"
)


def _morning_catchup_window(started_at: datetime | None) -> bool:
    """True when *started_at* falls in the Mon–Fri pre-market job window."""
    if started_at is None:
        return False
    if started_at.tzinfo is not None:
        from zoneinfo import ZoneInfo
        started_at = started_at.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    if started_at.weekday() >= 5:
        return False
    return time(8, 0) <= started_at.time() <= time(10, 30)


def _morning_prior_session_reason(*, dataset: str, prior: date) -> str:
    ds = dataset.lower()
    if "vix" in ds:
        return (
            f"prior session ({prior.isoformat()}) VIX not in NSE archive yet "
            f"(morning pre-market run)"
        )
    if "fii" in ds:
        return (
            f"prior session ({prior.isoformat()}) FII OI not published on NSE yet "
            f"(morning pre-market run)"
        )
    return (
        f"prior trading session ({prior.isoformat()}) not published on NSE yet "
        f"(morning pre-market run — today's bhav is not expected)"
    )


def clarify_morning_no_data_message(
    message: str,
    *,
    job_name: str,
    started_at: datetime | None,
) -> str:
    """Rewrite legacy/wrong-date NO_DATA rows from the morning catchup window."""
    if not message or job_name not in LATEST_DATE_BY_JOB:
        return message
    if "morning pre-market run" in message or "prior trading session" in message:
        return message
    if not _morning_catchup_window(started_at):
        return message

    m = _NO_DATA_RE.match(message.strip())
    if not m:
        return message

    run_day = started_at.date()
    trade_date = date.fromisoformat(m.group("trade_date"))
    reason = m.group("reason")
    if trade_date != run_day:
        return message
    if "market holiday or NSE has not published" not in reason:
        return message

    prior = previous_trading_day(run_day)
    dataset = m.group("dataset")
    new_reason = _morning_prior_session_reason(dataset=dataset, prior=prior)
    return f"{dataset} not available for {prior} — {new_reason}."


def reconcile_no_data_with_latest(
    message: str,
    latest: Optional[date],
) -> str:
    """Rewrite contradictory NO_DATA when DB already has the target session."""
    if latest is None:
        return message
    m = _NO_DATA_RE.match(message.strip())
    if not m:
        return message
    try:
        trade_date = date.fromisoformat(m.group("trade_date"))
    except ValueError:
        return message
    if latest >= trade_date:
        dataset = m.group("dataset")
        return (
            f"{dataset} for {trade_date.isoformat()} already in DB "
            f"(latest stored: {latest.isoformat()}). "
            f"No NSE re-download needed."
        )
    return message


def enrich_with_latest_in_db(
    db: "SQLServerConnection",
    job_name: str,
    message: str,
    *,
    started_at: datetime | None = None,
) -> str:
    """Append latest-available suffix when *message* does not already include it."""
    message = clarify_morning_no_data_message(
        message, job_name=job_name, started_at=started_at,
    )
    latest = latest_trade_date_for_job(db, job_name)
    reconciled = reconcile_no_data_with_latest(message, latest)
    if reconciled != message:
        return reconciled
    if "Latest available in DB:" in message or "No data in DB yet" in message:
        return message
    return message.rstrip(".") + "." + latest_available_suffix(latest)


def raise_no_data(
    db: "SQLServerConnection",
    *,
    dataset: str,
    trade_date: date,
    reason: str,
    latest_fn: LatestDateFn,
) -> None:
    from exceptions import NoDataError

    latest = latest_fn(db)
    raise NoDataError(format_no_data_message(
        dataset=dataset,
        trade_date=trade_date,
        reason=reason,
        latest_available=latest,
    ))
