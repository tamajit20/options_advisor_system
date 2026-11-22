"""Per-app-user Zerodha access tokens stored in SQL."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from database.connection import SQLServerConnection
from utils import now_ist


class ZerodhaSessionRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def get(self, user_id: int) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM zerodha_sessions WHERE user_id = ?",
            [int(user_id)],
        )

    def upsert(
        self,
        user_id: int,
        *,
        access_token: str,
        kite_user_id: str = "",
        api_key: Optional[str] = None,
        generated_at: Optional[datetime] = None,
    ) -> None:
        gen = generated_at or now_ist().replace(tzinfo=None)
        if getattr(gen, "tzinfo", None) is not None:
            gen = gen.replace(tzinfo=None)
        now = now_ist().replace(tzinfo=None)
        existing = self.get(user_id)
        if existing:
            self.db.execute(
                "UPDATE zerodha_sessions SET kite_user_id = ?, access_token = ?, "
                "api_key = ?, generated_at = ?, updated_at = ? WHERE user_id = ?",
                [
                    kite_user_id or None,
                    access_token,
                    api_key,
                    gen,
                    now,
                    int(user_id),
                ],
            ).close()
        else:
            self.db.execute(
                "INSERT INTO zerodha_sessions "
                "(user_id, kite_user_id, access_token, api_key, generated_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    int(user_id),
                    kite_user_id or None,
                    access_token,
                    api_key,
                    gen,
                    now,
                ],
            ).close()

    def clear(self, user_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM zerodha_sessions WHERE user_id = ?",
            [int(user_id)],
        )
        n = cur.rowcount if cur.rowcount is not None else 0
        cur.close()
        return n > 0
