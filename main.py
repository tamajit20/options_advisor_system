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


def _cmd_backfill_index_spot(days) -> int:
    from database.connection import SQLServerConnection
    from lifecycle.index_spot_backfill import run_index_spot_backfill

    db = SQLServerConnection()
    db.connect()
    try:
        n = run_index_spot_backfill(db, days=days)
        logger.info("Index spot backfill complete: %d rows upserted", n)
        return 0
    except Exception:
        logger.exception("Index spot backfill failed")
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
    try:
        from providers.zerodha.session import build_login_url, exchange_request_token
    except ImportError:
        print("ERROR: kiteconnect not installed. Run: pip install kiteconnect>=5.2")
        return 2

    try:
        login_url = build_login_url()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2
    except ImportError:
        print("ERROR: kiteconnect not installed. Run: pip install kiteconnect>=5.2")
        return 2

    print("")
    print("Step 1 — open this URL in your browser, log in, and approve the app:")
    print(f"    {login_url}")
    print("")
    print("Step 2 — after login you will be redirected to your registered URL")
    print("         with a 'request_token=XXXX' query parameter. Paste the value below.")
    from config import DASHBOARD_CONFIG
    base = (DASHBOARD_CONFIG.get("public_base_url") or "").strip().rstrip("/")
    if not base:
        base = f"http://127.0.0.1:{DASHBOARD_CONFIG['port']}"
    print(f"         Kite redirect URL for this deploy: {base}/zerodha/callback")
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
        session = exchange_request_token(request_token)
    except Exception as exc:
        print(f"ERROR: generate_session failed: {exc}")
        return 3

    print(f"OK — session saved.")
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


def _run_ws_runner_once(session, stop_event, bus, latest_spots: dict) -> str:
    """Run one WS session until logout, token rotation, or WS disconnect.

    Returns a reason string: ``logout``, ``token_rotated``, ``process_restart``,
    ``token_expired``, or ``stopped``.
    """
    from config import PROVIDERS_CONFIG, STRATEGY_CONFIG
    from database.connection import SQLServerConnection
    from database.models import EventCalendarRepo, TradeLevelEventRepo, TradeMtmSnapshotRepo, TradeRepo
    from database.runtime_flags import FLAG_KILL_SWITCH, RuntimeFlagsRepo
    from lifecycle.chain_aggregator import ChainTickAggregator
    from lifecycle.intraday_monitor import IntradayMonitor, make_db_snapshot_loader
    from lifecycle.live_risk_monitor import (
        LiveRiskMonitor,
        make_db_snapshot_loader as make_db_risk_snapshot_loader,
    )
    from lifecycle.opportunity_regen_watcher import OpportunityRegenWatcher
    from notifications import build_notifier
    from providers.cache import TTLCache
    from providers.zerodha.facade import KiteFacade
    from providers.zerodha.instruments import InstrumentMaster
    from providers.zerodha.session import load_session as _load_sess
    from providers.zerodha.subscription_manager import (
        SubscriptionManager,
        make_db_leg_loader,
        make_watchlist_leg_loader,
        merge_leg_loaders,
    )
    from providers.zerodha.ws_runner import KiteWSRunner, _ProcessRestartRequired
    from providers.ws_monitor import WSMonitor, default_snapshot_path
    from providers.ws_watchdog import WSWatchdog

    cache = TTLCache(default_ttl_seconds=PROVIDERS_CONFIG.get("live_cache_ttl_seconds", 5))

    runner = KiteWSRunner(
        api_key=session.api_key,
        access_token=session.access_token,
        cache=cache,
        event_bus=bus,
    )

    facade = KiteFacade(api_key=session.api_key, access_token=session.access_token)
    master = InstrumentMaster(loader=lambda: facade.instruments())

    db = SQLServerConnection()
    db.connect()

    flags_repo = RuntimeFlagsRepo(db)

    sub_manager = SubscriptionManager(
        runner=runner,
        instrument_master=master,
        leg_loader=merge_leg_loaders(
            make_db_leg_loader(db),
            make_watchlist_leg_loader(
                master,
                underlyings=STRATEGY_CONFIG.get("underlyings", []),
                spot_lookup=lambda s: latest_spots.get(s),
                band_pct=PROVIDERS_CONFIG.get("watchlist_band_pct", 0.05),
                expiries_per_underlying=int(
                    PROVIDERS_CONFIG.get("watchlist_expiries_per_underlying", 2)
                ),
            ),
        ),
        interval_seconds=float(
            PROVIDERS_CONFIG.get("ws_subscription_interval_seconds", 60)
        ),
        kill_switch_fn=lambda: flags_repo.get_bool(FLAG_KILL_SWITCH, default=False),
    )

    def _expiries_for(sym: str):
        from datetime import date as _d
        today = _d.today()
        return [e for e in master.list_expiries(sym) if e >= today][:2]

    chain_aggregator = ChainTickAggregator(
        db=db,
        expiry_provider=_expiries_for,
        event_bus=bus,
    )

    def _prime_legs(keys):
        out = {}
        sym_to_key = {}
        for (underlying, expiry, strike, otype) in keys:
            inst = master.get_option(underlying, expiry, float(strike), otype)
            if inst is None:
                continue
            sym = f"{inst.exchange}:{inst.tradingsymbol}"
            sym_to_key[sym] = (underlying, expiry, strike, otype)
        if not sym_to_key:
            return out
        try:
            quotes = facade.ltp(list(sym_to_key.keys())) or {}
        except Exception:
            return out
        for sym, payload in quotes.items():
            ltp = (payload or {}).get("last_price")
            if ltp is None:
                continue
            key = sym_to_key.get(sym)
            if key is not None:
                out[key] = float(ltp)
        return out

    def _persist_mtm_snapshot(payload: dict) -> None:
        TradeMtmSnapshotRepo(db).upsert_hourly(payload)
        db.commit()

    def _persist_level_event(payload: dict) -> None:
        TradeLevelEventRepo(db).insert(payload)
        db.commit()

    live_risk_monitor = LiveRiskMonitor(
        notifier=build_notifier(db, provider="zerodha"),
        snapshot_loader=make_db_risk_snapshot_loader(db),
        prime_loader=_prime_legs,
        event_bus=bus,
        trailing_persister=lambda tid, floor, idx: TradeRepo(db).update_trailing(
            tid, trailing_pnl_floor=floor, trailing_step_idx=idx),
        mtm_snapshot_persister=_persist_mtm_snapshot,
        level_event_persister=_persist_level_event,
        events_repo=EventCalendarRepo(db),
    )

    monitor = IntradayMonitor(
        notifier=build_notifier(db, provider="zerodha"),
        snapshot_loader=make_db_snapshot_loader(db),
        event_bus=bus,
    )

    regen_watcher = OpportunityRegenWatcher(
        notifier=build_notifier(db, provider="zerodha"),
        event_bus=bus,
    )

    ws_monitor = WSMonitor(
        snapshot_path=default_snapshot_path(),
        event_bus=bus,
        provider="zerodha",
        status_fn=runner.status,
    )

    ws_watchdog = WSWatchdog(
        snapshot_fn=ws_monitor.snapshot,
        notifier=build_notifier(db, provider="zerodha"),
        event_bus=bus,
    )

    exit_reason = {"reason": "stopped"}

    def _watch_session():
        my_token = session.access_token
        while not runner._stop_event.is_set():  # type: ignore[attr-defined]
            try:
                cur = _load_sess()
                if cur is None:
                    print("Session file removed — stopping WS runner (logout).")
                    exit_reason["reason"] = "logout"
                    runner.stop()
                    return
                if cur.access_token != my_token:
                    print("Session token rotated — reconnecting with new token.")
                    exit_reason["reason"] = "token_rotated"
                    runner.stop()
                    return
            except Exception:
                pass
            if runner._stop_event.wait(5.0):  # type: ignore[attr-defined]
                return

    print(f"Starting WS runner (user_id={session.user_id})")
    sub_manager.start()
    monitor.start()
    regen_watcher.start()
    ws_monitor.start()
    ws_watchdog.start()
    chain_aggregator.start()
    live_risk_monitor.start()

    import threading as _threading
    _threading.Thread(target=_watch_session, name="zerodha-session-watch", daemon=True).start()

    try:
        runner.start()
    except KeyboardInterrupt:
        runner.stop()
        exit_reason["reason"] = "stopped"
    except _ProcessRestartRequired:
        runner.stop()
        exit_reason["reason"] = "process_restart"
    finally:
        live_risk_monitor.stop()
        chain_aggregator.stop()
        ws_watchdog.stop()
        ws_monitor.stop()
        regen_watcher.stop()
        monitor.stop()
        sub_manager.stop()
        try:
            db.close()
        except Exception:
            pass

    status = runner.status()
    print(
        f"WS runner exited — final state={status.state.value}, "
        f"last_error={status.last_error}, reason={exit_reason['reason']}"
    )
    if status.state.value == "token_expired":
        return "token_expired"
    if stop_event.is_set() and exit_reason["reason"] == "stopped":
        return "stopped"
    return exit_reason["reason"]


def _cmd_ws_runner() -> int:
    """Long-lived WebSocket runner. Streams Zerodha live ticks into the
    in-process cache + event bus. This is intended to be the entrypoint
    of a dedicated docker service (`stock_ws_runner`) — only ONE instance
    per Kite api_key is allowed.

    Waits for a valid Zerodha session when none is present (dashboard login),
    reconnects automatically after token rotation, and survives WS disconnects
    without manual ``docker compose restart``.
    """
    import signal
    import threading

    from config import PROVIDERS_CONFIG, ZERODHA_API_CONFIG
    from providers.event_bus import get_event_bus
    from providers.zerodha.session import is_token_valid, load_session
    from providers.ws_monitor import default_snapshot_path, write_idle_snapshot

    if (PROVIDERS_CONFIG.get("active") or "").strip().lower() != "zerodha":
        print("ERROR: --ws-runner requires OPT_PROVIDERS=zerodha")
        return 2
    if not ZERODHA_API_CONFIG.get("enabled", True):
        print("ERROR: OPT_ZERODHA_ENABLED=false — refusing to start WS runner")
        return 2

    stop_event = threading.Event()

    def _on_sig(signum, _frame):
        print(f"Signal {signum} received — shutting down WS runner service.")
        stop_event.set()

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    bus = get_event_bus()
    latest_spots: dict = {}

    def _capture_spot(quote) -> None:
        if quote is None or quote.option_type is not None:
            return
        try:
            latest_spots[quote.symbol] = float(quote.last_price)
        except (TypeError, ValueError):
            pass

    bus.subscribe("tick", _capture_spot)
    snapshot_path = default_snapshot_path()

    while not stop_event.is_set():
        session = load_session()
        if session is None or not is_token_valid(session):
            write_idle_snapshot(
                snapshot_path,
                detail="waiting for Zerodha login via dashboard",
            )
            print(
                "Waiting for valid Zerodha session — paste token in dashboard "
                "WS Monitor tab."
            )
            if stop_event.wait(5.0):
                return 0
            continue

        reason = _run_ws_runner_once(session, stop_event, bus, latest_spots)
        if stop_event.is_set() or reason == "stopped":
            return 0
        if reason in ("logout", "token_rotated", "process_restart", "token_expired"):
            continue
        return 0

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


def _ensure_schema_on_startup() -> None:
    """Create any missing tables (idempotent). Safe on every container start."""
    try:
        from database.connection import SQLServerConnection
        from database.schema import create_all_tables

        db = SQLServerConnection()
        db.connect()
        try:
            create_all_tables(db)
            db.commit()
            logger.info("Startup schema ensure completed (options + scout tables).")
        finally:
            db.close()
    except Exception:
        logger.exception("Startup schema ensure failed (non-fatal)")


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

    _ensure_schema_on_startup()
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
    parser.add_argument(
        "--backfill-index-spot",
        action="store_true",
        help="Backfill index OHLC into options_spot_eod (NSE + Zerodha if logged in)",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=None,
        help="Calendar days of history for --backfill-index-spot (default from config)",
    )
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
    if args.backfill_index_spot:
        return _cmd_backfill_index_spot(args.backfill_days)
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
