"""
lifecycle/zerodha_execution_job.py
===================================

Background Zerodha execution jobs — non-blocking HTTP for multi-leg orders.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from typing import Any, Callable, Dict, Optional

from database.connection import SQLServerConnection
from database.thread_db import dedicated_connection
from database.zerodha_execution_job_repo import ZerodhaExecutionJobRepo
from utils import now_ist


logger = logging.getLogger(__name__)

_POOL_LOCK = threading.Lock()
_ACTIVE = 0
_MAX_CONCURRENT = 2
_JOB_LOCKS_GUARD = threading.Lock()
_JOB_LOCKS: Dict[str, threading.Lock] = {}


def _job_lock_for(key: str) -> threading.Lock:
    with _JOB_LOCKS_GUARD:
        if key not in _JOB_LOCKS:
            _JOB_LOCKS[key] = threading.Lock()
        return _JOB_LOCKS[key]


def _can_start_job() -> bool:
    global _ACTIVE
    with _POOL_LOCK:
        return _ACTIVE < _MAX_CONCURRENT


def _inc_active() -> None:
    global _ACTIVE
    with _POOL_LOCK:
        _ACTIVE += 1


def _dec_active() -> None:
    global _ACTIVE
    with _POOL_LOCK:
        _ACTIVE = max(0, _ACTIVE - 1)


def submit_execution_job(
    db: SQLServerConnection,
    *,
    operation: str,
    suggestion_id: Optional[str],
    trade_id: Optional[str],
    total_legs: int,
    runner: Callable[[SQLServerConnection, int], Any],
) -> int:
    """Insert job row and start background thread. Returns job id."""
    if not _can_start_job():
        raise RuntimeError(
            "Too many Zerodha executions in progress — wait for one to finish"
        )
    if suggestion_id:
        lock_key = f"sugg:{suggestion_id}"
    elif trade_id:
        lock_key = f"trade:{trade_id}"
    else:
        raise RuntimeError("Execution job requires a suggestion_id or trade_id")

    lock = _job_lock_for(lock_key)
    if not lock.acquire(blocking=False):
        raise RuntimeError("Zerodha execution already running for this target")

    try:
        repo = ZerodhaExecutionJobRepo(db)
        if suggestion_id and repo.running_for_suggestion(suggestion_id) is not None:
            raise RuntimeError(
                "Zerodha execution already running for this suggestion"
            )
        if trade_id and repo.running_for_trade(trade_id) is not None:
            raise RuntimeError(
                "Zerodha execution already running for this trade"
            )
        now = now_ist()
        job_id = repo.insert({
            "operation": operation,
            "suggestion_id": suggestion_id,
            "trade_id": trade_id,
            "status": "RUNNING",
            "total_legs": total_legs,
            "filled_legs": 0,
            "message": "Starting…",
            "created_at": now,
            "updated_at": now,
        })
        db.commit()
    except Exception:
        lock.release()
        raise
    else:
        lock.release()

    def _worker() -> None:
        _inc_active()
        try:
            with dedicated_connection() as wdb:
                try:
                    outcome = runner(wdb, job_id)
                    _finish_job(wdb, job_id, ok=True, outcome=outcome)
                except Exception as exc:
                    logger.exception("zerodha execution job %s failed", job_id)
                    _finish_job(wdb, job_id, ok=False, error=str(exc))
        finally:
            _dec_active()

    threading.Thread(
        target=_worker,
        name=f"zerodha-job-{job_id}",
        daemon=True,
    ).start()
    return job_id


def _finish_job(
    db: SQLServerConnection,
    job_id: int,
    *,
    ok: bool,
    outcome: Any = None,
    error: Optional[str] = None,
) -> None:
    repo = ZerodhaExecutionJobRepo(db)
    now = now_ist()
    payload: Dict[str, Any] = {}
    if outcome is not None:
        if hasattr(outcome, "__dataclass_fields__"):
            payload = asdict(outcome)
        elif isinstance(outcome, dict):
            payload = outcome
    repo.update(
        job_id,
        status="COMPLETE" if ok else "FAILED",
        message=payload.get("message") if ok else None,
        error_message=error if not ok else payload.get("message"),
        filled_legs=len(payload.get("leg_fills") or []) if isinstance(payload.get("leg_fills"), list) else int(payload.get("filled_legs") or 0),
        result_json=json.dumps(payload, default=str) if payload else None,
        completed_at=now,
        updated_at=now,
    )
    db.commit()


def update_job_progress(
    db: SQLServerConnection,
    job_id: int,
    *,
    current_leg_order: Optional[int] = None,
    filled_legs: Optional[int] = None,
    message: Optional[str] = None,
) -> None:
    repo = ZerodhaExecutionJobRepo(db)
    repo.update(
        job_id,
        status="RUNNING",
        current_leg_order=current_leg_order,
        filled_legs=filled_legs,
        message=message,
        updated_at=now_ist(),
    )
    db.commit()


def job_status_dict(db: SQLServerConnection, job_id: int) -> Optional[dict]:
    repo = ZerodhaExecutionJobRepo(db)
    row = repo.get(job_id)
    if row is None:
        return None
    out = dict(row)
    parsed = repo.result_dict(row)
    if parsed:
        out["result"] = parsed
    if out.get("created_at") and hasattr(out["created_at"], "isoformat"):
        out["created_at"] = out["created_at"].isoformat()
    if out.get("updated_at") and hasattr(out["updated_at"], "isoformat"):
        out["updated_at"] = out["updated_at"].isoformat()
    if out.get("completed_at") and hasattr(out["completed_at"], "isoformat"):
        out["completed_at"] = out["completed_at"].isoformat()
    return out
