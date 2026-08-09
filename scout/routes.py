"""
scout/routes.py — Flask blueprint for Intraday Scout (/api/scout/*).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import wraps
from typing import Callable

from flask import Blueprint, jsonify, request

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import (
    ScoutSignalRepo,
    ScoutTradeRepo,
)
from scout.config_loader import (
    default_watchlist,
    get_watchlist,
    invalidate_watchlist_cache,
    is_nifty50,
    watchlist_set,
)
from scout.instruments import (
    ScoutInstrumentError,
    nse_equity_universe,
    nifty50_symbols,
    refresh_nse_equity_master,
)
from scout.market_data import zerodha_ready
from scout.utils import is_market_open
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)

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


@scout_bp.route("/status")
@_with_db
def api_scout_status(db: SQLServerConnection):
    ok, msg = zerodha_ready()
    last_sig = ScoutSignalRepo(db).last_signal()
    selected = get_watchlist(db)
    return jsonify({
        "enabled": bool(SCOUT_CONFIG.get("enabled", True)),
        "mode": "websocket",
        "market_open": is_market_open(),
        "zerodha_ok": ok,
        "zerodha_message": msg,
        "watchlist_count": len(selected),
        "push_enabled": bool(SCOUT_CONFIG.get("push_enabled", True)),
        "last_signal": last_sig,
    })


@scout_bp.route("/signals")
@_with_db
def api_scout_signals(db: SQLServerConnection):
    limit = min(int(request.args.get("limit", 50)), 200)
    since = min(int(request.args.get("since_minutes", 120)), 24 * 60)
    rows = ScoutSignalRepo(db).recent(limit=limit, since_minutes=since)
    open_ids = ScoutTradeRepo(db).open_signal_ids()
    for row in rows:
        row["trade_open"] = row.get("id") in open_ids
    return jsonify({"signals": rows, "count": len(rows)})


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

    try:
        page, total, refreshed_at = nse_equity_universe(
            search=search,
            offset=offset,
            limit=limit,
            force_refresh=force,
        )
    except ScoutInstrumentError as exc:
        return jsonify({"error": str(exc)}), 503

    stocks = []
    page_syms = set()
    for row in page:
        sym = row["symbol"]
        page_syms.add(sym)
        stocks.append({
            **row,
            "selected": sym in selected,
            "is_default": sym in default,
        })

    # Always surface selected symbols that are not on the current page.
    for sym in sorted(selected):
        if sym in page_syms:
            continue
        stocks.insert(0, {
            "symbol": sym,
            "name": "",
            "is_nifty50": is_nifty50(sym),
            "selected": True,
            "is_default": sym in default,
            "pinned_selected": True,
        })

    return jsonify({
        "stocks": stocks,
        "selected": sorted(selected),
        "selected_count": len(selected),
        "total_equity_count": total,
        "nifty50": nifty50,
        "nifty50_count": len(nifty50),
        "search": search,
        "offset": offset,
        "limit": limit,
        "instrument_refreshed_at": refreshed_at,
    })


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
    rows = trade_repo.open_trades()
    out = []
    for r in rows:
        row = dict(r)
        sid = row.get("signal_id")
        if sid:
            row["signal"] = sig_repo.get(int(sid))
        else:
            row["signal"] = None
        out.append(row)
    return jsonify({"trades": out, "count": len(out)})


@scout_bp.route("/signals/<int:signal_id>/mark-taken", methods=["POST"])
@_with_db
def api_scout_mark_taken(db: SQLServerConnection, signal_id: int):
    """Record execution — enter fill price/qty from your Zerodha order (like mark-executed)."""
    body = request.get_json(silent=True) or {}
    quantity = int(body.get("quantity") or 1)
    entry_price = body.get("entry_price")
    if quantity < 1:
        return jsonify({"error": "quantity must be >= 1"}), 400

    sig_repo = ScoutSignalRepo(db)
    trade_repo = ScoutTradeRepo(db)
    sig = sig_repo.get(signal_id)
    if not sig:
        return jsonify({"error": "signal not found"}), 404

    if signal_id in trade_repo.open_signal_ids():
        return jsonify({"error": "open trade already exists for this signal"}), 409

    fill = float(entry_price) if entry_price is not None else float(sig["ltp"])
    if fill <= 0:
        return jsonify({"error": "entry_price must be positive"}), 400

    tid = trade_repo.mark_taken(
        signal_id=signal_id,
        symbol=str(sig["symbol"]),
        action=str(sig["action"]),
        signal_type=str(sig.get("signal_type") or ""),
        entry_price=fill,
        quantity=quantity,
        executed_at=now_ist(),
        notes=str(body.get("notes") or "")[:512] or None,
    )
    db.commit()
    return jsonify({"status": "ok", "trade_id": tid, "trade": trade_repo.get(tid)})


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
    return jsonify({"trades": rows, "count": len(rows), "from_date": from_d, "to_date": to_d})


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
    return jsonify(stats)


def register_scout(app) -> None:
    """Mount scout API on the shared Flask app."""
    app.register_blueprint(scout_bp)
    logger.info("Intraday Scout blueprint registered at /api/scout")
