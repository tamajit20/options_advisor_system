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
        # Seed default runtime flags (Phase 4). Existing rows are not
        # overwritten — operator toggles survive a re-init.
        from database.runtime_flags import RuntimeFlagsRepo
        RuntimeFlagsRepo(db).seed_defaults()
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


def _cmd_provider_status() -> int:
    """Print the active market-data providers + per-provider health.
    Used to verify that --OPT_PROVIDERS env wiring resolves to the expected
    adapter chain after a deploy."""
    from config import PROVIDERS_CONFIG
    from providers import list_active_providers, get_market_data

    active_env = PROVIDERS_CONFIG.get("active") or "(default: nse_eod)"
    primary = get_market_data()
    print(f"Configured OPT_PROVIDERS={active_env!r}")
    print(f"Primary provider:  {primary.name}")
    print(f"Capabilities:      {primary.capabilities()}")
    print("")
    print("Health snapshot:")
    rc = 0
    for h in list_active_providers():
        marker = "OK " if h.healthy else "FAIL"
        print(f"  [{marker}] {h.name:<10}  {h.detail}")
        if not h.healthy:
            rc = 3
    return rc


def _cmd_zerodha_login() -> int:
    """Interactive daily login flow for Zerodha Kite Connect.

    Prints the login URL, prompts for the `request_token` from the redirect URL,
    exchanges it for an access_token, and persists the session. Run once per
    morning (Kite tokens expire 06:00 IST daily)."""
    from config import ZERODHA_API_CONFIG
    from datetime import datetime, timedelta, timezone

    api_key = ZERODHA_API_CONFIG.get("api_key", "")
    api_secret = ZERODHA_API_CONFIG.get("api_secret", "")
    if not api_key or not api_secret:
        print("ERROR: OPT_ZERODHA_API_KEY and OPT_ZERODHA_API_SECRET must be set in env.")
        return 2

    try:
        from kiteconnect import KiteConnect  # type: ignore[import-not-found]
    except ImportError:
        print("ERROR: kiteconnect not installed. Run: pip install kiteconnect>=5.2")
        return 2

    kite = KiteConnect(api_key=api_key)
    print("")
    print("Step 1 — open this URL in your browser, log in, and approve the app:")
    print(f"    {kite.login_url()}")
    print("")
    print("Step 2 — after login you will be redirected to your registered URL")
    print("         with a 'request_token=XXXX' query parameter. Paste the value below.")
    print("")
    try:
        request_token = input("request_token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1
    if not request_token:
        print("ERROR: empty request_token")
        return 2

    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        print(f"ERROR: generate_session failed: {exc}")
        return 3

    from providers.zerodha.session import ZerodhaSession, save_session
    _IST = timezone(timedelta(hours=5, minutes=30))
    session = ZerodhaSession(
        api_key=api_key,
        access_token=data["access_token"],
        user_id=data.get("user_id", ""),
        generated_at=datetime.now(tz=_IST),
    )
    path = save_session(session)
    print(f"OK — session saved to {path}")
    print(f"     user_id={session.user_id}  generated_at={session.generated_at.isoformat()}")
    print("     Token is valid until 06:00 IST tomorrow.")
    return 0


def _cmd_zerodha_logout() -> int:
    """Clear the persisted Zerodha session (forces re-login on next start)."""
    from providers.zerodha.session import clear_session
    if clear_session():
        print("OK — Zerodha session cleared.")
        return 0
    print("No persisted Zerodha session found.")
    return 0


def _cmd_ws_runner() -> int:
    """Long-lived WebSocket runner. Streams Zerodha live ticks into the
    in-process cache + event bus. This is intended to be the entrypoint
    of a dedicated docker service (`stock_ws_runner`) — only ONE instance
    per Kite api_key is allowed.

    Phase 2b-i: WS runner core (this function bootstraps it).
    Phase 2b-ii: dynamic SubscriptionManager polls DB every 60s and
    pushes the union of (active trade legs + today's pending suggestion
    legs + index spots + VIX) into the runner.
    """
    from config import PROVIDERS_CONFIG, ZERODHA_API_CONFIG
    from database.connection import SQLServerConnection
    from database.runtime_flags import FLAG_KILL_SWITCH, RuntimeFlagsRepo
    from lifecycle.intraday_monitor import (
        IntradayMonitor,
        make_db_snapshot_loader,
    )
    from notifications import build_notifier
    from providers.cache import TTLCache
    from providers.event_bus import get_event_bus
    from providers.zerodha.facade import KiteFacade
    from providers.zerodha.instruments import InstrumentMaster
    from providers.zerodha.session import is_token_valid, load_session
    from providers.zerodha.subscription_manager import (
        SubscriptionManager,
        make_db_leg_loader,
    )
    from providers.zerodha.ws_runner import KiteWSRunner

    if (PROVIDERS_CONFIG.get("active") or "").strip().lower() != "zerodha":
        print("ERROR: --ws-runner requires OPT_PROVIDERS=zerodha")
        return 2
    if not ZERODHA_API_CONFIG.get("enabled", True):
        print("ERROR: OPT_ZERODHA_ENABLED=false — refusing to start WS runner")
        return 2

    session = load_session()
    if session is None or not is_token_valid(session):
        print("ERROR: no valid Zerodha session — run `python main.py --zerodha-login` first")
        return 2

    cache = TTLCache(default_ttl_seconds=PROVIDERS_CONFIG.get("live_cache_ttl_seconds", 5))
    bus = get_event_bus()

    runner = KiteWSRunner(
        api_key=session.api_key,
        access_token=session.access_token,
        cache=cache,
        event_bus=bus,
    )

    # Build the instrument master from a shared facade so the daily
    # 30k-row download happens exactly once per process.
    facade = KiteFacade(api_key=session.api_key, access_token=session.access_token)
    master = InstrumentMaster(loader=lambda: facade.instruments())

    db = SQLServerConnection()
    db.connect()

    flags_repo = RuntimeFlagsRepo(db)

    sub_manager = SubscriptionManager(
        runner=runner,
        instrument_master=master,
        leg_loader=make_db_leg_loader(db),
        interval_seconds=float(
            PROVIDERS_CONFIG.get("ws_subscription_interval_seconds", 60)
        ),
        kill_switch_fn=lambda: flags_repo.get_bool(FLAG_KILL_SWITCH, default=False),
    )

    # Phase 2b-iii — instant alerts. Subscribes to TOPIC_TICK and dispatches
    # SL_TRIGGER / PERFECT_CLOSURE / PERFECT_ENTRY via the notification router
    # (which itself respects sl_alerts / closure_alerts / opportunity_alerts
    # runtime flags).
    monitor = IntradayMonitor(
        notifier=build_notifier(db, provider="zerodha"),
        snapshot_loader=make_db_snapshot_loader(db),
        event_bus=bus,
    )

    print(f"Starting WS runner (user_id={session.user_id})")
    sub_manager.start()
    monitor.start()
    try:
        runner.start()
    except KeyboardInterrupt:
        runner.stop()
    finally:
        monitor.stop()
        sub_manager.stop()
        try:
            db.close()
        except Exception:
            pass
    status = runner.status()
    print(f"WS runner exited — final state={status.state.value}, last_error={status.last_error}")
    if status.state.value == "token_expired":
        return 2
    return 0


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
    parser.add_argument("--provider-status", action="store_true",
                        help="Show market-data provider configuration & health")
    parser.add_argument("--zerodha-login", action="store_true",
                        help="Interactive Zerodha Kite Connect daily login")
    parser.add_argument("--zerodha-logout", action="store_true",
                        help="Clear the persisted Zerodha session")
    parser.add_argument("--ws-runner", action="store_true",
                        help="Run the Zerodha WebSocket tick runner (long-lived, single instance)")
    parser.add_argument("--dashboard-only", action="store_true")
    parser.add_argument("--scheduler-only", action="store_true")
    args = parser.parse_args(argv)

    _setup_console_logging()

    if args.init_db:
        return _cmd_init_db()
    if args.check_db:
        return _cmd_check_db()
    if args.provider_status:
        return _cmd_provider_status()
    if args.zerodha_login:
        return _cmd_zerodha_login()
    if args.zerodha_logout:
        return _cmd_zerodha_logout()
    if args.ws_runner:
        return _cmd_ws_runner()
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
