"""
providers/ws_health.py
======================

Shared WebSocket health probe for scheduler fallback jobs.

Reads ``data/ws_status.json`` written by ``providers/ws_monitor.WSMonitor`` in
the ws_runner process. Used to gate intraday SL fallback so it runs **only**
when live ticks are unavailable — not as a parallel always-on monitor.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime
from typing import Optional, Tuple

from config import STRATEGY_CONFIG
from providers.ws_monitor import default_snapshot_path
from utils import now_ist

_UNHEALTHY_STATES = frozenset({
    "disconnected", "degraded", "token_expired",
    "waiting_for_login", "stale", "stopped",
})


def _in_nse_session(now: datetime) -> bool:
    lrm = STRATEGY_CONFIG.get("live_risk_monitor") or {}
    start_s = str(lrm.get("session_start", "09:15"))
    end_s = str(lrm.get("session_end", "15:30"))
    try:
        sh, sm = (int(x) for x in start_s.split(":")[:2])
        eh, em = (int(x) for x in end_s.split(":")[:2])
    except (ValueError, TypeError):
        sh, sm, eh, em = 9, 15, 15, 30
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(sh, sm) <= t <= dtime(eh, em)


def in_nse_session(now: datetime) -> bool:
    """True during configured NSE cash/options session (Mon–Fri)."""
    return _in_nse_session(now)


def _tick_age_seconds(snap: dict, now: datetime) -> Optional[float]:
    last_tick = snap.get("last_tick_at")
    if not last_tick:
        return None
    try:
        last_dt = datetime.fromisoformat(str(last_tick).replace("Z", "+00:00"))
    except ValueError:
        return None
    if last_dt.tzinfo is not None:
        from zoneinfo import ZoneInfo
        last_dt = last_dt.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    return (now - last_dt).total_seconds()


def _apply_stale_override(snap: dict, now: datetime) -> None:
    """Downgrade connected→stale when ticks stop (mirrors dashboard logic)."""
    raw_state = snap.get("connection_state")
    if raw_state != "connected":
        return
    in_market = _in_nse_session(now)
    threshold = 90.0 if in_market else 1800.0
    stale_reason: Optional[str] = None

    subs = snap.get("subscribed_tokens")
    if in_market and subs is not None and int(subs) == 0:
        stale_reason = "0 subscribed tokens during market hours"

    if stale_reason is None:
        age = _tick_age_seconds(snap, now)
        if age is not None and age > threshold:
            stale_reason = f"no ticks for {int(age)}s (threshold {int(threshold)}s)"
        elif age is None and in_market:
            started = snap.get("started_at")
            if started:
                try:
                    from datetime import timezone as _tz
                    started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=_tz.utc)
                    uptime_s = (datetime.now(_tz.utc) - started_dt).total_seconds()
                    if uptime_s > threshold:
                        stale_reason = f"no ticks since runner start ({int(uptime_s)}s ago)"
                except ValueError:
                    pass

    if stale_reason:
        snap["raw_connection_state"] = raw_state
        snap["connection_state"] = "stale"
        snap["stale_reason"] = stale_reason


def load_ws_status() -> Optional[dict]:
    path = default_snapshot_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def is_ws_unhealthy(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Return (unhealthy, reason). When False, LiveRiskMonitor should be active."""
    now = now or now_ist()
    if not _in_nse_session(now):
        return False, "outside_session"

    cfg = STRATEGY_CONFIG.get("intraday_sl_fallback") or {}
    tick_stale_sec = float(cfg.get("tick_stale_sec", 90.0))

    snap = load_ws_status()
    if snap is None:
        return True, "ws_status_missing"

    snap_age = snap.get("generated_at")
    if snap_age:
        try:
            gen_dt = datetime.fromisoformat(str(snap_age).replace("Z", "+00:00"))
            if gen_dt.tzinfo is not None:
                from zoneinfo import ZoneInfo
                gen_dt = gen_dt.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            if (now - gen_dt).total_seconds() > float(cfg.get("snapshot_max_age_sec", 120.0)):
                return True, "ws_status_stale"
        except ValueError:
            pass

    _apply_stale_override(snap, now)

    if snap.get("token_expired"):
        return True, "token_expired"

    state = str(snap.get("connection_state") or "unknown")
    if state in _UNHEALTHY_STATES:
        return True, f"connection_state={state}"

    age = _tick_age_seconds(snap, now)
    if age is None or age > tick_stale_sec:
        return True, f"last_tick_stale age={age}"

    return False, "ws_healthy"
