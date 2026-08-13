"""
scout/routes.py — Flask blueprint for Intraday Scout (/api/scout/*).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable, Optional

from flask import Blueprint, jsonify, request

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import (
    ScoutSignalRepo,
    ScoutTradeOrderRepo,
    ScoutTradeRepo,
    ScoutZerodhaLogRepo,
)
from scout.config_loader import (
    default_watchlist,
    get_automation,
    get_scout_settings,
    get_watchlist,
    invalidate_automation_cache,
    invalidate_settings_cache,
    invalidate_watchlist_cache,
    reload_scout_settings,
    set_automation,
    set_scout_settings,
    watchlist_set,
)
from scout.settings_schema import format_square_off_time, suggested_quantity
from scout.index_groups import INDEX_GROUPS, index_tags, nifty_bank_symbols
from scout.instruments import (
    ScoutInstrumentError,
    equity_rows_for_symbols,
    nse_equity_universe,
    nifty50_symbols,
    refresh_nse_equity_master,
)
from scout.live_quotes import latest_equity_ltps
from scout.market_data import zerodha_ready
from scout.signal_enrichment import (
    build_exit_plan,
    enrich_signal,
    evaluate_exit_alerts,
    scout_trade_mtm,
)
from scout.execution_engine import (
    execution_mode_label,
    place_protection_and_target,
    retry_unprotected_trades,
    zerodha_execute_enabled,
)
from scout.execution_health import build_execution_health
from scout.execution_flow import build_flow_items, build_trade_execution_flow
from scout.trade_audit import build_entry_audit, enrich_history_trade
from scout.utils import is_market_open
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)


def _coerce_int_id(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _signal_trigger_ts(signal: dict) -> float:
    raw = str(signal.get("triggered_at") or "")
    try:
        return datetime.fromisoformat(raw.replace(" ", "T")).timestamp()
    except ValueError:
        return 0.0


def _signal_display_sort_key(signal: dict) -> tuple:
    """Open opportunities first; executed/blocked signals later (newest first within tier)."""
    if signal.get("can_mark_taken"):
        tier = 0
    elif signal.get("trade_open"):
        tier = 1
    else:
        tier = 2
    return tier, -_signal_trigger_ts(signal)


def _sort_signals_for_display(signals: list) -> list:
    return sorted(signals, key=_signal_display_sort_key)

scout_bp = Blueprint("scout", __name__, url_prefix="/api/scout")


def _with_db(fn: Callable):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        db = SQLServerConnection()
        db.connect()
        try:
            return fn(db, *args, **kwargs)
        finally:
            db.close()
    wrapper.__name__ = fn.__name__
    return wrapper


def _parse_date_window(from_str: str, to_str: str, *, days_default: int = 30):
    today = today_ist()
    if from_str and to_str:
        return from_str[:10], to_str[:10]
    if from_str:
        return from_str[:10], today.isoformat()
    if to_str:
        start = today - timedelta(days=days_default)
        return start.isoformat(), to_str[:10]
    start = today - timedelta(days=days_default)
    return start.isoformat(), today.isoformat()


def _format_square_off_time(settings: dict) -> str:
    return format_square_off_time(settings)


@scout_bp.route("/flow")
@_with_db
def api_scout_flow(db: SQLServerConnection):
    """Unified execution view — signals awaiting entry + active trades with order flow."""
    settings = get_scout_settings(db)
    items = build_flow_items(db, settings=settings)
    return jsonify({
        "items": items,
        "count": len(items),
        "zerodha_execute_orders": zerodha_execute_enabled(settings),
        "execution_mode": execution_mode_label(settings),
        "square_off_time": _format_square_off_time(settings),
        "poll_seconds": int(SCOUT_CONFIG.get("signals_poll_seconds", 10)),
        "market_open": is_market_open(),
    })


@scout_bp.route("/status")
@_with_db
def api_scout_status(db: SQLServerConnection):
    ok, msg = zerodha_ready()
    last_sig = ScoutSignalRepo(db).last_signal()
    selected = get_watchlist(db)
    settings = get_scout_settings(db)
    automation = get_automation(db)
    trade_repo = ScoutTradeRepo(db)
    health = build_execution_health(db, settings, fetch_wallet=zerodha_execute_enabled(settings))
    try:
        from providers.zerodha.permission_check import latest_check_from_db, last_permission_summary, overlay_live_websocket_check
        perm = overlay_live_websocket_check(latest_check_from_db(db) or last_permission_summary())
    except Exception:
        perm = None
    return jsonify({
        "enabled": bool(SCOUT_CONFIG.get("enabled", True)),
        "mode": "websocket",
        "market_open": is_market_open(),
        "zerodha_ok": ok,
        "zerodha_message": msg,
        "zerodha_execute_orders": zerodha_execute_enabled(settings),
        "execution_mode": execution_mode_label(settings),
        "square_off_time": _format_square_off_time(settings),
        "watchlist_count": len(selected),
        "push_enabled": bool(SCOUT_CONFIG.get("push_enabled", True)),
        "signals_poll_seconds": int(SCOUT_CONFIG.get("signals_poll_seconds", 10)),
        "signals_live_poll_seconds": int(SCOUT_CONFIG.get("signals_live_poll_seconds", 3)),
        "signal_valid_minutes": int(settings.get("signal_valid_minutes", 30)),
        "auto_close_poll_seconds": int(settings.get("auto_close_poll_seconds", 10)),
        "automation": automation,
        "settings": settings,
        "trades_opened_today": trade_repo.count_trades_opened_today(),
        "last_signal": last_sig,
        "health": health,
        "wallet": health.get("wallet"),
        "zerodha_permissions": perm,
    })


@scout_bp.route("/health")
@_with_db
def api_scout_health(db: SQLServerConnection):
    settings = get_scout_settings(db)
    health = build_execution_health(db, settings, fetch_wallet=True)
    return jsonify(health)


@scout_bp.route("/signals")
@_with_db
def api_scout_signals(db: SQLServerConnection):
    limit = min(int(request.args.get("limit", 50)), 200)
    default_since = int(SCOUT_CONFIG.get("signal_display_minutes", 120))
    since = min(int(request.args.get("since_minutes", default_since)), 24 * 60)
    rows = ScoutSignalRepo(db).recent(limit=limit, since_minutes=since)
    trade_repo = ScoutTradeRepo(db)
    settings = get_scout_settings(db)
    open_trades = trade_repo.open_trades()
    trade_by_signal = {
        sid: int(t["id"])
        for t in open_trades
        if (sid := _coerce_int_id(t.get("signal_id"))) is not None
    }
    open_ids = set(trade_by_signal.keys())
    open_trade_by_symbol: dict[str, dict] = {}
    for t in open_trades:
        sym_key = str(t.get("symbol") or "").upper()
        if not sym_key or sym_key in open_trade_by_symbol:
            continue
        open_trade_by_symbol[sym_key] = {
            "trade_id": int(t["id"]),
            "signal_id": _coerce_int_id(t.get("signal_id")),
        }

    symbols = {str(r["symbol"]).upper() for r in rows}
    quotes = latest_equity_ltps(symbols)
    now = now_ist().replace(tzinfo=None)

    enriched = []
    for row in rows:
        sid_int = _coerce_int_id(row.get("id"))
        has_open_trade = sid_int is not None and sid_int in open_ids
        sym = str(row["symbol"]).upper()
        q = quotes.get(sym, {})
        live_ltp = q.get("ltp")
        e = enrich_signal(
            row,
            live_ltp=live_ltp,
            live_as_of=q.get("as_of"),
            now=now,
            settings=settings,
        )
        ref_px = live_ltp if live_ltp is not None and live_ltp > 0 else float(row.get("ltp") or 0)
        e["suggested_quantity"] = suggested_quantity(settings, ref_px)
        e["trade_open"] = has_open_trade
        e["trade_id"] = trade_by_signal.get(sid_int) if has_open_trade else None
        sym_open = open_trade_by_symbol.get(sym)
        symbol_blocked = (
            sym_open is not None
            and not has_open_trade
        )
        e["symbol_trade_blocked"] = symbol_blocked
        e["can_mark_taken"] = not has_open_trade and not symbol_blocked
        if symbol_blocked:
            e["blocking_trade_id"] = sym_open.get("trade_id")
            e["blocking_signal_id"] = sym_open.get("signal_id")
        else:
            e["blocking_trade_id"] = None
            e["blocking_signal_id"] = None
        from scout.auto_enter_status import evaluate_auto_enter_status

        e["auto_enter"] = evaluate_auto_enter_status(
            signal=row,
            enriched=e,
            settings=settings,
            trade_repo=trade_repo,
            market_open=is_market_open(),
            has_open_trade=has_open_trade,
            symbol_trade_blocked=symbol_blocked,
        )
        if e.get("validity_status") == "ACTIVE" or has_open_trade:
            enriched.append(e)

    enriched = _sort_signals_for_display(enriched)

    return jsonify({
        "signals": enriched,
        "count": len(enriched),
        "poll_seconds": int(SCOUT_CONFIG.get("signals_poll_seconds", 10)),
        "live_poll_seconds": int(SCOUT_CONFIG.get("signals_live_poll_seconds", 3)),
        "market_open": is_market_open(),
    })


@scout_bp.route("/live-quotes")
def api_scout_live_quotes():
    """Latest equity LTP from ws_status.json — no DB, for fast UI ticks."""
    raw = (request.args.get("symbols") or "").strip()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()] or None
    quotes = latest_equity_ltps(symbols)
    return jsonify({
        "quotes": quotes,
        "live_poll_seconds": int(SCOUT_CONFIG.get("signals_live_poll_seconds", 3)),
    })


@scout_bp.route("/watchlist")
@_with_db
def api_scout_watchlist_get(db: SQLServerConnection):
    search = (request.args.get("search") or "").strip()
    offset = max(int(request.args.get("offset", 0)), 0)
    limit = min(max(int(request.args.get("limit", 80)), 1), 500)
    force = request.args.get("refresh", "").lower() in ("1", "true", "yes")

    selected = set(get_watchlist(db))
    default = set(default_watchlist())
    nifty50 = nifty50_symbols()
    nifty_bank = nifty_bank_symbols()

    zerodha_ok = True
    notice = None
    try:
        page, total, refreshed_at = nse_equity_universe(
            search=search,
            offset=offset,
            limit=limit,
            force_refresh=force,
        )
    except ScoutInstrumentError as exc:
        zerodha_ok = False
        notice = str(exc)
        refreshed_at = None
        if search:
            page, total = [], 0
        else:
            fallback = [
                {
                    "symbol": sym,
                    "name": "",
                    "is_nifty50": True,
                    "index_tags": index_tags(sym),
                }
                for sym in nifty50
            ]
            total = len(fallback)
            page = fallback[offset: offset + limit]

    stocks = []
    for row in page:
        sym = row["symbol"]
        stocks.append({
            **row,
            "index_tags": row.get("index_tags") or index_tags(sym),
            "selected": sym in selected,
            "is_default": sym in default,
        })

    selected_stocks: list = []
    if zerodha_ok and not search:
        on_page = {r["symbol"] for r in stocks}
        missing = sorted(sym for sym in selected if sym not in on_page)
        if missing:
            try:
                selected_stocks = equity_rows_for_symbols(missing)
            except ScoutInstrumentError:
                selected_stocks = []

    return jsonify({
        "stocks": stocks,
        "selected_stocks": selected_stocks,
        "selected": sorted(selected),
        "selected_count": len(selected),
        "total_equity_count": total,
        "nifty50": nifty50,
        "nifty50_count": len(nifty50),
        "nifty_bank": nifty_bank,
        "nifty_bank_count": len(nifty_bank),
        "index_groups": INDEX_GROUPS,
        "search": search,
        "offset": offset,
        "limit": limit,
        "instrument_refreshed_at": refreshed_at,
        "zerodha_ok": zerodha_ok,
        "notice": notice,
    })


@scout_bp.route("/automation")
@_with_db
def api_scout_automation_get(db: SQLServerConnection):
    return jsonify(get_automation(db))


@scout_bp.route("/automation", methods=["PUT"])
@_with_db
def api_scout_automation_put(db: SQLServerConnection):
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    cleaned = set_automation(db, body)
    db.commit()
    return jsonify({"status": "ok", "automation": cleaned})


@scout_bp.route("/settings")
@_with_db
def api_scout_settings_get(db: SQLServerConnection):
    settings = get_scout_settings(db)
    trade_repo = ScoutTradeRepo(db)
    return jsonify({
        "settings": settings,
        "trades_opened_today": trade_repo.count_trades_opened_today(),
    })


@scout_bp.route("/settings", methods=["PUT"])
@_with_db
def api_scout_settings_put(db: SQLServerConnection):
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    current = reload_scout_settings(db)
    merged = {**current, **body}
    cleaned = set_scout_settings(db, merged)
    db.commit()
    return jsonify({"status": "ok", "settings": cleaned})


@scout_bp.route("/watchlist/refresh-instruments", methods=["POST"])
@_with_db
def api_scout_watchlist_refresh(db: SQLServerConnection):
    try:
        count = refresh_nse_equity_master()
    except ScoutInstrumentError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"status": "ok", "instrument_count": count})


@scout_bp.route("/watchlist", methods=["PUT"])
@_with_db
def api_scout_watchlist_put(db: SQLServerConnection):
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols")
    if not isinstance(symbols, list):
        return jsonify({"error": "symbols must be a list"}), 400
    cleaned = watchlist_set(db, symbols)
    db.commit()
    invalidate_watchlist_cache()
    return jsonify({
        "status": "ok",
        "selected": cleaned,
        "selected_count": len(cleaned),
    })


@scout_bp.route("/trades/open")
@_with_db
def api_scout_trades_open(db: SQLServerConnection):
    trade_repo = ScoutTradeRepo(db)
    sig_repo = ScoutSignalRepo(db)
    settings = get_scout_settings(db)
    rows = trade_repo.open_trades()
    symbols = {str(r["symbol"]).upper() for r in rows}
    quotes = latest_equity_ltps(symbols)
    now = now_ist().replace(tzinfo=None)
    out = []
    for r in rows:
        row = dict(r)
        sid = row.get("signal_id")
        if sid:
            row["signal"] = sig_repo.get(int(sid))
        else:
            row["signal"] = None
        sym = str(row["symbol"]).upper()
        q = quotes.get(sym, {})
        ltp = q.get("ltp")
        mtm = scout_trade_mtm(row, ltp)
        if mtm:
            row.update(mtm)
            row["live_as_of"] = q.get("as_of")
        sig = row.get("signal") or {
            "action": row.get("action"),
            "invalidation": None,
            "signal_type": row.get("signal_type"),
            "meta": {},
        }
        row["exit_plan"] = build_exit_plan(
            sig,
            entry_price=float(row.get("entry_price") or 0),
            executed_at=row.get("executed_at"),
            live_ltp=ltp,
            now=now,
            settings=settings,
        )
        row["exit_alerts"] = evaluate_exit_alerts(
            action=str(row.get("action") or ""),
            live_ltp=ltp,
            exit_plan=row["exit_plan"],
            entry_price=float(row.get("entry_price") or 0),
            peak_price=row.get("peak_price"),
            settings=settings,
        )
        orders = ScoutTradeOrderRepo(db).for_trade(int(row["id"]))
        row["execution"] = build_trade_execution_flow(
            trade=row,
            signal=row.get("signal"),
            orders=orders,
            live_ltp=ltp,
            settings=get_scout_settings(db),
        )
        out.append(row)
    return jsonify({
        "trades": out,
        "count": len(out),
        "poll_seconds": int(SCOUT_CONFIG.get("signals_poll_seconds", 10)),
    })


@scout_bp.route("/signals/<int:signal_id>/mark-taken", methods=["POST"])
@_with_db
def api_scout_mark_taken(db: SQLServerConnection, signal_id: int):
    """Record execution — enter fill price/qty from your Zerodha order (like mark-executed)."""
    body = request.get_json(silent=True) or {}
    settings = get_scout_settings(db)
    entry_price = body.get("entry_price")

    sig_repo = ScoutSignalRepo(db)
    trade_repo = ScoutTradeRepo(db)
    sig = sig_repo.get(signal_id)
    if not sig:
        return jsonify({"error": "signal not found"}), 404

    open_ids = trade_repo.open_signal_ids()
    if signal_id in open_ids:
        return jsonify({"error": "open trade already exists for this signal"}), 409

    sym_upper = str(sig["symbol"]).upper()
    for t in trade_repo.open_trades():
        if str(t.get("symbol") or "").upper() != sym_upper:
            continue
        existing_sid = _coerce_int_id(t.get("signal_id"))
        if existing_sid is not None and existing_sid != signal_id:
            return jsonify({
                "error": (
                    f"Open trade already exists for {sig['symbol']} "
                    f"(TRD #{t['id']}, SIG #{existing_sid}). Close or void it first."
                ),
            }), 409

    quotes = latest_equity_ltps([sig["symbol"]])
    q = quotes.get(str(sig["symbol"]).upper(), {})
    enriched = enrich_signal(
        sig,
        live_ltp=q.get("ltp"),
        now=now_ist().replace(tzinfo=None),
        settings=settings,
    )
    if enriched.get("validity_status") != "ACTIVE":
        return jsonify({
            "error": f"Signal is no longer valid ({enriched.get('validity_status')})",
            "validity_status": enriched.get("validity_status"),
        }), 410

    fill = float(entry_price) if entry_price is not None else float(sig["ltp"])
    if fill <= 0:
        return jsonify({"error": "entry_price must be positive"}), 400

    if body.get("quantity") is None:
        quantity = suggested_quantity(settings, fill)
    else:
        quantity = int(body.get("quantity") or 1)
    if quantity < 1:
        return jsonify({"error": "quantity must be >= 1"}), 400

    mode = execution_mode_label()
    tid = trade_repo.mark_taken(
        signal_id=signal_id,
        symbol=str(sig["symbol"]),
        action=str(sig["action"]),
        signal_type=str(sig.get("signal_type") or ""),
        entry_price=fill,
        quantity=quantity,
        executed_at=now_ist(),
        execution_mode="manual",
        notes=build_entry_audit(
            sig,
            entry_price=fill,
            executed_at=now_ist(),
            mode="manual",
            source="manual",
        ),
    )
    trade = dict(trade_repo.get(tid) or {})
    # Manual mark-taken = operator already filled on Zerodha outside Scout.
    # DB-only protection/target (live=False) is intentional — not the 3-step auto flow.
    place_protection_and_target(
        db,
        trade=trade,
        signal=sig,
        entry_price=fill,
        settings=settings,
        live=False,
    )
    # Manual entry: record simulated Step 1 entry order for UI flow
    order_repo = ScoutTradeOrderRepo(db)
    if not order_repo.get_leg(tid, "ENTRY"):
        order_repo.insert(
            trade_id=tid,
            step_num=1,
            leg="ENTRY",
            quantity=quantity,
            order_type="MANUAL",
            transaction_type=str(sig.get("action") or "BUY").upper(),
            product=str(SCOUT_CONFIG.get("zerodha_product", "MIS")),
            price=fill,
            status="SIMULATED",
            status_message="Manual fill recorded in Scout",
        )
    db.commit()
    trade = dict(trade_repo.get(tid) or {})
    trade["exit_plan"] = build_exit_plan(
        sig,
        entry_price=fill,
        executed_at=trade.get("executed_at"),
        live_ltp=q.get("ltp"),
        now=now_ist().replace(tzinfo=None),
    )
    trade["exit_alerts"] = evaluate_exit_alerts(
        action=str(sig.get("action") or ""),
        live_ltp=q.get("ltp"),
        exit_plan=trade["exit_plan"],
    )
    return jsonify({"status": "ok", "trade_id": tid, "trade": trade})


@scout_bp.route("/trades/<int:trade_id>/close", methods=["POST"])
@_with_db
def api_scout_trade_close(db: SQLServerConnection, trade_id: int):
    body = request.get_json(silent=True) or {}
    exit_price = body.get("exit_price")
    if exit_price is None:
        return jsonify({"error": "exit_price required"}), 400
    trade_repo = ScoutTradeRepo(db)
    trade = trade_repo.close(
        trade_id,
        exit_price=float(exit_price),
        closed_at=now_ist(),
        exit_reason=str(body.get("exit_reason") or "manual")[:256],
    )
    if not trade:
        return jsonify({"error": "trade not found or already closed"}), 404
    db.commit()
    return jsonify({"status": "ok", "trade": trade})


@scout_bp.route("/trades/<int:trade_id>", methods=["DELETE"])
@_with_db
def api_scout_trade_void(db: SQLServerConnection, trade_id: int):
    if not ScoutTradeRepo(db).void(trade_id):
        return jsonify({"error": "trade not found or not open"}), 404
    db.commit()
    return jsonify({"status": "ok"})


@scout_bp.route("/history/trades")
@_with_db
def api_scout_history_trades(db: SQLServerConnection):
    from_d, to_d = _parse_date_window(
        request.args.get("from_date", ""),
        request.args.get("to_date", ""),
        days_default=int(request.args.get("days", 30)),
    )
    symbol = request.args.get("symbol", "").strip() or None
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = ScoutTradeRepo(db).closed_trades(
        from_date=from_d,
        to_date=to_d,
        symbol=symbol,
        limit=limit,
    )
    enriched = [enrich_history_trade(r) for r in rows]
    return jsonify({"trades": enriched, "count": len(enriched), "from_date": from_d, "to_date": to_d})


@scout_bp.route("/history/stats")
@_with_db
def api_scout_history_stats(db: SQLServerConnection):
    from_d, to_d = _parse_date_window(
        request.args.get("from_date", ""),
        request.args.get("to_date", ""),
        days_default=int(request.args.get("days", 30)),
    )
    stats = ScoutTradeRepo(db).performance_stats(from_date=from_d, to_date=to_d)
    stats["from_date"] = from_d
    stats["to_date"] = to_d
    from scout.history_display import pf_class, pnl_class, win_pct_class
    net = stats.get("total_net_pnl") if stats.get("total_net_pnl") is not None else stats.get("total_pnl")
    stats["display"] = {
        "net_pnl_class": pnl_class(net),
        "win_pct_class": win_pct_class(stats.get("win_rate_pct")),
        "profit_factor_class": pf_class(stats.get("profit_factor")),
    }
    return jsonify(stats)


@scout_bp.route("/zerodha-log")
@_with_db
def api_scout_zerodha_log(db: SQLServerConnection):
    """Persistent Zerodha permission / connectivity errors (date filter)."""
    from_d, to_d = _parse_date_window(
        request.args.get("from_date", ""),
        request.args.get("to_date", ""),
        days_default=int(request.args.get("days", 30)),
    )
    severity = (request.args.get("severity") or "").strip() or None
    search = (request.args.get("search") or "").strip() or None
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(0, int(request.args.get("offset", 0)))
    repo = ScoutZerodhaLogRepo(db)
    rows = repo.fetch(
        from_date=from_d,
        to_date=to_d,
        severity=severity,
        search=search,
        limit=limit,
        offset=offset,
    )
    return jsonify({
        "entries": rows,
        "count": len(rows),
        "total": repo.count(from_date=from_d, to_date=to_d, severity=severity),
        "from_date": from_d,
        "to_date": to_d,
    })


@scout_bp.route("/zerodha-check/latest")
@_with_db
def api_scout_zerodha_check_latest(db: SQLServerConnection):
    from providers.zerodha.permission_check import (
        latest_check_from_db,
        last_permission_summary,
        overlay_live_websocket_check,
    )

    summary = overlay_live_websocket_check(latest_check_from_db(db) or last_permission_summary())
    row = ScoutZerodhaLogRepo(db).latest_summary()
    return jsonify({
        "summary": summary,
        "last_logged": row,
    })


@scout_bp.route("/zerodha-check", methods=["POST"])
@_with_db
def api_scout_zerodha_check_run(db: SQLServerConnection):
    from providers.zerodha.permission_check import run_and_persist_check

    summary = run_and_persist_check(db, trigger="manual", include_ws=True)
    db.commit()
    return jsonify({"summary": summary})


def register_scout(app) -> None:
    """Mount scout API on the shared Flask app."""
    app.register_blueprint(scout_bp)
    logger.info("Intraday Scout blueprint registered at /api/scout")
