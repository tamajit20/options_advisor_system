"""Execution health — WS staleness, unprotected trades, wallet, alarms."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.connection import SQLServerConnection
from database.scout_models import ScoutTradeRepo
from scout.execution_engine import zerodha_execute_enabled
from scout.wallet import last_wallet_error, wallet_summary
from utils import now_ist

logger = logging.getLogger(__name__)

_WS_STALE_SECONDS = 45


def _ws_status_path() -> Path:
    from providers.ws_monitor import default_snapshot_path
    return default_snapshot_path()


def _read_ws_snapshot() -> Optional[dict]:
    path = _ws_status_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("ws_status read failed: %s", exc)
        return None


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_seconds(ts: Optional[datetime]) -> Optional[float]:
    if not ts:
        return None
    now = now_ist().replace(tzinfo=None)
    ref = ts.replace(tzinfo=None) if ts.tzinfo else ts
    return max(0.0, (now - ref).total_seconds())


def _snapshot_timestamps(snap: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return (last_tick_at, snapshot_generated_at) from ws_status.json."""
    tick = None
    for key in ("last_tick_at", "runner_last_tick_at"):
        tick = _parse_ts(snap.get(key))
        if tick:
            break
    generated = None
    for key in ("generated_at", "updated_at", "snapshot_at"):
        generated = _parse_ts(snap.get(key))
        if generated:
            break
    return tick, generated


def ws_health(*, market_open: Optional[bool] = None) -> dict:
    snap = _read_ws_snapshot()
    if not snap:
        return {
            "ok": False,
            "connected": False,
            "stale": True,
            "age_seconds": None,
            "reason": "ws_status.json missing — WS runner may be down",
        }

    if market_open is None:
        try:
            from scout.utils import is_market_open
            market_open = is_market_open()
        except Exception:
            market_open = True

    conn = str(snap.get("connection_state") or "").lower()
    tick_ts, snap_ts = _snapshot_timestamps(snap)
    tick_age = _age_seconds(tick_ts)
    snap_age = _age_seconds(snap_ts)
    tick_rate = float(snap.get("tick_rate_per_sec") or 0)

    connected = conn in ("connected", "streaming", "open")
    stale = True
    age: Optional[float] = None
    if market_open:
        if tick_age is not None:
            age = tick_age
            stale = tick_age > _WS_STALE_SECONDS
        elif snap_age is not None:
            age = snap_age
            stale = snap_age > _WS_STALE_SECONDS
        elif connected and tick_rate > 0:
            age = 0.0
            stale = False
    else:
        age = snap_age if snap_age is not None else tick_age
        monitor_fresh = age is not None and age <= _WS_STALE_SECONDS
        stale = not connected or (not monitor_fresh and tick_rate <= 0)

    ok = connected and not stale
    reason = ""
    if not connected:
        reason = f"websocket {conn or 'disconnected'}"
    elif stale:
        if age is None:
            reason = "ws snapshot missing timestamps"
        else:
            reason = f"ws snapshot stale ({int(age)}s old)"

    return {
        "ok": ok,
        "connected": connected,
        "stale": stale,
        "age_seconds": round(age, 1) if age is not None else None,
        "tick_rate_per_sec": round(tick_rate, 2) if tick_rate else 0.0,
        "connection_state": conn or "unknown",
        "reason": reason,
    }


def build_execution_health(
    db: SQLServerConnection,
    settings: dict,
    *,
    fetch_wallet: bool = True,
) -> dict:
    """Aggregate execution alarms for API and dashboard."""
    live = zerodha_execute_enabled(settings)
    trade_repo = ScoutTradeRepo(db)
    unprotected = trade_repo.unprotected_trades()
    ws = ws_health()
    wallet = wallet_summary(db, settings, fetch=fetch_wallet)

    try:
        from providers.zerodha.permission_check import latest_check_from_db, last_permission_summary
        perm = latest_check_from_db(db) or last_permission_summary()
    except Exception:
        perm = None

    alarms: List[dict] = []

    if live:
        if perm is not None and not perm.get("overall_ok"):
            failed = [c for c in (perm.get("checks") or []) if not c.get("ok")]
            for c in failed[:5]:
                alarms.append({
                    "level": "critical",
                    "code": f"perm_{c.get('check_id')}",
                    "message": c.get("error") or c.get("label") or "Permission check failed",
                })
            if not failed:
                alarms.append({
                    "level": "critical",
                    "code": "permissions_failed",
                    "message": "Zerodha permission check failed — live orders blocked",
                })

        if not ws.get("ok"):
            alarms.append({
                "level": "critical",
                "code": "ws_disconnected",
                "message": ws.get("reason") or "WebSocket unhealthy",
            })

        for t in unprotected:
            alarms.append({
                "level": "critical",
                "code": "unprotected_position",
                "trade_id": int(t["id"]),
                "symbol": t.get("symbol"),
                "message": (
                    f"TRD #{t['id']} {t.get('symbol')} — live position without stop-loss on Zerodha"
                ),
            })

        w_err = wallet.get("error")
        if w_err and w_err not in ("paper_mode",):
            alarms.append({
                "level": "warning",
                "code": "wallet_unavailable",
                "message": f"Cannot read Zerodha wallet: {w_err}",
            })
        elif wallet.get("free_inr") is not None and float(wallet["free_inr"]) <= 0:
            alarms.append({
                "level": "warning",
                "code": "wallet_exhausted",
                "message": (
                    f"Deployable capital fully used "
                    f"(₹{wallet.get('deployed_inr', 0):,.0f} deployed, "
                    f"cap ₹{wallet.get('max_deployable_inr', 0):,.0f})"
                ),
            })

        try:
            from providers.zerodha.order_client import last_order_error
            order_err = last_order_error()
        except ImportError:
            order_err = None
        if order_err:
            alarms.append({
                "level": "warning",
                "code": "last_order_error",
                "message": order_err[:200],
            })

    critical = sum(1 for a in alarms if a["level"] == "critical")
    return {
        "live": live,
        "healthy": critical == 0,
        "alarm_count": len(alarms),
        "critical_count": critical,
        "alarms": alarms,
        "unprotected_count": len(unprotected),
        "unprotected_trades": unprotected,
        "websocket": ws,
        "wallet": wallet,
    }
