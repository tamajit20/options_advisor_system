"""
database/broker_order_repo.py
===============================

Persistence for Zerodha (Kite) orders — maps suggestion/trade legs to
broker order IDs and tracks fill status for audit and UI.
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Any, Dict, Iterable, List, Optional

from database.connection import SQLServerConnection


def net_unbooked_entry_fills(
    rows: Iterable[dict],
    *,
    except_job_id: Optional[int] = None,
) -> List[dict]:
    """COMPLETE ENTRY fills that still represent an open, unbooked position.

    Walks rows in id order. Each COMPLETE ENTRY with a Kite id adds quantity
    to a pool; each later COMPLETE ROLLBACK for the same leg consumes it.
    Whatever remains is a real leftover on Kite, not a retry artifact.
    """
    except_id: Optional[int] = None
    if except_job_id is not None:
        try:
            except_id = int(except_job_id)
        except (TypeError, ValueError):
            except_id = None

    def _qty(row: dict) -> int:
        try:
            return int(row.get("filled_quantity") or row.get("quantity") or 0)
        except (TypeError, ValueError):
            return 0

    def _job(row: dict) -> Optional[int]:
        raw = row.get("execution_job_id")
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    ordered = sorted(
        rows,
        key=lambda r: (int(r.get("id") or 0), str(r.get("created_at") or "")),
    )
    pool: List[dict] = []
    for row in ordered:
        op = str(row.get("operation") or "").upper()
        status = str(row.get("status") or "").upper()
        if op == "ENTRY" and status == "COMPLETE":
            if row.get("trade_id"):
                continue
            if not row.get("kite_order_id"):
                continue
            if except_id is not None and _job(row) == except_id:
                continue
            qty = _qty(row)
            if qty <= 0:
                continue
            pool.append({
                "row": row,
                "remaining": qty,
                "leg": int(row.get("leg_order") or 0),
                "sym": row.get("tradingsymbol"),
            })
            continue
        if op == "ROLLBACK" and status == "COMPLETE":
            qty = _qty(row)
            if qty <= 0:
                continue
            leg = int(row.get("leg_order") or 0)
            sym = row.get("tradingsymbol")
            for item in pool:
                if item["remaining"] <= 0 or item["leg"] != leg:
                    continue
                if sym and item["sym"] and item["sym"] != sym:
                    continue
                take = min(item["remaining"], qty)
                item["remaining"] -= take
                qty -= take
                if qty <= 0:
                    break
    return [item["row"] for item in pool if item["remaining"] > 0]


class BrokerOrderRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(self, row: Dict[str, Any]) -> int:
        cur = self.db.execute(
            """
            INSERT INTO options_broker_orders
              (operation, suggestion_id, trade_id, leg_order, kite_order_id,
               tradingsymbol, exchange, transaction_type, quantity, limit_price,
               fill_price, status, tag, error_message, retry_count,
               filled_quantity, pending_quantity, status_message, order_type,
               validity, execution_job_id, created_at, updated_at)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["operation"],
                row.get("suggestion_id"),
                row.get("trade_id"),
                int(row["leg_order"]),
                row.get("kite_order_id"),
                row["tradingsymbol"],
                row.get("exchange", "NFO"),
                row["transaction_type"],
                int(row["quantity"]),
                row.get("limit_price"),
                row.get("fill_price"),
                row.get("status", "PENDING"),
                row.get("tag"),
                row.get("error_message"),
                int(row.get("retry_count") or 0),
                row.get("filled_quantity"),
                row.get("pending_quantity"),
                row.get("status_message"),
                row.get("order_type"),
                row.get("validity"),
                row.get("execution_job_id"),
                row.get("created_at"),
                row.get("updated_at"),
            ],
        )
        out = cur.fetchone()
        cur.close()
        return int(out[0])

    def update_status(
        self,
        row_id: int,
        *,
        status: str,
        kite_order_id: Optional[str] = None,
        fill_price: Optional[float] = None,
        error_message: Optional[str] = None,
        retry_count: Optional[int] = None,
        filled_quantity: Optional[int] = None,
        pending_quantity: Optional[int] = None,
        status_message: Optional[str] = None,
        order_type: Optional[str] = None,
        validity: Optional[str] = None,
        limit_price: Optional[float] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        sets = ["status = ?", "updated_at = ?"]
        params: list = [status, updated_at]
        if kite_order_id is not None:
            sets.append("kite_order_id = ?")
            params.append(kite_order_id)
        if fill_price is not None:
            sets.append("fill_price = ?")
            params.append(fill_price)
        if error_message is not None:
            sets.append("error_message = ?")
            params.append(error_message)
        if retry_count is not None:
            sets.append("retry_count = ?")
            params.append(retry_count)
        if filled_quantity is not None:
            sets.append("filled_quantity = ?")
            params.append(filled_quantity)
        if pending_quantity is not None:
            sets.append("pending_quantity = ?")
            params.append(pending_quantity)
        if status_message is not None:
            sets.append("status_message = ?")
            params.append(status_message)
        if order_type is not None:
            sets.append("order_type = ?")
            params.append(order_type)
        if validity is not None:
            sets.append("validity = ?")
            params.append(validity)
        if limit_price is not None:
            sets.append("limit_price = ?")
            params.append(limit_price)
        params.append(row_id)
        self.db.execute(
            f"UPDATE options_broker_orders SET {', '.join(sets)} WHERE id = ?",
            params,
        ).close()

    def by_suggestion(self, suggestion_id: str) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_broker_orders WHERE suggestion_id = ? "
            "ORDER BY leg_order, id",
            [suggestion_id],
        ) or []

    def attach_trade_id(
        self,
        trade_id: str,
        *,
        suggestion_id: str,
        execution_job_id: Optional[int] = None,
    ) -> None:
        """Stamp ``trade_id`` on the rows this job just placed.

        Only the current job is linked. A prior failed attempt (IP reject,
        timeout) stays unlinked so history does not look like part of the trade.
        """
        if execution_job_id is not None:
            self.db.execute(
                "UPDATE options_broker_orders SET trade_id = ? "
                "WHERE suggestion_id = ? AND execution_job_id = ? AND trade_id IS NULL",
                [trade_id, suggestion_id, execution_job_id],
            ).close()
            return
        self.db.execute(
            "UPDATE options_broker_orders SET trade_id = ? "
            "WHERE suggestion_id = ? AND operation = 'ENTRY' AND trade_id IS NULL",
            [trade_id, suggestion_id],
        ).close()

    def by_trade(self, trade_id: str, *, operation: Optional[str] = None) -> List[dict]:
        if operation:
            return self.db.fetch_all(
                "SELECT * FROM options_broker_orders WHERE trade_id = ? AND operation = ? "
                "ORDER BY leg_order, id",
                [trade_id, operation],
            )
        return self.db.fetch_all(
            "SELECT * FROM options_broker_orders WHERE trade_id = ? ORDER BY leg_order, id",
            [trade_id],
        )

    def pending_for_suggestion(self, suggestion_id: str) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_broker_orders WHERE suggestion_id = ? "
            "AND status IN ('PENDING', 'OPEN', 'TRIGGER PENDING')",
            [suggestion_id],
        )

    def orphan_entry_fills(
        self,
        suggestion_id: str,
        *,
        except_job_id: Optional[int] = None,
    ) -> List[dict]:
        """Unbooked COMPLETE ENTRY fills that still mean an open Kite position.

        A retry must not be blocked by:
        - a prior attempt that never reached Kite (FAILED, no order id)
        - a prior attempt that filled and was fully rolled back
        - the current job's own fills, which are about to be booked
        """
        return net_unbooked_entry_fills(
            self.by_suggestion(suggestion_id),
            except_job_id=except_job_id,
        )

    def has_kite_orders_for_trade(self, trade_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT TOP 1 1 AS x FROM options_broker_orders "
            "WHERE trade_id = ? AND kite_order_id IS NOT NULL",
            [trade_id],
        )
        return row is not None

    def pending_for_trade(self, trade_id: str, *, operation: Optional[str] = None) -> List[dict]:
        clauses = [
            "trade_id = ?",
            "status IN ('PENDING', 'OPEN', 'TRIGGER PENDING')",
        ]
        params: list = [trade_id]
        if operation:
            clauses.append("operation = ?")
            params.append(operation)
        where = " AND ".join(clauses)
        return self.db.fetch_all(
            f"SELECT * FROM options_broker_orders WHERE {where} ORDER BY leg_order, id",
            params,
        )

    def list_since(
        self,
        since: datetime,
        *,
        limit: int = 500,
        trade_id: Optional[str] = None,
        suggestion_id: Optional[str] = None,
    ) -> List[dict]:
        clauses = ["created_at >= ?"]
        params: list = [int(limit), since]
        if trade_id:
            clauses.append("trade_id = ?")
            params.append(trade_id)
        if suggestion_id:
            clauses.append("suggestion_id = ?")
            params.append(suggestion_id)
        where = " AND ".join(clauses)
        return self.db.fetch_all(
            f"SELECT TOP (?) * FROM options_broker_orders WHERE {where} "
            "ORDER BY created_at DESC, id DESC",
            params,
        )

    def by_job(self, job_id: int) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_broker_orders WHERE execution_job_id = ? "
            "ORDER BY leg_order, id",
            [job_id],
        )

    def rollback_complete_for_leg(
        self,
        *,
        suggestion_id: Optional[str],
        trade_id: Optional[str],
        leg_order: int,
    ) -> bool:
        clauses = ["operation = 'ROLLBACK'", "leg_order = ?", "status = 'COMPLETE'"]
        params: list = [leg_order]
        if trade_id:
            clauses.append("trade_id = ?")
            params.append(trade_id)
        elif suggestion_id:
            clauses.append("suggestion_id = ?")
            params.append(suggestion_id)
        row = self.db.fetch_one(
            f"SELECT TOP 1 1 AS x FROM options_broker_orders WHERE {' AND '.join(clauses)}",
            params,
        )
        return row is not None

    def delete_older_than(self, cutoff: date) -> int:
        """Delete broker order audit rows with ``created_at`` before ``cutoff``."""
        cur = self.db.execute(
            "DELETE FROM options_broker_orders WHERE created_at < ?",
            [datetime.combine(cutoff, datetime.min.time())],
        )
        n = cur.rowcount or 0
        cur.close()
        return n
