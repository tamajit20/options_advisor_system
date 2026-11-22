"""Per-app-user Zerodha Kite app credentials + access tokens (SQL)."""

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

    def list_user_ids_with_token(self) -> list:
        """App user ids that currently have a Zerodha access token."""
        rows = self.db.fetch_all(
            "SELECT user_id FROM zerodha_sessions "
            "WHERE access_token IS NOT NULL AND LTRIM(RTRIM(access_token)) <> ''"
        ) or []
        out = []
        for r in rows:
            try:
                out.append(int(r["user_id"]))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def set_app_credentials(
        self,
        user_id: int,
        *,
        api_key: str,
        api_secret: str,
    ) -> None:
        """Store this user's Kite Connect app key/secret (keeps any live token)."""
        key = (api_key or "").strip()
        secret = (api_secret or "").strip()
        if not key or not secret:
            raise ValueError("api_key and api_secret are required")
        now = now_ist().replace(tzinfo=None)
        existing = self.get(user_id)
        if existing:
            self.db.execute(
                "UPDATE zerodha_sessions SET api_key = ?, api_secret = ?, "
                "updated_at = ? WHERE user_id = ?",
                [key, secret, now, int(user_id)],
            ).close()
        else:
            self.db.execute(
                "INSERT INTO zerodha_sessions "
                "(user_id, kite_user_id, access_token, api_key, api_secret, "
                " generated_at, updated_at) "
                "VALUES (?, NULL, NULL, ?, ?, NULL, ?)",
                [int(user_id), key, secret, now],
            ).close()

    def upsert_token(
        self,
        user_id: int,
        *,
        access_token: str,
        kite_user_id: str = "",
        api_key: Optional[str] = None,
        generated_at: Optional[datetime] = None,
    ) -> None:
        """Save / refresh the daily access token; preserve stored api_secret."""
        gen = generated_at or now_ist().replace(tzinfo=None)
        if getattr(gen, "tzinfo", None) is not None:
            gen = gen.replace(tzinfo=None)
        now = now_ist().replace(tzinfo=None)
        existing = self.get(user_id)
        if existing:
            # Keep previous api_key when caller omits it.
            key = api_key if api_key is not None else existing.get("api_key")
            self.db.execute(
                "UPDATE zerodha_sessions SET kite_user_id = ?, access_token = ?, "
                "api_key = ?, generated_at = ?, updated_at = ? WHERE user_id = ?",
                [
                    kite_user_id or None,
                    access_token,
                    key,
                    gen,
                    now,
                    int(user_id),
                ],
            ).close()
        else:
            self.db.execute(
                "INSERT INTO zerodha_sessions "
                "(user_id, kite_user_id, access_token, api_key, api_secret, "
                " generated_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                [
                    int(user_id),
                    kite_user_id or None,
                    access_token,
                    api_key,
                    gen,
                    now,
                ],
            ).close()

    # Back-compat name used by older call sites.
    def upsert(
        self,
        user_id: int,
        *,
        access_token: str,
        kite_user_id: str = "",
        api_key: Optional[str] = None,
        generated_at: Optional[datetime] = None,
    ) -> None:
        self.upsert_token(
            user_id,
            access_token=access_token,
            kite_user_id=kite_user_id,
            api_key=api_key,
            generated_at=generated_at,
        )

    def clear_token(self, user_id: int) -> bool:
        """Clear access token only — keep this user's Kite app key/secret."""
        existing = self.get(user_id)
        if not existing:
            return False
        if not existing.get("access_token") and not existing.get("kite_user_id"):
            return False
        now = now_ist().replace(tzinfo=None)
        self.db.execute(
            "UPDATE zerodha_sessions SET access_token = NULL, kite_user_id = NULL, "
            "generated_at = NULL, updated_at = ? WHERE user_id = ?",
            [now, int(user_id)],
        ).close()
        return True

    def clear(self, user_id: int) -> bool:
        """Clear the daily token (credentials stay). Prefer ``clear_token``."""
        return self.clear_token(user_id)

    def public_kite_app(self, user_id: int) -> dict:
        """Safe summary only — never returns api_key or api_secret values."""
        row = self.get(user_id) or {}
        key = (row.get("api_key") or "").strip()
        secret = (row.get("api_secret") or "").strip()
        return {
            "configured": bool(key and secret),
            "has_token": bool(row.get("access_token")),
            "kite_user_id": row.get("kite_user_id") or None,
        }
