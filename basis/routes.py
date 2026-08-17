"""
basis/routes.py — Flask blueprint for Cash-Futures Basis Monitor (/api/basis/*).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from functools import wraps
from typing import Callable, Optional

from flask import Blueprint, Response, jsonify, request, stream_with_context

from config import BASIS_CONFIG
from basis.config_loader import get_basis_settings, reload_basis_settings, set_basis_settings
from basis.settings_schema import default_basis_settings
from database.basis_models import BasisConfigRepo, BasisEpisodeRepo, BasisPairRepo
from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)

basis_bp = Blueprint("basis", __name__, url_prefix="/api/basis")

_live_engine = None
_LIVE_STATE_PATH = str(BASIS_CONFIG.get("live_state_path") or "data/basis_live_state.json")


def set_live_engine(engine) -> None:
    global _live_engine
    _live_engine = engine


def _read_live_state_file() -> Optional[dict]:
    if not os.path.exists(_LIVE_STATE_PATH):
        return None
    try:
        with open(_LIVE_STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "basis" in data:
            return data
    except Exception:
        logger.debug("basis live state read failed", exc_info=True)
    return None


def _live_payload() -> dict:
    if _live_engine is not None:
        try:
            snap = _live_engine.live_snapshot()
            snap.setdefault("source", "engine")
            return snap
        except AttributeError:
            rows = _live_engine.live_basis()
            return {"basis": rows, "count": len(rows), "source": "engine"}
    snap = _read_live_state_file()
    if snap is not None:
        snap.setdefault("source", "file")
        snap.setdefault("count", len(snap.get("basis") or []))
        return snap
    db = SQLServerConnection()
    db.connect()
    try:
        rows = BasisEpisodeRepo(db).open_episodes()
        return {"basis": rows, "count": len(rows), "source": "db"}
    finally:
        db.close()


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


@basis_bp.get("/status")
@_with_db
def basis_status(db):
    pairs = BasisPairRepo(db).count_active()
    settings = get_basis_settings(db)
    return jsonify({
        "enabled": settings.get("enabled", True),
        "universe": settings.get("universe", "nifty50_fo"),
        "pairs_count": pairs,
        "tick_staleness_sec": settings.get("tick_staleness_sec"),
        "min_basis_store_pct": settings.get("min_basis_store_pct", 0),
        "min_duration_store_sec": settings.get("min_duration_store_sec", 0),
    })


@basis_bp.get("/config")
@_with_db
def basis_config_get(db):
    return jsonify({
        "settings": get_basis_settings(db),
        "defaults": default_basis_settings(),
    })


@basis_bp.put("/config")
@_with_db
def basis_config_put(db):
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    current = reload_basis_settings(db)
    merged = {**current, **body}
    cleaned = set_basis_settings(db, merged)
    db.commit()
    return jsonify({"status": "ok", "settings": cleaned})


@basis_bp.get("/pairs")
@_with_db
def list_pairs(db):
    rows = BasisPairRepo(db).list_all()
    return jsonify({"pairs": rows, "count": len(rows)})


@basis_bp.post("/pairs/refresh")
@_with_db
def refresh_pairs(db):
    from providers.zerodha.facade import KiteFacade
    from providers.zerodha.instruments import InstrumentMaster
    from providers.zerodha.session import load_session
    from basis.instruments import refresh_pairs_to_db

    session = load_session()
    if session is None:
        return jsonify({"error": "Zerodha session required to refresh pairs"}), 503

    universe = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        universe = body.get("universe")
    if not universe:
        settings = get_basis_settings(db)
        universe = settings.get("universe", BASIS_CONFIG.get("universe", "nifty50_fo"))

    facade = KiteFacade(api_key=session.api_key, access_token=session.access_token)
    master = InstrumentMaster(loader=lambda: facade.instruments())
    count = refresh_pairs_to_db(db, master, universe=universe)
    db.commit()
    return jsonify({"ok": True, "pairs_refreshed": count, "universe": universe})


@basis_bp.get("/episodes/history")
@_with_db
def list_episodes(db):
    symbol = request.args.get("symbol")
    min_basis_pct = request.args.get("min_basis_pct", type=float)
    min_duration_sec = request.args.get("min_duration_sec", type=int)
    limit = request.args.get("limit", default=200, type=int)
    from_dt = _parse_dt(request.args.get("from") or request.args.get("from_date"))
    to_dt = _parse_dt(request.args.get("to") or request.args.get("to_date"))
    if to_dt and to_dt.hour == 0 and to_dt.minute == 0:
        to_dt = to_dt.replace(hour=23, minute=59, second=59)

    rows = BasisEpisodeRepo(db).list_episodes(
        from_dt=from_dt,
        to_dt=to_dt,
        symbol=symbol,
        min_basis_pct=min_basis_pct,
        min_duration_sec=min_duration_sec,
        limit=min(max(limit, 1), 1000),
    )
    return jsonify({"episodes": rows, "count": len(rows)})


@basis_bp.get("/live")
def live_basis():
    return jsonify(_live_payload())


@basis_bp.get("/live/snapshot")
def live_basis_snapshot():
    return jsonify(_live_payload())


@basis_bp.get("/live/stream")
def live_basis_stream():
    poll_interval = float(BASIS_CONFIG.get("live_stream_poll_sec", 0.5))

    @stream_with_context
    def _gen():
        last_sig: Optional[str] = None
        heartbeat_at = time.monotonic()
        yield ": connected\n\n"
        while True:
            time.sleep(poll_interval)
            try:
                snap = _live_payload()
                sig = json.dumps(snap, sort_keys=True, default=str)
                if sig != last_sig:
                    last_sig = sig
                    yield f"data: {json.dumps(snap, default=str)}\n\n"
            except Exception:
                pass
            if time.monotonic() - heartbeat_at >= 15:
                heartbeat_at = time.monotonic()
                yield ": ping\n\n"

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def register_basis(app) -> None:
    app.register_blueprint(basis_bp)
