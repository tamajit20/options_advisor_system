"""
providers/zerodha/permission_check.py
=====================================

Probe Kite Connect permissions after login and at start-of-day.
Results are persisted to scout_zerodha_log for the Scout Errors UI.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from config import SCOUT_CONFIG, ZERODHA_API_CONFIG
from database.connection import SQLServerConnection
from providers.zerodha.session import is_token_valid, load_session
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)

_last_check_summary: Optional[dict] = None


def last_permission_summary() -> Optional[dict]:
    return dict(_last_check_summary) if _last_check_summary else None


def permissions_ok_for_live() -> bool:
    """True when the latest persisted check passed all required probes."""
    summary = last_permission_summary()
    if summary is not None:
        return bool(summary.get("overall_ok"))
    return False


def _set_summary(summary: dict) -> dict:
    global _last_check_summary
    _last_check_summary = dict(summary)
    return summary


def _check_row(
    check_id: str,
    label: str,
    ok: bool,
    *,
    detail: str = "",
    error: Optional[str] = None,
) -> dict:
    return {
        "check_id": check_id,
        "label": label,
        "ok": bool(ok),
        "detail": detail,
        "error": error,
    }


def _probe_api_credentials() -> dict:
    key = str(ZERODHA_API_CONFIG.get("api_key") or "").strip()
    secret = str(ZERODHA_API_CONFIG.get("api_secret") or "").strip()
    enabled = bool(ZERODHA_API_CONFIG.get("enabled", True))
    if not enabled:
        return _check_row(
            "api_enabled", "Zerodha API enabled", False,
            error="OPT_ZERODHA_ENABLED is false",
        )
    if not key or not secret:
        return _check_row(
            "api_credentials", "API key & secret configured", False,
            error="Set OPT_ZERODHA_API_KEY and OPT_ZERODHA_API_SECRET in environment",
        )
    return _check_row(
        "api_credentials", "API key & secret configured", True,
        detail=f"api_key …{key[-4:]}" if len(key) >= 4 else "configured",
    )


def _probe_session() -> dict:
    sess = load_session()
    if not sess or not sess.access_token:
        return _check_row(
            "session", "Access token (daily login)", False,
            error="Not logged in — paste request_token in WS Monitor",
        )
    if not is_token_valid(sess):
        return _check_row(
            "session", "Access token (daily login)", False,
            error="Token expired — log in again after 06:00 IST",
            detail=f"user {sess.user_id}" if sess.user_id else "",
        )
    return _check_row(
        "session", "Access token (daily login)", True,
        detail=f"user {sess.user_id}" if sess.user_id else "valid",
    )


def _kite_client_or_error():
    from providers.zerodha.order_client import KiteOrderClient, ZerodhaOrderError
    try:
        return KiteOrderClient(), None
    except ZerodhaOrderError as exc:
        return None, str(exc)


def _probe_profile(client) -> dict:
    try:
        prof = client._kite.profile()
        user = prof.get("user_name") or prof.get("user_id") or ""
        return _check_row("profile", "Profile API", True, detail=str(user))
    except Exception as exc:
        return _check_row("profile", "Profile API", False, error=str(exc))


def _probe_margins(client) -> dict:
    try:
        raw = client.margins()
        equity = raw.get("equity") if isinstance(raw, dict) else {}
        avail = (equity or {}).get("available") or {}
        bal = avail.get("live_balance") or avail.get("cash") or equity.get("net")
        return _check_row(
            "margins", "Wallet / margins API", True,
            detail=f"equity balance ₹{float(bal or 0):,.0f}",
        )
    except Exception as exc:
        return _check_row("margins", "Wallet / margins API", False, error=str(exc))


def _probe_order_margins(client) -> dict:
    """Order basket margin — confirms order-placement scope without live orders."""
    try:
        orders = [{
            "exchange": str(SCOUT_CONFIG.get("zerodha_exchange", "NSE")),
            "tradingsymbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "product": str(SCOUT_CONFIG.get("zerodha_product", "MIS")),
            "order_type": str(SCOUT_CONFIG.get("zerodha_entry_order_type", "LIMIT")),
            "price": 1000.0,
        }]
        resp = client.order_margins(orders)
        required = 0.0
        if resp:
            row = resp[0] if isinstance(resp, list) else resp
            required = float(row.get("total") or row.get("margin") or 0)
        return _check_row(
            "order_margins", "Order margin API (live orders)", True,
            detail=f"sample MIS margin ₹{required:,.0f}",
        )
    except Exception as exc:
        msg = str(exc)
        hint = ""
        if "permission" in msg.lower() or "insufficient" in msg.lower():
            hint = " — Kite app may be market-data-only; enable order permissions"
        return _check_row(
            "order_margins", "Order margin API (live orders)", False,
            error=msg + hint,
        )


def _probe_websocket() -> dict:
    try:
        from scout.execution_health import ws_health
        ws = ws_health()
        if ws.get("ok"):
            return _check_row(
                "websocket", "WebSocket runner", True,
                detail=f"connected · snapshot {ws.get('age_seconds')}s old",
            )
        return _check_row(
            "websocket", "WebSocket runner", False,
            error=ws.get("reason") or "WebSocket unhealthy",
            detail=f"state={ws.get('connection_state')}",
        )
    except Exception as exc:
        return _check_row("websocket", "WebSocket runner", False, error=str(exc))


def run_zerodha_permission_check(*, include_ws: bool = True) -> dict:
    """Run all probes in-process. Does not persist."""
    checks: List[dict] = []
    checks.append(_probe_api_credentials())
    checks.append(_probe_session())

    client, client_err = _kite_client_or_error()
    if client_err:
        checks.append(_check_row("kite_client", "Kite client", False, error=client_err))
    else:
        checks.append(_check_row("kite_client", "Kite client", True))
        checks.append(_probe_profile(client))
        checks.append(_probe_margins(client))
        checks.append(_probe_order_margins(client))

    if include_ws:
        checks.append(_probe_websocket())

    required_ids = {
        "api_credentials", "api_enabled", "session", "kite_client",
        "profile", "margins", "order_margins",
    }
    failed = [c for c in checks if not c["ok"] and c["check_id"] in required_ids]
    overall_ok = len(failed) == 0

    sess = load_session()
    return _set_summary({
        "overall_ok": overall_ok,
        "checked_at": now_ist().replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),
        "user_id": sess.user_id if sess else None,
        "checks": checks,
        "failed_count": len([c for c in checks if not c["ok"]]),
    })


def _persist_check(
    db: SQLServerConnection,
    *,
    trigger: str,
    summary: dict,
) -> str:
    from database.scout_models import ScoutZerodhaLogRepo

    run_id = str(uuid.uuid4())
    repo = ScoutZerodhaLogRepo(db)
    user_id = summary.get("user_id")
    logged_at = now_ist().replace(tzinfo=None)

    for chk in summary.get("checks") or []:
        if chk.get("ok"):
            continue
        sev = "ERROR" if chk.get("check_id") in {
            "api_credentials", "api_enabled", "session", "kite_client",
            "profile", "margins", "order_margins",
        } else "WARNING"
        msg = chk.get("error") or chk.get("label") or "check failed"
        repo.insert(
            run_id=run_id,
            trigger_source=trigger,
            severity=sev,
            code=str(chk.get("check_id") or "unknown"),
            message=str(msg)[:1024],
            detail=chk.get("detail") or None,
            user_id=user_id,
            logged_at=logged_at,
        )

    summary_sev = "INFO" if summary.get("overall_ok") else "ERROR"
    summary_msg = (
        "All Zerodha permission checks passed"
        if summary.get("overall_ok")
        else f"{summary.get('failed_count', 0)} check(s) failed — live orders blocked"
    )
    repo.insert(
        run_id=run_id,
        trigger_source=trigger,
        severity=summary_sev,
        code="check_summary",
        message=summary_msg,
        detail=json.dumps(summary, default=str),
        user_id=user_id,
        logged_at=logged_at,
    )
    return run_id


def run_and_persist_check(
    db: SQLServerConnection,
    *,
    trigger: str,
    include_ws: bool = True,
) -> dict:
    """Run probes and write failures + summary to scout_zerodha_log."""
    summary = run_zerodha_permission_check(include_ws=include_ws)
    summary["trigger"] = trigger
    summary["run_id"] = _persist_check(db, trigger=trigger, summary=summary)
    logger.info(
        "Zerodha permission check (%s): overall_ok=%s failed=%s",
        trigger, summary.get("overall_ok"), summary.get("failed_count"),
    )
    return summary


def latest_check_from_db(db: SQLServerConnection) -> Optional[dict]:
    from database.scout_models import ScoutZerodhaLogRepo

    row = ScoutZerodhaLogRepo(db).latest_summary()
    if not row:
        return None
    detail = row.get("detail")
    if detail:
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                _set_summary(parsed)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "overall_ok": str(row.get("severity") or "").upper() == "INFO",
        "checked_at": row.get("logged_at"),
        "message": row.get("message"),
    }


def should_run_daily_check(db: SQLServerConnection) -> bool:
    """Run once per IST calendar day when a valid session exists."""
    sess = load_session()
    if not sess or not is_token_valid(sess):
        return False
    from database.scout_models import ScoutZerodhaLogRepo

    last = ScoutZerodhaLogRepo(db).latest_summary_at()
    if last is None:
        return True
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return True
    if isinstance(last, datetime) and last.tzinfo:
        last = last.replace(tzinfo=None)
    return last.date() < today_ist().date()


def run_daily_check_if_needed(
    db: SQLServerConnection,
    *,
    trigger: str = "startup",
    force: bool = False,
) -> Optional[dict]:
    if not force and not should_run_daily_check(db):
        latest_check_from_db(db)
        return last_permission_summary()
    return run_and_persist_check(db, trigger=trigger)


def open_db_and_run_check(*, trigger: str, force: bool = False) -> Optional[dict]:
    """Helper for hooks without an existing connection (login, app startup)."""
    db = SQLServerConnection()
    try:
        db.connect()
        if trigger == "login":
            out = run_and_persist_check(db, trigger=trigger)
        else:
            out = run_daily_check_if_needed(db, trigger=trigger, force=force)
        db.commit()
        return out
    except Exception:
        logger.exception("Zerodha permission check failed (%s)", trigger)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass
