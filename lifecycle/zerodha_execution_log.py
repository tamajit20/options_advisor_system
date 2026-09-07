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
    jobs: Optional[Iterable[dict]] = None,
) -> List[dict]:
    """Cluster order rows by job, then trade, then suggestion.

    ``jobs`` is optional ``options_zerodha_execution_jobs`` rows used to
    explain *why* an attempt failed or was flattened.
    """
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

        bucket["orders"].append(dict(row))

    out: List[dict] = [_refresh_group_meta(b) for b in buckets.values()]
    out = _merge_unlinked_rollbacks(out)

    jobs_by_id: Dict[int, dict] = {}
    for job in jobs or []:
        jid = job.get("id")
        if jid is None or jid == "":
            continue
        try:
            jobs_by_id[int(jid)] = job
        except (TypeError, ValueError):
            continue

    for bucket in out:
        if bucket.get("started_at") is not None:
            started = bucket["started_at"]
            bucket["started_at"] = (
                started.isoformat() if isinstance(started, datetime) else str(started)
            )
        if bucket.get("last_at") is not None:
            last = bucket["last_at"]
            bucket["last_at"] = last.isoformat() if isinstance(last, datetime) else str(last)
        bucket.update(describe_broker_group(bucket, jobs_by_id))

    out.sort(
        key=lambda g: g.get("last_at") or "",
        reverse=True,
    )
    return out


def _sort_orders(orders: List[dict]) -> None:
    orders.sort(
        key=lambda o: (
            _as_dt(o.get("created_at")) or datetime.min,
            int(o.get("leg_order") or 0),
            int(o.get("id") or 0),
        )
    )


def _refresh_group_meta(bucket: dict) -> dict:
    orders = bucket.get("orders") or []
    _sort_orders(orders)
    started = None
    last = None
    ops: List[str] = []
    for row in orders:
        created = _as_dt(row.get("created_at"))
        updated = _as_dt(row.get("updated_at")) or created
        if created is not None and (started is None or created < started):
            started = created
        if updated is not None and (last is None or updated > last):
            last = updated
        op = (row.get("operation") or "").upper()
        if op and op not in ops:
            ops.append(op)
    bucket["started_at"] = started
    bucket["last_at"] = last
    bucket["operations"] = sorted(ops)
    bucket["overall_status"] = _overall_status(orders)
    return bucket


def _job_id_from_key(group_key: Optional[str]) -> Optional[int]:
    if not group_key or not str(group_key).startswith("job:"):
        return None
    try:
        return int(str(group_key).split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        return None


def _is_rollback_only(group: dict) -> bool:
    ops = {str(o).upper() for o in (group.get("operations") or [])}
    if ops == {"ROLLBACK"}:
        return True
    orders = group.get("orders") or []
    return bool(orders) and all(
        str(o.get("operation") or "").upper() == "ROLLBACK" for o in orders
    )


def _time_key(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _merge_unlinked_rollbacks(groups: List[dict]) -> List[dict]:
    """Fold ROLLBACK-only cards with no job id into the nearest prior ENTRY.

    Older flatten rows were stored without ``execution_job_id``, so they
    appeared as a second COMPLETE card for the same suggestion.
    """
    others: List[dict] = []
    rollbacks: List[dict] = []
    for group in groups:
        if _is_rollback_only(group) and _job_id_from_key(group.get("group_key")) is None:
            rollbacks.append(group)
        else:
            others.append(group)

    leftover: List[dict] = []
    for rb in rollbacks:
        sid = rb.get("suggestion_id")
        rb_start = _time_key(rb.get("started_at"))
        candidates = [
            g for g in others
            if g.get("suggestion_id") == sid
            and "ENTRY" in (g.get("operations") or [])
            and _time_key(g.get("last_at")) <= rb_start
        ]
        if not candidates:
            leftover.append(rb)
            continue
        target = max(candidates, key=lambda g: _time_key(g.get("last_at")))
        target["orders"].extend(rb.get("orders") or [])
        if not target.get("trade_id") and rb.get("trade_id"):
            target["trade_id"] = rb["trade_id"]
        _refresh_group_meta(target)

    return others + leftover


def _human_error(msg: Optional[str]) -> Optional[str]:
    """Turn a raw Kite / job error into a short audit reason."""
    if not msg:
        return None
    text = " ".join(str(msg).split())
    if not text:
        return None
    low = text.lower()
    if "no ips configured" in low or (
        "ip" in low and ("whitelist" in low or "not enabled for this ip" in low)
    ):
        return (
            "Kite rejected the order because no static IPs were set on the "
            "developer app."
        )
    if "already running for this suggestion" in low:
        return (
            "A previous execution was still marked running, so this click "
            "was blocked."
        )
    if (
        "without a recorded trade" in low
        or "leftover" in low
        or "flatten on kite" in low
        or "orphan" in low
    ):
        return (
            "The app refused to save the trade (fills looked like leftovers), "
            "then flattened both legs."
        )
    cut = len(text)
    for marker in (" Learn more", " https://", " http://"):
        idx = low.find(marker.lower())
        if idx > 0:
            cut = min(cut, idx)
    text = text[:cut].rstrip(" -.:")
    return text[:240] or None


def describe_broker_group(
    group: dict,
    jobs_by_id: Optional[Dict[int, dict]] = None,
) -> dict:
    """Actor headline + reason for the audit card (collapsed row)."""
    orders = group.get("orders") or []
    ops = {str(o.get("operation") or "").upper() for o in orders}
    statuses = {str(o.get("status") or "").upper() for o in orders}
    err = next((o.get("error_message") for o in orders if o.get("error_message")), None)
    jid = _job_id_from_key(group.get("group_key"))
    job = (jobs_by_id or {}).get(jid) if jid else None
    if job:
        err = err or job.get("error_message") or job.get("message")
    reason = _human_error(err)

    has_entry = "ENTRY" in ops
    has_rollback = "ROLLBACK" in ops
    has_exit = "EXIT" in ops
    any_complete = "COMPLETE" in statuses
    any_fail = bool(statuses & _TERMINAL_FAIL)
    all_fail = bool(statuses) and statuses <= _TERMINAL_FAIL

    if has_entry and has_exit and not has_rollback:
        return {
            "actor": "you",
            "headline": "You placed entry, then closed",
            "detail": reason or "Entry and exit are recorded on the same trade.",
            "badge": group.get("overall_status") or "COMPLETE",
        }
    if has_rollback and has_entry:
        return {
            "actor": "both",
            "headline": "You placed entry · system reverted",
            "detail": reason or (
                "The fills could not be saved as a trade, so the system "
                "flattened the position."
            ),
            "badge": "REVERTED",
        }
    if has_rollback:
        return {
            "actor": "system",
            "headline": "System reverted",
            "detail": reason or (
                "Closed the legs this click had just opened because the "
                "trade was not recorded."
            ),
            "badge": "REVERTED",
        }
    if has_exit and not has_entry:
        if all_fail:
            return {
                "actor": "you",
                "headline": "You closed the trade — failed",
                "detail": reason or "Kite rejected the exit.",
                "badge": "FAILED",
            }
        return {
            "actor": "you",
            "headline": "You closed the trade",
            "detail": reason,
            "badge": group.get("overall_status") or "COMPLETE",
        }
    if has_entry and (all_fail or (any_fail and not any_complete)):
        return {
            "actor": "you",
            "headline": "You placed entry — failed",
            "detail": reason or "Kite rejected the order.",
            "badge": "FAILED",
        }
    if has_entry and any_fail and any_complete:
        return {
            "actor": "you",
            "headline": "You placed entry — partial",
            "detail": reason or "Some legs filled; others failed.",
            "badge": "PARTIAL",
        }
    if has_entry and group.get("trade_id"):
        tid = group["trade_id"]
        return {
            "actor": "you",
            "headline": "You placed entry",
            "detail": f"Trade {tid} recorded.",
            "badge": "COMPLETE",
        }
    if has_entry:
        return {
            "actor": "you",
            "headline": "You placed entry",
            "detail": reason or "Legs filled on Kite. No trade was recorded in the app.",
            "badge": group.get("overall_status") or "COMPLETE",
        }
    return {
        "actor": "system",
        "headline": "Broker order",
        "detail": reason,
        "badge": group.get("overall_status") or "UNKNOWN",
    }


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
