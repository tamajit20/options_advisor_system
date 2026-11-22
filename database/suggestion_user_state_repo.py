"""Per-user suggestion status + Amount needed so parallel execute stays isolated."""

from __future__ import annotations

from typing import Dict, List, Optional

from database.connection import SQLServerConnection
from utils import now_ist


class SuggestionUserStateRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def get(self, suggestion_id: str, user_id: int) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM options_suggestion_user_state "
            "WHERE suggestion_id = ? AND user_id = ?",
            [suggestion_id, int(user_id)],
        )

    def status_for_user(self, suggestion_id: str, user_id: int) -> str:
        row = self.get(suggestion_id, user_id)
        if row and row.get("status"):
            return str(row["status"]).upper()
        return "PENDING"

    def upsert(
        self,
        suggestion_id: str,
        user_id: int,
        status: str,
        *,
        trade_id: Optional[str] = None,
    ) -> None:
        status_u = str(status or "PENDING").upper()
        existing = self.get(suggestion_id, user_id)
        now = now_ist().replace(tzinfo=None)
        if existing:
            self.db.execute(
                "UPDATE options_suggestion_user_state "
                "SET status = ?, trade_id = COALESCE(?, trade_id), updated_at = ? "
                "WHERE suggestion_id = ? AND user_id = ?",
                [status_u, trade_id, now, suggestion_id, int(user_id)],
            ).close()
        else:
            self.db.execute(
                "INSERT INTO options_suggestion_user_state "
                "(suggestion_id, user_id, status, trade_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [suggestion_id, int(user_id), status_u, trade_id, now],
            ).close()

    def save_zerodha_margin_once(
        self,
        suggestion_id: str,
        user_id: int,
        *,
        margin_final: float,
        margin_peak: Optional[float] = None,
        lots: Optional[int] = None,
    ) -> bool:
        """One-time write of this user's Final/Peak for the suggestion card."""
        try:
            fin = float(margin_final)
        except (TypeError, ValueError):
            return False
        if fin <= 0:
            return False
        existing = self.get(suggestion_id, user_id)
        if existing and existing.get("zerodha_margin_final") is not None:
            return False
        now = now_ist().replace(tzinfo=None)
        peak = None
        if margin_peak is not None:
            try:
                peak = float(margin_peak)
            except (TypeError, ValueError):
                peak = None
        lots_i = None
        if lots is not None:
            try:
                lots_i = int(lots)
            except (TypeError, ValueError):
                lots_i = None
        if existing:
            self.db.execute(
                "UPDATE options_suggestion_user_state "
                "SET zerodha_margin_final = ?, zerodha_margin_peak = ?, "
                "    zerodha_margin_lots = ?, zerodha_margin_quoted_at = ?, "
                "    updated_at = ? "
                "WHERE suggestion_id = ? AND user_id = ? "
                "  AND zerodha_margin_final IS NULL",
                [fin, peak, lots_i, now, now, suggestion_id, int(user_id)],
            ).close()
        else:
            self.db.execute(
                "INSERT INTO options_suggestion_user_state "
                "(suggestion_id, user_id, status, trade_id, updated_at, "
                " zerodha_margin_final, zerodha_margin_peak, "
                " zerodha_margin_lots, zerodha_margin_quoted_at) "
                "VALUES (?, ?, 'PENDING', NULL, ?, ?, ?, ?, ?)",
                [suggestion_id, int(user_id), now, fin, peak, lots_i, now],
            ).close()
        return True

    def map_for_user(self, suggestion_ids: List[str], user_id: int) -> Dict[str, dict]:
        ids = [s for s in suggestion_ids if s]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.db.fetch_all(
            f"SELECT * FROM options_suggestion_user_state "
            f"WHERE user_id = ? AND suggestion_id IN ({placeholders})",
            [int(user_id), *ids],
        ) or []
        return {str(r["suggestion_id"]): r for r in rows}
