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
from database.scout_models import ScoutSignalRepo
from scout.market_data import zerodha_ready
from scout.utils import is_market_open

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


@scout_bp.route("/status")
@_with_db
def api_scout_status(db: SQLServerConnection):
    ok, msg = zerodha_ready()
    last_sig = ScoutSignalRepo(db).last_signal()
    return jsonify({
        "enabled": bool(SCOUT_CONFIG.get("enabled", True)),
        "mode": "websocket",
        "market_open": is_market_open(),
        "zerodha_ok": ok,
        "zerodha_message": msg,
        "watchlist_count": len(SCOUT_CONFIG.get("watchlist") or []),
        "push_enabled": bool(SCOUT_CONFIG.get("push_enabled", True)),
        "last_signal": last_sig,
    })


@scout_bp.route("/signals")
@_with_db
def api_scout_signals(db: SQLServerConnection):
    limit = min(int(request.args.get("limit", 50)), 200)
    since = min(int(request.args.get("since_minutes", 120)), 24 * 60)
    rows = ScoutSignalRepo(db).recent(limit=limit, since_minutes=since)
    return jsonify({"signals": rows, "count": len(rows)})


def register_scout(app) -> None:
    """Mount scout API on the shared Flask app."""
    app.register_blueprint(scout_bp)
    logger.info("Intraday Scout blueprint registered at /api/scout")
