"""
lifecycle/zerodha_execution_log.py
==================================

Group ``options_broker_orders`` rows into trade/suggestion execution
sessions for the dashboard audit view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


_TERMINAL_FAIL = frozenset({"FAILED", "REJECTED", "CANCELLED"})


def _as_dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def group_broker_orders(
    rows: Iterable[dict],
    *,
    trade_names: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Cluster order rows by ``trade_id`` (preferred) or ``suggestion_id``."""
    trade_names = trade_names or {}
    buckets: Dict[str, dict] = {}

    for row in rows:
        trade_id = (row.get("trade_id") or "").strip() or None
        suggestion_id = (row.get("suggestion_id") or "").strip() or None
        job_id = row.get("execution_job_id")
        # One card per attempt. Mixing a failed IP reject with a later fill
        # made the live view look like 4/5 legs when two of those rows were
        # a rollback of the retry.
        if job_id is not None and job_id != "":
            group_key = f"job:{job_id}"
        else:
            group_key = trade_id or suggestion_id
        if not group_key:
            group_key = f"orphan-{row.get('id')}"

        bucket = buckets.get(group_key)
        if bucket is None:
            bucket = {
                "group_key": group_key,
                "trade_id": trade_id,
                "suggestion_id": suggestion_id,
                "trade_name": trade_names.get(trade_id or "") if trade_id else None,
                "started_at": None,
                "last_at": None,
                "overall_status": "UNKNOWN",
                "operations": [],
                "orders": [],
            }
            buckets[group_key] = bucket

        if trade_id and not bucket.get("trade_id"):
            bucket["trade_id"] = trade_id
        if suggestion_id and not bucket.get("suggestion_id"):
            bucket["suggestion_id"] = suggestion_id
        if trade_id and trade_names.get(trade_id):
            bucket["trade_name"] = trade_names[trade_id]

        order = dict(row)
        bucket["orders"].append(order)

        created = _as_dt(row.get("created_at"))
        updated = _as_dt(row.get("updated_at")) or created
        if created is not None:
            if bucket["started_at"] is None or created < bucket["started_at"]:
                bucket["started_at"] = created
        if updated is not None:
            if bucket["last_at"] is None or updated > bucket["last_at"]:
                bucket["last_at"] = updated

        op = (row.get("operation") or "").upper()
        if op and op not in bucket["operations"]:
            bucket["operations"].append(op)

    out: List[dict] = []
    for bucket in buckets.values():
        bucket["orders"].sort(
            key=lambda o: (
                _as_dt(o.get("created_at")) or datetime.min,
                int(o.get("leg_order") or 0),
                int(o.get("id") or 0),
            )
        )
        bucket["overall_status"] = _overall_status(bucket["orders"])
        bucket["operations"] = sorted(bucket["operations"])
        if bucket["started_at"] is not None:
            bucket["started_at"] = bucket["started_at"].isoformat()
        if bucket["last_at"] is not None:
            bucket["last_at"] = bucket["last_at"].isoformat()
        out.append(bucket)

    out.sort(
        key=lambda g: g.get("last_at") or "",
        reverse=True,
    )
    return out


def _overall_status(orders: List[dict]) -> str:
    if not orders:
        return "UNKNOWN"
    statuses = {(o.get("status") or "").upper() for o in orders}
    if statuses <= {"COMPLETE"}:
        return "COMPLETE"
    if statuses & {"OPEN", "PENDING", "TRIGGER PENDING"}:
        if statuses & {"COMPLETE", "FAILED", "REJECTED", "CANCELLED"}:
            return "PARTIAL"
        return "IN_FLIGHT"
    if statuses & _TERMINAL_FAIL:
        if "COMPLETE" in statuses:
            return "PARTIAL"
        return "FAILED"
    return "UNKNOWN"


def live_suggestion_order_status(
    rows: List[dict],
    latest_job: Optional[dict] = None,
) -> dict:
    """Progress for the suggestion card — latest attempt only.

    Historical FAILED rows and rollbacks from a previous click must not
    change filled/total or keep the inflight panel open.
    """
    job_status = str((latest_job or {}).get("status") or "").upper()
    job_id = (latest_job or {}).get("id")
    focused = list(rows)
    if job_id is not None:
        focused = [
            r for r in rows
            if str(r.get("execution_job_id") or "") == str(job_id)
        ]
    entries = [
        r for r in focused
        if str(r.get("operation") or "").upper() == "ENTRY"
    ]
    filled = sum(
        1 for r in entries if str(r.get("status") or "").upper() == "COMPLETE"
    )
    try:
        total = int((latest_job or {}).get("total_legs") or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        total = len(entries)

    if not latest_job and not focused:
        overall = "NONE"
    elif job_status == "RUNNING":
        overall = _overall_status(entries) if entries else "IN_FLIGHT"
        if overall == "UNKNOWN":
            overall = "IN_FLIGHT"
    elif job_status == "FAILED":
        overall = "FAILED"
    elif job_status in ("COMPLETE", "SUCCESS"):
        overall = "COMPLETE"
    elif entries:
        overall = _overall_status(entries)
    else:
        overall = "NONE"

    trade_id = next((r.get("trade_id") for r in focused if r.get("trade_id")), None)
    return {
        "orders": focused,
        "overall_status": overall,
        "filled_count": filled,
        "total_orders": total,
        "trade_id": trade_id,
    }
