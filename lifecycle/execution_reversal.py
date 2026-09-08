"""Record auto-revert P&L after Zerodha flatten, without creating a trade."""

from __future__ import annotations

import logging
from typing import List, Optional

from contracts import Notification
from database.broker_order_repo import BrokerOrderRepo
from database.connection import SQLServerConnection
from database.execution_reversal_repo import ExecutionReversalRepo
from database.models import NotificationRepo
from engine.reversal_pnl import compute_reversal_from_orders
from utils import now_ist

logger = logging.getLogger(__name__)


def record_reversal_from_orders(
    db: SQLServerConnection,
    orders: List[dict],
    *,
    suggestion_id: Optional[str] = None,
    trade_id: Optional[str] = None,
    execution_job_id: Optional[int] = None,
    reason: str = "ENTRY_ROLLBACK",
) -> Optional[dict]:
    """Insert a reversal ledger row if these fills are a flatten round-trip."""
    open_op = "EXIT" if reason.startswith("EXIT") else "ENTRY"
    snap = compute_reversal_from_orders(orders, open_operation=open_op)
    if snap is None:
        return None
    repo = ExecutionReversalRepo(db)
    existing = repo.get_by_fingerprint(snap["order_fingerprint"])
    if existing:
        return existing
    if execution_job_id is not None:
        by_job = repo.get_by_job(execution_job_id)
        if by_job:
            return by_job
    row = {
        **snap,
        "suggestion_id": suggestion_id,
        "trade_id": trade_id,
        "execution_job_id": execution_job_id,
        "reason": reason,
        "created_at": now_ist(),
    }
    row_id = repo.insert(row)
    row["id"] = row_id
    db.commit()
    _notify(db, row)
    logger.info(
        "execution reversal recorded id=%s job=%s net=%.2f charges=%.2f",
        row_id, execution_job_id, row["net_pnl"], row["total_charges"],
    )
    return row


def record_reversal_after_flatten(
    db: SQLServerConnection,
    *,
    suggestion_id: Optional[str],
    trade_id: Optional[str],
    execution_job_id: Optional[int],
    reason: str,
) -> Optional[dict]:
    broker = BrokerOrderRepo(db)
    orders: List[dict] = []
    if execution_job_id is not None:
        orders = broker.by_job(execution_job_id)
    if not isinstance(orders, (list, tuple)):
        orders = []
    if not orders and suggestion_id:
        orders = broker.by_suggestion(suggestion_id)
    if not isinstance(orders, (list, tuple)):
        orders = []
    if not orders and trade_id:
        orders = broker.by_trade(trade_id)
    if not isinstance(orders, (list, tuple)):
        orders = []
    if not orders:
        return None
    try:
        return record_reversal_from_orders(
            db, orders,
            suggestion_id=suggestion_id,
            trade_id=trade_id,
            execution_job_id=execution_job_id,
            reason=reason,
        )
    except Exception:
        logger.exception("failed to record execution reversal P&L")
        try:
            db.rollback()
        except Exception:
            pass
        return None


def attach_reversals_to_groups(db: SQLServerConnection, groups: List[dict]) -> None:
    """Backfill + stamp reversal P&L onto execution-log cards."""
    repo = ExecutionReversalRepo(db)
    broker = BrokerOrderRepo(db)
    for group in groups:
        ops = {str(o).upper() for o in (group.get("operations") or [])}
        if "ROLLBACK" not in ops:
            continue
        jid = None
        key = str(group.get("group_key") or "")
        if key.startswith("job:"):
            try:
                jid = int(key.split(":", 1)[1])
            except (TypeError, ValueError, IndexError):
                jid = None
        orders = group.get("orders") or []
        if jid is not None:
            fetched = broker.by_job(jid)
            if fetched:
                orders = fetched
        reason = "EXIT_ROLLBACK" if "EXIT" in ops else "ENTRY_ROLLBACK"
        row = record_reversal_from_orders(
            db, orders,
            suggestion_id=group.get("suggestion_id"),
            trade_id=group.get("trade_id"),
            execution_job_id=jid,
            reason=reason,
        )
        if row is None and jid is not None:
            row = repo.get_by_job(jid)
        if not row:
            continue
        group["reversal"] = {
            "id": row.get("id"),
            "gross_pnl": float(row.get("gross_pnl") or 0),
            "total_charges": float(row.get("total_charges") or 0),
            "net_pnl": float(row.get("net_pnl") or 0),
        }
        extra = (
            f" Round-trip P&L ₹{float(row['net_pnl']):.2f} "
            f"(gross ₹{float(row['gross_pnl']):.2f}, "
            f"charges ₹{float(row['total_charges']):.2f})."
        )
        detail = str(group.get("detail") or "").rstrip()
        if "Round-trip P&L" not in detail:
            group["detail"] = (detail + extra).strip()


def _notify(db: SQLServerConnection, row: dict) -> None:
    try:
        net = float(row["net_pnl"])
        sign = "+" if net >= 0 else ""
        NotificationRepo(db).insert(Notification(
            created_at=now_ist(),
            notif_type="EXECUTION_REVERSAL_PNL",
            severity="INFO",
            title="Reverted fill P&L recorded",
            body=(
                f"Auto-flatten round-trip {sign}₹{net:.2f} "
                f"(charges ₹{float(row['total_charges']):.2f}). "
                "This is booked separately from live trades."
            ),
            related_suggestion_id=row.get("suggestion_id"),
            related_trade_id=row.get("trade_id"),
        ))
        db.commit()
    except Exception:
        logger.debug("reversal P&L notification failed", exc_info=True)
