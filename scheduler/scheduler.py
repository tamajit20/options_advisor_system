"""
scheduler/scheduler.py
======================

APScheduler wrapper for the Options Advisor.

Each scheduled job follows the pattern:
    1. _job_started(job_id, job_name)  → INSERT options_job_log row
    2. Run orchestrator function
    3. On success: _job_finished(job_id, "SUCCESS", rows)
       On failure: _job_finished(job_id, "FAILED",  err) + notification
    4. Downstream chain: if FII fails, FII downstream jobs still run; but if
       FO_BHAV fails, IV calc + suggestion engine SKIP (chain dependency).

The orchestrators each take an open `SQLServerConnection`. We open one per job
to keep transaction scope tight.
"""

from __future__ import annotations

import logging
import traceback
from datetime import date, datetime
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config import SCHEDULER_CONFIG
from contracts import Notification
from database.connection import SQLServerConnection
from database.log_repo import JobLogRepo
from database.models import NotificationRepo
from lifecycle.download_orchestrator import (
    run_fii, run_fo_bhav, run_spot_bhav, run_vix,
)
from lifecycle.events_seeder import run_events_seed
from lifecycle.exit_orchestrator import run_exit_engine
from lifecycle.iv_orchestrator import run_iv_calculation
from lifecycle.event_eve_review import run_event_eve_review
from lifecycle.snapshot_orchestrator import (
    run_drift_verifier,
    run_intraday_close_snapshot,
)
from lifecycle.intraday_validator import run_intraday_validator
from lifecycle.suggestion_engine import run_live_suggestion_engine, run_suggestion_engine
from simulation.simulator import run_simulation_update
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job-state tracker — used by chain-skip logic
# ---------------------------------------------------------------------------
_LAST_STATUS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Generic job wrapper
# ---------------------------------------------------------------------------

def _make_job_id(name: str) -> str:
    return f"{name}-{today_ist().strftime('%Y%m%d')}"


def _run_job(
    job_name: str,
    fn: Callable[[SQLServerConnection], int],
    *,
    requires: Optional[list[str]] = None,
    skip_freshness: bool = False,
    job_id_suffix: Optional[str] = None,
) -> None:
    """Open DB, run `fn`, persist start/finish via JobLogRepo, post notification.

    skip_freshness: when True, bypass both the chain-skip and data-freshness
    gates. Used by manual `Run now` triggers where the operator is explicitly
    overriding the trade_date (e.g. backfilling yesterday) and wants the run
    to execute regardless of upstream freshness.
    job_id_suffix: optional extra suffix for the job_id so manual reruns of
    the same day don't overwrite the scheduled run's row.
    """
    job_id = _make_job_id(job_name)
    if job_id_suffix:
        job_id = f"{job_id}-{job_id_suffix}"

    # Chain-skip: if any required upstream job FAILED today, skip this one.
    if requires and not skip_freshness:
        for upstream in requires:
            up_status = _LAST_STATUS.get(upstream)
            if up_status in ("FAILED", "CRITICAL"):
                _record_skipped(job_id, job_name, upstream)
                return

    db = SQLServerConnection()
    try:
        db.connect()
        # Phase 3 — #6: data-based freshness gate. Independent of the
        # in-process `_LAST_STATUS` dict (which is empty after a process
        # restart). For each upstream we run a registered probe against
        # the DB; if the data isn't present yet we skip with a clear
        # reason rather than running on stale inputs.
        if requires and not skip_freshness:
            stale = _check_data_freshness(db, requires)
            if stale is not None:
                _record_skipped_with_db(
                    db, job_id, job_name,
                    f"data not fresh for upstream {stale}",
                )
                return
        job_log = JobLogRepo(db)
        notif = NotificationRepo(db)
        job_log.start(job_id, job_name)
        db.commit()

        try:
            rows = fn(db) or 0
            job_log.finish(job_id, "SUCCESS", rows_processed=int(rows))
            db.commit()
            _LAST_STATUS[job_name] = "SUCCESS"
            logger.info("Job %s SUCCESS rows=%d", job_id, rows)
        except Exception as exc:
            err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            logger.exception("Job %s FAILED", job_id)
            try:
                db.rollback()
            except Exception:
                pass
            try:
                JobLogRepo(db).finish(job_id, "FAILED", error_message=err[:1900])
                NotificationRepo(db).insert(Notification(
                    created_at=now_ist(),
                    notif_type="JOB_FAILURE",
                    severity="ERROR",
                    title=f"Job failed: {job_name}",
                    body=err[:500],
                ))
                db.commit()
            except Exception:
                logger.exception("Failed to record job failure")
            _LAST_STATUS[job_name] = "FAILED"
    finally:
        db.close()


def _record_skipped(job_id: str, job_name: str, upstream: str) -> None:
    """Open a fresh DB connection to record SKIPPED. Use when no
    connection is available (e.g. early in `_run_job` before connect)."""
    db = SQLServerConnection()
    try:
        db.connect()
        _record_skipped_with_db(
            db, job_id, job_name,
            f"Upstream {upstream} failed",
        )
    except Exception:
        logger.exception("Failed to record skipped job %s", job_id)
    finally:
        db.close()


def _record_skipped_with_db(
    db: SQLServerConnection,
    job_id: str,
    job_name: str,
    reason: str,
) -> None:
    """Record SKIPPED on an already-open connection. Used by data-freshness
    gate so we don't open a second connection mid-run."""
    try:
        JobLogRepo(db).start(job_id, job_name)
        JobLogRepo(db).finish(job_id, "SKIPPED", error_message=reason[:500])
        db.commit()
        _LAST_STATUS[job_name] = "SKIPPED"
        logger.warning("Job %s SKIPPED — %s", job_id, reason)
    except Exception:
        logger.exception("Failed to record skipped job %s", job_id)


# ---------------------------------------------------------------------------
# Phase 3 — #6: data-freshness probes
# ---------------------------------------------------------------------------
# Maps an upstream-job-name to a predicate that returns True iff that job's
# output data is present and current (today's IST trade date). Survives
# process restarts because it queries the DB rather than the in-memory
# `_LAST_STATUS` dict. Probes are best-effort: a probe that raises is
# treated as "data unavailable" and downstream is skipped with that reason.
def _probe_fo_bhav(db: SQLServerConnection) -> bool:
    from database.models import FoEodRepo
    return FoEodRepo(db).latest_trade_date() == today_ist()


def _probe_spot_bhav(db: SQLServerConnection) -> bool:
    from database.models import SpotEodRepo
    row = SpotEodRepo(db).latest("NIFTY") or {}
    return row.get("trade_date") == today_ist()


def _probe_iv_calculation(db: SQLServerConnection) -> bool:
    from database.models import IvHistoryRepo
    return IvHistoryRepo(db).latest_trade_date() == today_ist()


_DATA_PROBES: dict[str, Callable[[SQLServerConnection], bool]] = {
    "fo_bhav_download":   _probe_fo_bhav,
    "spot_bhav_download": _probe_spot_bhav,
    "iv_calculation":     _probe_iv_calculation,
}


def _check_data_freshness(
    db: SQLServerConnection,
    upstreams: list[str],
) -> Optional[str]:
    """Return the first upstream whose data is missing/stale, or None
    if every probed upstream is fresh. Upstreams without a registered
    probe are silently skipped (only the in-process status gate covers
    them). Probe exceptions count as "stale"."""
    for up in upstreams:
        probe = _DATA_PROBES.get(up)
        if probe is None:
            continue
        try:
            ok = bool(probe(db))
        except Exception:
            logger.exception("data-freshness probe for %s raised", up)
            ok = False
        if not ok:
            return up
    return None


# ---------------------------------------------------------------------------
# Job entry points
# ---------------------------------------------------------------------------

def job_fo_bhav():    _run_job("fo_bhav_download",   run_fo_bhav)
def job_spot_bhav():  _run_job("spot_bhav_download", run_spot_bhav)
def job_vix():        _run_job("vix_download",       run_vix)
def job_fii():        _run_job("fii_download",       run_fii)

def job_iv():         _run_job("iv_calculation",     run_iv_calculation,
                               requires=["fo_bhav_download", "spot_bhav_download"])
def job_suggestion(): _run_job("suggestion_engine",  run_suggestion_engine,
                               requires=["iv_calculation"])
def job_live_suggestion(): _run_job("live_suggestion_engine", run_live_suggestion_engine)
def job_simulation(): _run_job("simulation_update",  run_simulation_update)
def job_exit():       _run_job("exit_engine",        run_exit_engine,
                               requires=["fo_bhav_download"])
def job_events_seed(): _run_job("events_seed",       run_events_seed)
def job_event_eve_review(): _run_job("event_eve_review", run_event_eve_review)


def job_intraday_close_snapshot():
    _run_job("intraday_close_snapshot", run_intraday_close_snapshot)


def job_drift_verifier():
    _run_job("drift_verifier", run_drift_verifier,
             requires=["fo_bhav_download"])


def job_intraday_validator():
    _run_job("intraday_validator", run_intraday_validator)


def job_weekly_cleanup():
    """Apply retention policy and trim historical data."""
    from datetime import timedelta as _td
    from config import RETENTION_CONFIG

    def _cleanup(db: SQLServerConnection) -> int:
        from database.models import (
            FoEodRepo, SpotEodRepo, VixRepo, FiiRepo, IvHistoryRepo,
            SuggestionRepo, NotificationRepo,
        )
        today = today_ist()
        n = 0
        n += FoEodRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["fo_bhav_keep_days"]))
        n += SpotEodRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["spot_bhav_keep_days"]))
        n += VixRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["vix_keep_days"]))
        n += FiiRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["fii_keep_days"]))
        n += IvHistoryRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["iv_history_keep_days"]))
        n += SuggestionRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["suggestions_keep_days"]))
        n += NotificationRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["notifications_keep_days"]))
        return n

    _run_job("weekly_cleanup", _cleanup)


# ---------------------------------------------------------------------------
# Scheduler bootstrap
# ---------------------------------------------------------------------------

JOB_FUNCS = {
    "fo_bhav_download":   job_fo_bhav,
    "spot_bhav_download": job_spot_bhav,
    "vix_download":       job_vix,
    "fii_download":       job_fii,
    "iv_calculation":     job_iv,
    "suggestion_engine":       job_suggestion,
    "live_suggestion_engine":  job_live_suggestion,
    # Phase 3 — #1: extra intraday windows. All map to the same handler;
    # config.py keys these separately so we can enable/disable each
    # window and assign unique cron triggers.
    "live_suggestion_engine_0945": job_live_suggestion,
    "live_suggestion_engine_1300": job_live_suggestion,
    "live_suggestion_engine_1430": job_live_suggestion,
    "simulation_update":       job_simulation,
    "exit_engine":        job_exit,
    "events_seed":        job_events_seed,
    "event_eve_review":   job_event_eve_review,
    "weekly_cleanup":     job_weekly_cleanup,
    "intraday_close_snapshot": job_intraday_close_snapshot,
    "drift_verifier":          job_drift_verifier,
    "intraday_validator":      job_intraday_validator,
}


def build_scheduler() -> BackgroundScheduler:
    sch = BackgroundScheduler(timezone=SCHEDULER_CONFIG["timezone"])
    jobs = SCHEDULER_CONFIG["jobs"]
    for name, conf in jobs.items():
        if not conf.get("enabled", True):
            continue
        fn = JOB_FUNCS.get(name)
        if fn is None:
            logger.warning("No handler for scheduled job %s", name)
            continue
        trigger_kwargs = {k: v for k, v in conf.items() if k != "enabled"}
        sch.add_job(fn, CronTrigger(**trigger_kwargs), id=name, name=name,
                    misfire_grace_time=600, max_instances=1, replace_existing=True)
        logger.info("Scheduled %s @ %s", name, trigger_kwargs)
    return sch


# Module-level reference to the running scheduler (for dashboard manual triggers).
_SCHEDULER: Optional[BackgroundScheduler] = None


def get_scheduler() -> Optional[BackgroundScheduler]:
    """Return the currently-running BackgroundScheduler, or None if not started."""
    return _SCHEDULER


def trigger_job_now(job_name: str, trade_date: str | None = None) -> bool:
    """Dispatch an immediate one-off run of a configured job.

    Returns True if dispatched, False if the job_name is unknown.
    Raises RuntimeError if the scheduler is not running.

    trade_date: optional ISO date string 'YYYY-MM-DD'.  When provided it is
    passed as a keyword argument to the job function.  Only jobs whose
    orchestrator accepts a trade_date parameter will use it; others ignore it.
    """
    if job_name not in JOB_FUNCS:
        return False
    sch = _SCHEDULER
    if sch is None or not sch.running:
        raise RuntimeError("Scheduler is not running")

    base_fn = JOB_FUNCS[job_name]
    manual_suffix = f"manual-{datetime.now().strftime('%H%M%S')}"

    if trade_date:
        from datetime import date as _date
        _td = _date.fromisoformat(trade_date)
        # Jobs that support trade_date (download + calc + lifecycle) share the same
        # pattern: the underlying orchestrator accepts trade_date keyword arg.
        # We wrap the job to inject it, then run it through `_run_job` so the
        # manual run is fully logged in `options_job_log` (the dashboard reads
        # job status from that table).
        _SUPPORTED = {
            "fo_bhav_download", "spot_bhav_download", "vix_download", "fii_download",
            "iv_calculation", "suggestion_engine", "exit_engine",
        }
        if job_name in _SUPPORTED:
            from lifecycle.download_orchestrator import (
                run_fo_bhav, run_spot_bhav, run_vix, run_fii,
            )
            from lifecycle.iv_orchestrator import run_iv_calculation
            from lifecycle.suggestion_engine import run_suggestion_engine
            from lifecycle.exit_orchestrator import run_exit_engine
            _orch_map: dict[str, Callable[[SQLServerConnection], int]] = {
                "fo_bhav_download":   lambda db: run_fo_bhav(db, _td) or 0,
                "spot_bhav_download": lambda db: run_spot_bhav(db, _td) or 0,
                "vix_download":       lambda db: run_vix(db, _td) or 0,
                "fii_download":       lambda db: run_fii(db, _td) or 0,
                "iv_calculation":     lambda db: run_iv_calculation(db, _td) or 0,
                "suggestion_engine":  lambda db: run_suggestion_engine(db, _td) or 0,
                "exit_engine":        lambda db: run_exit_engine(db, _td) or 0,
            }
            orch = _orch_map[job_name]
            def fn():
                # Manual override: bypass the freshness gate (operator chose
                # the date) but still log start/finish to options_job_log.
                _run_job(
                    job_name, orch,
                    skip_freshness=True,
                    job_id_suffix=manual_suffix,
                )
        else:
            fn = base_fn
    else:
        # Plain manual run (no date override): use the registered job function
        # directly. It already routes through `_run_job` and logs to the DB.
        fn = base_fn

    run_at = datetime.now()
    sch.add_job(
        fn,
        trigger=DateTrigger(run_date=run_at),
        id=f"manual-{job_name}-{run_at.strftime('%Y%m%d%H%M%S%f')}",
        name=f"Manual {job_name}" + (f" ({trade_date})" if trade_date else ""),
        misfire_grace_time=600,
        max_instances=1,
    )
    logger.info("Manual trigger queued: %s trade_date=%s", job_name, trade_date or "auto")
    return True


def start_scheduler() -> BackgroundScheduler:
    global _SCHEDULER
    sch = build_scheduler()
    sch.start()
    _SCHEDULER = sch
    logger.info("Scheduler started with %d jobs", len(sch.get_jobs()))
    return sch