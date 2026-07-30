"""
lifecycle/no_data_messages.py
===============================

Consistent NO_DATA / SKIPPED messages that include the latest date already
stored in the database.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Callable, Optional

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


def enrich_with_latest_in_db(
    db: "SQLServerConnection",
    job_name: str,
    message: str,
) -> str:
    """Append latest-available suffix when *message* does not already include it."""
    if "Latest available in DB:" in message or "No data in DB yet" in message:
        return message
    latest = latest_trade_date_for_job(db, job_name)
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
