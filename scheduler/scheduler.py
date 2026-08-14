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

import contextvars
import logging
import threading
import traceback
from datetime import date, datetime
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import SCHEDULER_CONFIG
from contracts import Notification
from database.connection import SQLServerConnection
from database.log_repo import JobLogRepo
from database.models import NotificationRepo
from lifecycle.download_orchestrator import (
    run_fii, run_fo_bhav, run_spot_bhav, run_vix,
)
from lifecycle.events_seeder import run_events_seed
from scout.index_constituents import run_scout_index_constituents
from lifecycle.exit_orchestrator import run_exit_engine
from lifecycle.iv_orchestrator import run_iv_calculation
from lifecycle.event_eve_review import run_event_eve_review
from lifecycle.snapshot_orchestrator import (
    run_drift_verifier,
    run_intraday_close_snapshot,
)
from lifecycle.intraday_validator import run_intraday_validator
from lifecycle.suggestion_engine import run_live_suggestion_engine, run_suggestion_engine
from lifecycle.trade_greeks_job import run_trade_greeks_update
from simulation.simulator import run_simulation_update
from exceptions import NoDataError
from lifecycle.no_data_messages import enrich_with_latest_in_db
from lifecycle.eod_session import (
    eod_pipeline_session,
    effective_bhav_end_date,
    upstream_missing_reason,
)
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)

# EOD steps run sequentially by eod_nightly_pipeline (Azure VM 20:30–21:00 window).
_EOD_PIPELINE_STEPS: tuple[str, ...] = (
    "fo_bhav_download",
    "spot_bhav_download",
    "vix_download",
    "fii_download",
    "iv_calculation",
    "suggestion_engine",
    "drift_verifier",
    "simulation_update",
    "exit_engine",
    "trade_greeks_update",
)

_EOD_JOBS_SUPERSEDED = frozenset(_EOD_PIPELINE_STEPS)


def _eod_pipeline_enabled() -> bool:
    conf = SCHEDULER_CONFIG.get("jobs", {}).get("eod_nightly_pipeline", {})
    return bool(conf.get("enabled", False))


def _morning_eod_catchup_enabled() -> bool:
    conf = SCHEDULER_CONFIG.get("jobs", {}).get("morning_eod_catchup", {})
    return bool(conf.get("enabled", False))


# ---------------------------------------------------------------------------
# Job-state tracker — used by chain-skip logic
# ---------------------------------------------------------------------------
_LAST_STATUS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Generic job wrapper
# ---------------------------------------------------------------------------

def _make_job_id(name: str) -> str:
    return f"{name}-{today_ist().strftime('%Y%m%d')}"


class JobTimeoutError(Exception):
    """Raised when a job exceeds its configured wall-clock budget."""


def _job_timeout_seconds(job_name: str) -> int:
    timeouts = SCHEDULER_CONFIG.get("job_timeout_seconds", {}) or {}
    default = int(SCHEDULER_CONFIG.get("default_job_timeout_seconds", 600))
    return int(timeouts.get(job_name, default))


def _run_with_timeout(job_name, fn, db):
    """Run `fn(db)` with a watchdog that aborts on timeout.

    On expiry the watchdog closes the DB connection from a separate
    thread -- this severs the TCP session, SQL Server kills the SPID,
    and any locks held by uncommitted transactions are released
    immediately. The worker thread will surface a connection-dropped
    error which we re-raise as JobTimeoutError so `_run_job` records
    the row as FAILED.

    A one-off `_run_job_timeout` override (set on the function) allows
    manual triggers / tests to override the configured timeout.
    """
    timeout_s = fn.__dict__.get("_run_job_timeout") if hasattr(fn, "__dict__") else None
    if timeout_s is None:
        timeout_s = _job_timeout_seconds(job_name)
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        timeout_s = float(_job_timeout_seconds(job_name))
    if timeout_s <= 0:
        return fn(db)

    result: dict = {"value": None, "error": None}
    done = threading.Event()
    # ContextVar (EOD morning session) does not inherit into new threads —
    # copy the parent context so download jobs target prior-session bhav.
    ctx = contextvars.copy_context()

    def _worker():
        try:
            result["value"] = ctx.run(fn, db)
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc
        finally:
            done.set()

    t = threading.Thread(
        target=_worker,
        name=f"job-{job_name}",
        daemon=True,
    )
    t.start()
    finished = done.wait(timeout_s)
    if not finished:
        # Force-close the DB connection from this thread to release
        # SQL Server locks. The worker will get an exception on its
        # next pyodbc call but we no longer care -- we raise our own.
        logger.error(
            "Job %s exceeded %ds wall-clock budget; closing DB to release locks",
            job_name, timeout_s,
        )
        # Dump the worker thread's stack so we can see exactly where
        # it was blocked. Critical for diagnosing future hangs.
        try:
            import sys, traceback
            frame = sys._current_frames().get(t.ident)
            if frame is not None:
                stack = "".join(traceback.format_stack(frame))
                logger.error("Job %s worker stack at timeout:\n%s", job_name, stack)
        except Exception:  # noqa: BLE001
            logger.warning("Watchdog: stack dump failed", exc_info=True)
        try:
            db.close()
        except Exception:  # noqa: BLE001
            logger.warning("Watchdog: db.close() raised", exc_info=True)
        raise JobTimeoutError(
            f"job '{job_name}' exceeded {timeout_s}s timeout"
        )
    if result["error"] is not None:
        raise result["error"]
    return result["value"]


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

    # Chain-skip: if any required upstream job FAILED or has NO_DATA today,
    # skip this one with a clear reason.
    if requires and not skip_freshness:
        for upstream in requires:
            up_status = _LAST_STATUS.get(upstream)
            if up_status in ("FAILED", "CRITICAL"):
                _record_skipped(job_id, job_name, f"{upstream} failed")
                return
            if up_status == "NO_DATA":
                base = (
                    f"{upstream} has no data — market holiday or file not yet published"
                )
                _record_skipped(job_id, job_name, base, latest_for_job=upstream)
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
                base = upstream_missing_reason(stale)
                _record_skipped_with_db(
                    db, job_id, job_name,
                    enrich_with_latest_in_db(db, stale, base),
                )
                return
        job_log = JobLogRepo(db)
        notif = NotificationRepo(db)
        job_log.start(job_id, job_name)
        db.commit()

        try:
            rows = _run_with_timeout(job_name, fn, db) or 0
            job_log.finish(job_id, "SUCCESS", rows_processed=int(rows))
            db.commit()
            _LAST_STATUS[job_name] = "SUCCESS"
            logger.info("Job %s SUCCESS rows=%d", job_id, rows)
        except NoDataError as exc:
            # Source had no data (holiday, file not yet published, etc.).
            # This is NOT a system failure — record NO_DATA with a clear
            # message so operators understand why downstream jobs are skipped.
            msg = enrich_with_latest_in_db(db, job_name, str(exc))
            logger.warning("Job %s NO_DATA — %s", job_id, msg)
            try:
                db.rollback()
            except Exception:
                pass
            try:
                if db.connection is None:
                    db.connect()
                JobLogRepo(db).finish(job_id, "NO_DATA", error_message=msg[:1900])
                NotificationRepo(db).insert(Notification(
                    created_at=now_ist(),
                    notif_type="JOB_NO_DATA",
                    severity="WARNING",
                    title=f"No data: {job_name}",
                    body=msg[:500],
                ))
                db.commit()
            except Exception:
                logger.exception("Failed to record NO_DATA status for job %s", job_id)
            _LAST_STATUS[job_name] = "NO_DATA"
        except Exception as exc:
            err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            logger.exception("Job %s FAILED", job_id)
            try:
                db.rollback()
            except Exception:
                pass
            try:
                # If the watchdog severed the original connection, open a
                # fresh one to record the failure. The closed connection
                # will fail on `cursor()` so re-init only when needed.
                if db.connection is None:
                    db.connect()
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


def _record_skipped(
    job_id: str,
    job_name: str,
    reason: str,
    *,
    latest_for_job: str | None = None,
) -> None:
    """Open a fresh DB connection to record SKIPPED. Use when no
    connection is available (e.g. early in `_run_job` before connect)."""
    db = SQLServerConnection()
    try:
        db.connect()
        msg = reason
        if latest_for_job:
            msg = enrich_with_latest_in_db(db, latest_for_job, reason)
        _record_skipped_with_db(db, job_id, job_name, msg)
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
    return FoEodRepo(db).has_trade_date(effective_bhav_end_date())


def _probe_spot_bhav(db: SQLServerConnection) -> bool:
    from database.models import SpotEodRepo
    row = SpotEodRepo(db).for_date("NIFTY", effective_bhav_end_date())
    return bool(row)


def _probe_iv_calculation(db: SQLServerConnection) -> bool:
    from database.models import IvHistoryRepo
    return IvHistoryRepo(db).has_trade_date(effective_bhav_end_date())


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

def job_iv():         _run_job("iv_calculation",     run_iv_calculation)
def job_suggestion(): _run_job("suggestion_engine",  run_suggestion_engine)
def _live_suggestion_window_job(window_suffix: str):
    """One APScheduler trigger; logs under live_suggestion_engine with HHMM suffix."""

    def _fn() -> None:
        _run_job(
            "live_suggestion_engine",
            run_live_suggestion_engine,
            job_id_suffix=window_suffix,
        )

    _fn.__name__ = f"job_live_suggestion_{window_suffix}"
    return _fn


def job_live_suggestion() -> None:
    """Manual / default run (no window suffix)."""
    _run_job("live_suggestion_engine", run_live_suggestion_engine)
def job_simulation(): _run_job("simulation_update",  run_simulation_update)
def job_exit():       _run_job("exit_engine",        run_exit_engine)
def job_trade_greeks(): _run_job("trade_greeks_update", run_trade_greeks_update)
def job_events_seed(): _run_job("events_seed",       run_events_seed)
def job_scout_index_constituents(): _run_job("scout_index_constituents", run_scout_index_constituents)
def job_event_eve_review(): _run_job("event_eve_review", run_event_eve_review)


def job_intraday_close_snapshot():
    _run_job("intraday_close_snapshot", run_intraday_close_snapshot)


def job_drift_verifier():
    _run_job("drift_verifier", run_drift_verifier)


def job_intraday_validator():
    _run_job("intraday_validator", run_intraday_validator)


def _run_eod_pipeline_steps(label: str) -> int:
    """Run the full EOD chain sequentially (shared by nightly + morning catchup).

    Each step is attempted regardless of upstream status. Orchestrators
    handle missing data internally (skip/return 0); independent jobs (VIX,
    FII, simulation) always get a chance to run even when bhav is late.
    """
    summary: dict[str, str] = {}
    for step in _EOD_PIPELINE_STEPS:
        step_fn = JOB_FUNCS.get(step)
        if step_fn is None:
            logger.warning("%s: missing handler for %s", label, step)
            summary[step] = "MISSING"
            continue
        logger.info("%s — starting step %s", label, step)
        try:
            step_fn()
        except Exception:
            logger.exception("%s — step %s raised unexpectedly", label, step)
            summary[step] = "FAILED"
            continue
        summary[step] = _LAST_STATUS.get(step, "UNKNOWN")
        logger.info(
            "%s — finished step %s status=%s",
            label, step, summary[step],
        )
    logger.info("%s complete: %s", label, summary)
    return 0


def job_eod_nightly_pipeline():
    """Run the full EOD chain sequentially (Mon–Fri after evening VM boot)."""

    def _pipeline(db: SQLServerConnection) -> int:
        del db  # each step opens its own connection via _run_job
        with eod_pipeline_session(morning_catchup=False):
            return _run_eod_pipeline_steps("EOD pipeline")

    _run_job("eod_nightly_pipeline", _pipeline)


def job_morning_eod_catchup():
    """Run the EOD chain on morning VM boot if last night's run was missed.

    Scheduled at 09:00 IST (VM starts 08:55). Backfills the **prior trading
    session** bhav (Mon → Fri) before the 09:15 open. Steps are idempotent.
    """

    def _pipeline(db: SQLServerConnection) -> int:
        del db
        with eod_pipeline_session(morning_catchup=True):
            return _run_eod_pipeline_steps("Morning EOD catchup")

    _run_job("morning_eod_catchup", _pipeline)


def job_weekly_cleanup():
    """Apply retention policy and trim historical data."""
    from datetime import timedelta as _td
    from config import RETENTION_CONFIG

    def _cleanup(db: SQLServerConnection) -> int:
        from database.models import (
            FoEodRepo, SpotEodRepo, VixRepo, FiiRepo, IvHistoryRepo,
            SuggestionRepo, NotificationRepo,
            ChainTimeseriesRepo, AtmIvTimeseriesRepo, TradeMtmSnapshotRepo,
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
        n += ChainTimeseriesRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["chain_5min_keep_days"]))
        n += AtmIvTimeseriesRepo(db).delete_older_than(today - _td(days=RETENTION_CONFIG["atm_iv_5min_keep_days"]))
        mtm_repo = TradeMtmSnapshotRepo(db)
        n += mtm_repo.archive_non_active()
        n += mtm_repo.delete_history_older_than(
            today - _td(days=RETENTION_CONFIG["trade_mtm_snapshot_history_keep_days"]))
        db.commit()
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
    "live_suggestion_engine": job_live_suggestion,
    "simulation_update":       job_simulation,
    "exit_engine":        job_exit,
    "trade_greeks_update": job_trade_greeks,
    "events_seed":        job_events_seed,
    "scout_index_constituents": job_scout_index_constituents,
    "event_eve_review":   job_event_eve_review,
    "weekly_cleanup":     job_weekly_cleanup,
    "intraday_close_snapshot": job_intraday_close_snapshot,
    "drift_verifier":          job_drift_verifier,
    "intraday_validator":      job_intraday_validator,
    "eod_nightly_pipeline":    job_eod_nightly_pipeline,
    "morning_eod_catchup":     job_morning_eod_catchup,
}


def _schedule_window_suffix(trigger_kwargs: dict) -> str:
    return f"{int(trigger_kwargs['hour']):02d}{int(trigger_kwargs['minute']):02d}"


def build_scheduler() -> BackgroundScheduler:
    sch = BackgroundScheduler(timezone=SCHEDULER_CONFIG["timezone"])
    jobs = SCHEDULER_CONFIG["jobs"]
    pipeline_on = _eod_pipeline_enabled()
    for name, conf in jobs.items():
        if not conf.get("enabled", True):
            continue
        if pipeline_on and name in _EOD_JOBS_SUPERSEDED:
            logger.info(
                "Skipping individual schedule for %s (eod_nightly_pipeline enabled)",
                name,
            )
            continue

        schedules = conf.get("schedules")
        interval_minutes = conf.get("interval_minutes")
        if interval_minutes:
            fn = JOB_FUNCS.get(name)
            if fn is None:
                logger.warning("No handler for scheduled job %s", name)
                continue
            sch.add_job(
                fn,
                IntervalTrigger(minutes=int(interval_minutes)),
                id=name,
                name=name,
                misfire_grace_time=600,
                max_instances=1,
                replace_existing=True,
            )
            logger.info("Scheduled %s every %s minutes", name, interval_minutes)
            continue
        if schedules:
            fn = JOB_FUNCS.get(name)
            if fn is None:
                logger.warning("No handler for scheduled job %s", name)
                continue
            for slot in schedules:
                if not slot.get("enabled", True):
                    continue
                trigger_kwargs = {
                    k: v for k, v in slot.items() if k != "enabled"
                }
                win = _schedule_window_suffix(trigger_kwargs)
                trigger_id = f"{name}@{win}"
                slot_fn = (
                    _live_suggestion_window_job(win)
                    if name == "live_suggestion_engine"
                    else fn
                )
                sch.add_job(
                    slot_fn,
                    CronTrigger(**trigger_kwargs),
                    id=trigger_id,
                    name=name,
                    misfire_grace_time=600,
                    max_instances=1,
                    replace_existing=True,
                )
                logger.info("Scheduled %s (%s) @ %s", name, trigger_id, trigger_kwargs)
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
    manual_suffix = f"manual-{now_ist().strftime('%H%M%S')}"

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

    run_at = now_ist()
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


def _sweep_orphan_running_jobs() -> int:
    """Mark all RUNNING rows as FAILED on startup.

    Any row in RUNNING state when the scheduler process boots is by
    definition orphaned: the new process didn't start it, so the
    previous incarnation either crashed, was killed mid-job, or had a
    hung worker thread. We sweep BEFORE `sch.start()` so there's no
    race with a legit in-flight run.

    Without this sweep the dashboard shows a perpetual `RUNNING` and
    -- if the underlying transaction was uncommitted -- SQL Server
    holds locks indefinitely.
    """
    db = SQLServerConnection()
    try:
        db.connect()
        cur = db.execute(
            "UPDATE options_job_log "
            "SET status='FAILED', "
            "    finished_at=COALESCE(finished_at, started_at), "
            "    error_message=COALESCE(error_message, 'orphan-cleanup: scheduler restart') "
            "WHERE status='RUNNING'"
        )
        n = cur.rowcount or 0
        db.commit()
        if n:
            logger.warning("Cleared %d orphan RUNNING job_log row(s) on startup", n)
        return int(n)
    except Exception:
        logger.exception("Orphan-RUNNING sweep failed")
        return 0
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    global _SCHEDULER
    _sweep_orphan_running_jobs()
    sch = build_scheduler()
    sch.start()
    _SCHEDULER = sch
    logger.info("Scheduler started with %d jobs", len(sch.get_jobs()))
    return sch