"""
arb/routes.py — Flask blueprint for Arb Monitor (/api/arb/*).
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import wraps
from typing import Callable, Optional

from flask import Blueprint, jsonify, request

from config import ARB_CONFIG
from database.arb_models import ArbConfigRepo, ArbGapRepo, ArbPairRepo
from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)

arb_bp = Blueprint("arb", __name__, url_prefix="/api/arb")

_live_engine = None


def set_live_engine(engine) -> None:
    global _live_engine
    _live_engine = engine


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None


def _with_db(handler: Callable) -> Callable:
    @wraps(handler)
    def _wrapped(*args, **kwargs):
        db = SQLServerConnection()
        db.connect()
        try:
            return handler(db, *args, **kwargs)
        finally:
            db.close()

    return _wrapped


@arb_bp.get("/status")
@_with_db
def arb_status(db):
    pairs = ArbPairRepo(db).count_active()
    cfg = ArbConfigRepo(db)
    return jsonify({
        "enabled": cfg.get_enabled(default=ARB_CONFIG.get("enabled", True)),
        "universe": cfg.get_universe(default=ARB_CONFIG.get("universe", "nifty50_dual")),
        "pairs_count": pairs,
        "tick_staleness_sec": ARB_CONFIG.get("tick_staleness_sec", 3),
    })


@arb_bp.get("/pairs")
@_with_db
def list_pairs(db):
    rows = ArbPairRepo(db).list_all()
    return jsonify({"pairs": rows, "count": len(rows)})


@arb_bp.post("/pairs/refresh")
@_with_db
def refresh_pairs(db):
    from providers.zerodha.facade import KiteFacade
    from providers.zerodha.instruments import InstrumentMaster
    from providers.zerodha.session import load_session
    from arb.instruments import refresh_pairs_to_db

    session = load_session()
    if session is None:
        return jsonify({"error": "Zerodha session required to refresh pairs"}), 503

    universe = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        universe = body.get("universe")
    if not universe:
        universe = ArbConfigRepo(db).get_universe(default=ARB_CONFIG.get("universe", "nifty50_dual"))

    facade = KiteFacade(api_key=session.api_key, access_token=session.access_token)
    master = InstrumentMaster(loader=lambda: facade.instruments())
    count = refresh_pairs_to_db(db, master, universe=universe)
    db.commit()
    return jsonify({"ok": True, "pairs_refreshed": count, "universe": universe})


@arb_bp.get("/gaps")
@_with_db
def list_gaps(db):
    symbol = request.args.get("symbol")
    min_gap_pct = request.args.get("min_gap_pct", type=float)
    min_duration_sec = request.args.get("min_duration_sec", type=int)
    limit = request.args.get("limit", default=200, type=int)
    from_dt = _parse_dt(request.args.get("from") or request.args.get("from_date"))
    to_dt = _parse_dt(request.args.get("to") or request.args.get("to_date"))
    if to_dt and to_dt.hour == 0 and to_dt.minute == 0:
        to_dt = to_dt.replace(hour=23, minute=59, second=59)

    rows = ArbGapRepo(db).list_gaps(
        from_dt=from_dt,
        to_dt=to_dt,
        symbol=symbol,
        min_gap_pct=min_gap_pct,
        min_duration_sec=min_duration_sec,
        limit=min(max(limit, 1), 1000),
    )
    return jsonify({"gaps": rows, "count": len(rows)})


@arb_bp.get("/live")
def live_gaps():
    if _live_engine is not None:
        gaps = _live_engine.live_gaps()
        return jsonify({"gaps": gaps, "count": len(gaps), "source": "engine"})
    db = SQLServerConnection()
    db.connect()
    try:
        rows = ArbGapRepo(db).open_gaps()
        return jsonify({"gaps": rows, "count": len(rows), "source": "db"})
    finally:
        db.close()


def register_arb(app) -> None:
    app.register_blueprint(arb_bp)
