"""App users (dashboard login) — exactly one active admin via is_admin."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from database.connection import SQLServerConnection
from utils import now_ist


class AppUserRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def count(self) -> int:
        row = self.db.fetch_one("SELECT COUNT(*) AS n FROM app_users")
        return int((row or {}).get("n") or 0)

    def get(self, user_id: int) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT id, login, display_name, is_admin, is_active, created_at, updated_at "
            "FROM app_users WHERE id = ?",
            [int(user_id)],
        )

    def get_by_login(self, login: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM app_users WHERE login = ?",
            [str(login or "").strip().lower()],
        )

    def get_admin(self) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT id, login, display_name, is_admin, is_active, created_at, updated_at "
            "FROM app_users WHERE is_admin = 1 AND is_active = 1"
        )

    def list_users(self) -> List[dict]:
        return self.db.fetch_all(
            "SELECT id, login, display_name, is_admin, is_active, created_at, updated_at "
            "FROM app_users ORDER BY is_admin DESC, login ASC"
        ) or []

    def create_user(
        self,
        *,
        login: str,
        password: str,
        display_name: Optional[str] = None,
        is_admin: bool = False,
        is_active: bool = True,
    ) -> int:
        login_n = str(login or "").strip().lower()
        if not login_n or not password:
            raise ValueError("login and password are required")
        if self.get_by_login(login_n):
            raise ValueError(f"login already exists: {login_n}")
        if is_admin:
            self._clear_admin_flags()
        cur = self.db.execute(
            """
            INSERT INTO app_users
              (login, password_hash, display_name, is_admin, is_active, created_at, updated_at)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                login_n,
                generate_password_hash(password),
                (display_name or login_n)[:120],
                1 if is_admin else 0,
                1 if is_active else 0,
                now_ist().replace(tzinfo=None),
                now_ist().replace(tzinfo=None),
            ],
        )
        out = cur.fetchone()
        cur.close()
        return int(out[0])

    def authenticate(self, login: str, password: str) -> Optional[dict]:
        row = self.get_by_login(login)
        if not row or not int(row.get("is_active") or 0):
            return None
        if not check_password_hash(str(row.get("password_hash") or ""), password or ""):
            return None
        return self.get(int(row["id"]))

    def set_active(self, user_id: int, active: bool) -> None:
        u = self.get(user_id)
        if u is None:
            raise ValueError("unknown user")
        if int(u.get("is_admin") or 0) and not active:
            raise ValueError("cannot disable the current admin — transfer admin first")
        self.db.execute(
            "UPDATE app_users SET is_active = ?, updated_at = ? WHERE id = ?",
            [1 if active else 0, now_ist().replace(tzinfo=None), int(user_id)],
        ).close()

    def set_password(self, user_id: int, password: str) -> None:
        if not password:
            raise ValueError("password required")
        self.db.execute(
            "UPDATE app_users SET password_hash = ?, updated_at = ? WHERE id = ?",
            [
                generate_password_hash(password),
                now_ist().replace(tzinfo=None),
                int(user_id),
            ],
        ).close()

    def transfer_admin(self, to_user_id: int) -> None:
        """Make *to_user_id* the sole admin; demote any previous admin."""
        target = self.get(to_user_id)
        if target is None:
            raise ValueError("unknown user")
        if not int(target.get("is_active") or 0):
            raise ValueError("cannot promote an inactive user")
        self._clear_admin_flags()
        self.db.execute(
            "UPDATE app_users SET is_admin = 1, updated_at = ? WHERE id = ?",
            [now_ist().replace(tzinfo=None), int(to_user_id)],
        ).close()

    def _clear_admin_flags(self) -> None:
        self.db.execute(
            "UPDATE app_users SET is_admin = 0, updated_at = ? WHERE is_admin = 1",
            [now_ist().replace(tzinfo=None)],
        ).close()

    @staticmethod
    def public_row(row: Optional[dict]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "login": row.get("login"),
            "display_name": row.get("display_name"),
            "is_admin": bool(int(row.get("is_admin") or 0)),
            "is_active": bool(int(row.get("is_active") or 0)),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
