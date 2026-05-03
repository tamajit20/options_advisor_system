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
from lifecycle.suggestion_engine import run_suggestion_engine
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
) -> None:
    """Open DB, run `fn`, persist start/finish via JobLogRepo, post notification."""
    job_id = _make_job_id(job_name)

    # Chain-skip: if any required upstream job FAILED today, skip this one.
    if requires:
        for upstream in requires:
            up_status = _LAST_STATUS.get(upstream)
            if up_status in ("FAILED", "CRITICAL"):
                _record_skipped(job_id, job_name, upstream)
                return

    db = SQLServerConnection()
    try:
        db.connect()
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
    db = SQLServerConnection()
    try:
        db.connect()
        JobLogRepo(db).start(job_id, job_name)
        JobLogRepo(db).finish(
            job_id, "SKIPPED",
            error_message=f"Upstream {upstream} failed",
        )
        db.commit()
        _LAST_STATUS[job_name] = "SKIPPED"
        logger.warning("Job %s SKIPPED — upstream %s failed", job_id, upstream)
    except Exception:
        logger.exception("Failed to record skipped job %s", job_id)
    finally:
        db.close()


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
def job_simulation(): _run_job("simulation_update",  run_simulation_update)
def job_exit():       _run_job("exit_engine",        run_exit_engine,
                               requires=["fo_bhav_download"])
def job_events_seed(): _run_job("events_seed",       run_events_seed)


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
    "suggestion_engine":  job_suggestion,
    "simulation_update":  job_simulation,
    "exit_engine":        job_exit,
    "events_seed":        job_events_seed,
    "weekly_cleanup":     job_weekly_cleanup,
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
    if trade_date:
        from datetime import date as _date
        from database.connection import SQLServerConnection as _DB
        _td = _date.fromisoformat(trade_date)
        # Jobs that support trade_date (download + calc + lifecycle) share the same
        # pattern: the underlying orchestrator accepts trade_date keyword arg.
        # We wrap the job to inject it.
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
            _orch_map = {
                "fo_bhav_download":   lambda db: run_fo_bhav(db, _td),
                "spot_bhav_download": lambda db: run_spot_bhav(db, _td),
                "vix_download":       lambda db: run_vix(db, _td),
                "fii_download":       lambda db: run_fii(db, _td),
                "iv_calculation":     lambda db: run_iv_calculation(db, _td),
                "suggestion_engine":  lambda db: run_suggestion_engine(db, _td),
                "exit_engine":        lambda db: run_exit_engine(db, _td),
            }
            orch = _orch_map[job_name]
            def fn():
                from database.connection import SQLServerConnection as _DBC
                import traceback as _tb
                db = _DBC()
                try:
                    db.connect()
                    orch(db)
                    db.commit()
                except Exception:
                    logger.exception("Manual job %s (trade_date=%s) failed", job_name, trade_date)
                    try: db.rollback()
                    except Exception: pass
                finally:
                    db.close()
        else:
            fn = base_fn
    else:
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