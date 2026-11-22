"""Persist round-trip P&L for auto-reverted Zerodha fills."""

from __future__ import annotations

from typing import List, Optional

from database.connection import SQLServerConnection


class ExecutionReversalRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def get_by_fingerprint(self, fingerprint: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM options_execution_reversals WHERE order_fingerprint = ?",
            [fingerprint],
        )

    def get_by_job(self, job_id: int) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT TOP 1 * FROM options_execution_reversals "
            "WHERE execution_job_id = ? ORDER BY id DESC",
            [job_id],
        )

    def insert(self, row: dict) -> int:
        cur = self.db.execute(
            """
            INSERT INTO options_execution_reversals
              (suggestion_id, trade_id, execution_job_id, reason, created_at,
               gross_pnl, total_charges, net_pnl, brokerage, stt,
               exchange_charges, sebi, stamp_duty, gst, matched_qty,
               order_fingerprint, legs_json, notes)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row.get("suggestion_id"),
                row.get("trade_id"),
                row.get("execution_job_id"),
                row["reason"],
                row["created_at"],
                row["gross_pnl"],
                row["total_charges"],
                row["net_pnl"],
                row.get("brokerage"),
                row.get("stt"),
                row.get("exchange_charges"),
                row.get("sebi"),
                row.get("stamp_duty"),
                row.get("gst"),
                int(row.get("matched_qty") or 0),
                row["order_fingerprint"],
                row.get("legs_json"),
                row.get("notes"),
            ],
        )
        out = cur.fetchone()
        cur.close()
        return int(out[0])

    @staticmethod
    def _owner_scope_sql(alias: str = "r") -> str:
        """Resolve reversal owner via linked trade / broker orders."""
        return (
            f"COALESCE("
            f"(SELECT TOP 1 t.owner_user_id FROM options_trades t "
            f" WHERE {alias}.trade_id IS NOT NULL AND t.trade_id = {alias}.trade_id), "
            f"(SELECT TOP 1 o.owner_user_id FROM options_broker_orders o "
            f" WHERE {alias}.execution_job_id IS NOT NULL "
            f"   AND o.execution_job_id = {alias}.execution_job_id "
            f"   AND o.owner_user_id IS NOT NULL "
            f" ORDER BY o.created_at DESC), "
            f"(SELECT TOP 1 o.owner_user_id FROM options_broker_orders o "
            f" WHERE {alias}.suggestion_id IS NOT NULL "
            f"   AND o.suggestion_id = {alias}.suggestion_id "
            f"   AND o.owner_user_id IS NOT NULL "
            f" ORDER BY o.created_at DESC)"
            f")"
        )

    def list_between(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        owner_user_id: Optional[int] = None,
    ) -> List[dict]:
        filters = ["1 = 1"]
        params: list = []
        if from_date:
            filters.append("CONVERT(date, r.created_at) >= ?")
            params.append(from_date)
        if to_date:
            filters.append("CONVERT(date, r.created_at) <= ?")
            params.append(to_date)
        if owner_user_id is not None:
            filters.append(f"{self._owner_scope_sql('r')} = ?")
            params.append(int(owner_user_id))
        where = " AND ".join(filters)
        return self.db.fetch_all(
            f"SELECT r.* FROM options_execution_reversals r WHERE {where} "
            "ORDER BY r.created_at ASC",
            params,
        ) or []
