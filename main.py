"""
options_advisor_system / main.py
================================

Entry point. Examples:

    python main.py --init-db          # create database + all tables
    python main.py --check-db         # verify DB connectivity
    python main.py                    # run scheduler + dashboard (default)
    python main.py --dashboard-only   # run only the Flask dashboard
    python main.py --scheduler-only   # run only APScheduler

This module orchestrates startup; it is NOT a place for business logic.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

# Ensure repo root is on sys.path when invoked directly
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import LOGGING_CONFIG, DASHBOARD_CONFIG  # noqa: E402

logger = logging.getLogger("options_advisor")


def _setup_console_logging() -> None:
    """Bootstrap console logging only. DB-based logging is wired up after
    the database connection is established (see database.log_repo)."""
    level = getattr(logging, LOGGING_CONFIG["console_level"].upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format=LOGGING_CONFIG["format"],
    )


def _install_db_logging() -> None:
    try:
        from database.log_repo import install_db_logging
        install_db_logging()
        logger.info("DB logging handler installed")
    except Exception:
        logger.exception("Could not install DB logging handler")


def _cmd_init_db() -> int:
    from database.connection import SQLServerConnection
    from database.schema import create_database_if_missing, create_all_tables

    logger.info("Initialising OptionsAdvisorDB ...")
    create_database_if_missing()
    db = SQLServerConnection()
    db.connect()
    try:
        create_all_tables(db)
        db.commit()
        logger.info("Database initialised successfully.")
        return 0
    except Exception as exc:
        db.rollback()
        logger.exception("DB init failed: %s", exc)
        return 1
    finally:
        db.close()


def _cmd_check_db() -> int:
    from database.connection import SQLServerConnection

    db = SQLServerConnection()
    try:
        db.connect()
        ver = db.scalar("SELECT @@VERSION")
        logger.info("DB OK — %s", (ver or "")[:80])
        return 0
    except Exception as exc:
        logger.exception("DB check failed: %s", exc)
        return 2
    finally:
        db.close()


def _run_dashboard() -> None:
    from dashboard.server import create_app

    app = create_app()
    app.run(
        host=DASHBOARD_CONFIG["host"],
        port=DASHBOARD_CONFIG["port"],
        debug=DASHBOARD_CONFIG["debug"],
        use_reloader=False,
    )


def _run_scheduler(stop_event: threading.Event) -> None:
    from scheduler.scheduler import start_scheduler

    sched = start_scheduler()
    logger.info("Scheduler started.")
    try:
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def _seed_events_on_startup() -> None:
    """Seed EVENTS_CONFIG into options_events_calendar on every startup.
    Non-fatal: a failure here does not prevent the app from running."""
    try:
        from database.connection import SQLServerConnection
        from lifecycle.events_seeder import run_events_seed
        db = SQLServerConnection()
        db.connect()
        try:
            n = run_events_seed(db)
            if n:
                logger.info("Startup events seed: inserted %d new events", n)
        finally:
            db.close()
    except Exception:
        logger.exception("Startup events seed failed (non-fatal)")


def _run_full() -> int:
    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Signal %s received, shutting down ...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    _seed_events_on_startup()

    sched_thread = threading.Thread(
        target=_run_scheduler, args=(stop_event,), name="scheduler", daemon=True
    )
    sched_thread.start()

    try:
        _run_dashboard()
    finally:
        stop_event.set()
        sched_thread.join(timeout=5)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="options_advisor")
    parser.add_argument("--init-db", action="store_true", help="Create DB + tables")
    parser.add_argument("--check-db", action="store_true", help="Verify DB connectivity")
    parser.add_argument("--dashboard-only", action="store_true")
    parser.add_argument("--scheduler-only", action="store_true")
    args = parser.parse_args(argv)

    _setup_console_logging()

    if args.init_db:
        return _cmd_init_db()
    if args.check_db:
        return _cmd_check_db()
    _install_db_logging()
    if args.dashboard_only:
        _run_dashboard()
        return 0
    if args.scheduler_only:
        stop = threading.Event()

        def _h(signum, _f):
            stop.set()

        signal.signal(signal.SIGINT, _h)
        signal.signal(signal.SIGTERM, _h)
        _run_scheduler(stop)
        return 0
    return _run_full()


if __name__ == "__main__":
    sys.exit(main())
