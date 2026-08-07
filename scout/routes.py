"""
scout/routes.py — Flask blueprint for Intraday Scout (/api/scout/*).
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from flask import Blueprint, jsonify, request

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import ScoutScanLogRepo, ScoutSignalRepo
from scout.market_data import zerodha_ready
from scout.orchestrator import run_scout_scan
from scout.utils import is_market_open

logger = logging.getLogger(__name__)

scout_bp = Blueprint("scout", __name__, url_prefix="/api/scout")

SCOUT_JOB_META = {
    "scout_scanner": {
        "icon": "🔍",
        "name": "Intraday Scout Scan",
        "description": "Scans the equity watchlist on live Zerodha 1m candles for BUY/SELL intraday setups.",
        "module": "scout",
    },
}


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


@scout_bp.route("/status")
@_with_db
def api_scout_status(db: SQLServerConnection):
    ok, msg = zerodha_ready()
    last = ScoutScanLogRepo(db).last_success()
    return jsonify({
        "enabled": bool(SCOUT_CONFIG.get("enabled", True)),
        "market_open": is_market_open(),
        "zerodha_ok": ok,
        "zerodha_message": msg,
        "watchlist_count": len(SCOUT_CONFIG.get("watchlist") or []),
        "last_scan": last,
    })


@scout_bp.route("/signals")
@_with_db
def api_scout_signals(db: SQLServerConnection):
    limit = min(int(request.args.get("limit", 50)), 200)
    since = min(int(request.args.get("since_minutes", 120)), 24 * 60)
    rows = ScoutSignalRepo(db).recent(limit=limit, since_minutes=since)
    return jsonify({"signals": rows, "count": len(rows)})


@scout_bp.route("/scan", methods=["POST"])
@_with_db
def api_scout_scan_now(db: SQLServerConnection):
    ok, msg = zerodha_ready()
    if not ok:
        return jsonify({"error": msg}), 503
    n = run_scout_scan(db)
    return jsonify({"status": "ok", "signals_found": n})


def register_scout(app) -> None:
    """Mount scout API on the shared Flask app."""
    app.register_blueprint(scout_bp)
    logger.info("Intraday Scout blueprint registered at /api/scout")
