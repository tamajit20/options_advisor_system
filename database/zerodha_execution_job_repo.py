"""
database/zerodha_execution_job_repo.py
=========================================

Async Zerodha execution job tracking.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from database.connection import SQLServerConnection


class ZerodhaExecutionJobRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(self, row: Dict[str, Any]) -> int:
        cur = self.db.execute(
            """
            INSERT INTO options_zerodha_execution_jobs
              (operation, suggestion_id, trade_id, status, current_leg_order,
               total_legs, filled_legs, message, error_message, result_json,
               created_at, updated_at, completed_at)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["operation"],
                row.get("suggestion_id"),
                row.get("trade_id"),
                row.get("status", "PENDING"),
                row.get("current_leg_order"),
                int(row.get("total_legs") or 0),
                int(row.get("filled_legs") or 0),
                row.get("message"),
                row.get("error_message"),
                row.get("result_json"),
                row.get("created_at"),
                row.get("updated_at"),
                row.get("completed_at"),
            ],
        )
        out = cur.fetchone()
        cur.close()
        return int(out[0])

    def update(
        self,
        job_id: int,
        *,
        status: Optional[str] = None,
        current_leg_order: Optional[int] = None,
        filled_legs: Optional[int] = None,
        message: Optional[str] = None,
        error_message: Optional[str] = None,
        result_json: Optional[str] = None,
        completed_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        sets: List[str] = []
        params: list = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if current_leg_order is not None:
            sets.append("current_leg_order = ?")
            params.append(current_leg_order)
        if filled_legs is not None:
            sets.append("filled_legs = ?")
            params.append(filled_legs)
        if message is not None:
            sets.append("message = ?")
            params.append(message)
        if error_message is not None:
            sets.append("error_message = ?")
            params.append(error_message)
        if result_json is not None:
            sets.append("result_json = ?")
            params.append(result_json)
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(completed_at)
        if updated_at is not None:
            sets.append("updated_at = ?")
            params.append(updated_at)
        if not sets:
            return
        params.append(job_id)
        self.db.execute(
            f"UPDATE options_zerodha_execution_jobs SET {', '.join(sets)} WHERE id = ?",
            params,
        ).close()

    def get(self, job_id: int) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM options_zerodha_execution_jobs WHERE id = ?",
            [job_id],
        )

    def latest_for_suggestion(self, suggestion_id: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT TOP 1 * FROM options_zerodha_execution_jobs "
            "WHERE suggestion_id = ? ORDER BY created_at DESC, id DESC",
            [suggestion_id],
        )

    def list_for_suggestions(self, suggestion_ids: List[str]) -> List[dict]:
        seen: List[str] = []
        for sid in suggestion_ids:
            text = (sid or "").strip()
            if text and text not in seen:
                seen.append(text)
        if not seen:
            return []
        placeholders = ",".join("?" * len(seen))
        return self.db.fetch_all(
            "SELECT * FROM options_zerodha_execution_jobs "
            f"WHERE suggestion_id IN ({placeholders}) ORDER BY id",
            seen,
        ) or []

    def latest_for_trade(self, trade_id: str, *, operation: Optional[str] = None) -> Optional[dict]:
        if operation:
            return self.db.fetch_one(
                "SELECT TOP 1 * FROM options_zerodha_execution_jobs "
                "WHERE trade_id = ? AND operation = ? "
                "ORDER BY created_at DESC, id DESC",
                [trade_id, operation],
            )
        return self.db.fetch_one(
            "SELECT TOP 1 * FROM options_zerodha_execution_jobs "
            "WHERE trade_id = ? ORDER BY created_at DESC, id DESC",
            [trade_id],
        )

    def running_for_suggestion(self, suggestion_id: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT TOP 1 * FROM options_zerodha_execution_jobs "
            "WHERE suggestion_id = ? AND status = 'RUNNING' "
            "ORDER BY created_at DESC, id DESC",
            [suggestion_id],
        )

    def running_for_trade(self, trade_id: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT TOP 1 * FROM options_zerodha_execution_jobs "
            "WHERE trade_id = ? AND status = 'RUNNING' "
            "ORDER BY created_at DESC, id DESC",
            [trade_id],
        )

    def delete_older_than(self, cutoff: date) -> int:
        from database.retention import delete_older_than as _batched

        return _batched(self.db, "options_zerodha_execution_jobs", "created_at", cutoff)

    @staticmethod
    def result_dict(job: dict) -> Optional[dict]:
        raw = job.get("result_json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
