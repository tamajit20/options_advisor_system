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
