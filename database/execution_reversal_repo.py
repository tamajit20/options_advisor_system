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

    def list_between(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> List[dict]:
        filters = ["1 = 1"]
        params: list = []
        if from_date:
            filters.append("CONVERT(date, created_at) >= ?")
            params.append(from_date)
        if to_date:
            filters.append("CONVERT(date, created_at) <= ?")
            params.append(to_date)
        where = " AND ".join(filters)
        return self.db.fetch_all(
            f"SELECT * FROM options_execution_reversals WHERE {where} "
            "ORDER BY created_at ASC",
            params,
        ) or []
