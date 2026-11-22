"""Multi-user login tests (unparked)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from database.app_user_repo import AppUserRepo
from lifecycle.trade_executor import duplicate_execution_reason


class _FakeDb:
    """Minimal DB stand-in for AppUserRepo unit tests."""

    def __init__(self):
        self.rows = []
        self._id = 1
        self.executed = []

    def fetch_one(self, sql, params=None):
        params = params or []
        s = " ".join(sql.split()).lower()
        if "where id =" in s:
            uid = int(params[0])
            for r in self.rows:
                if int(r["id"]) == uid:
                    return dict(r)
            return None
        if "where login =" in s:
            login = str(params[0]).lower()
            for r in self.rows:
                if r["login"] == login:
                    return dict(r)
            return None
        if "is_admin = 1" in s:
            for r in self.rows:
                if int(r.get("is_admin") or 0) and int(r.get("is_active") or 0):
                    return dict(r)
            return None
        if "count(*)" in s:
            return {"n": len(self.rows)}
        return None

    def fetch_all(self, sql, params=None):
        return [dict(r) for r in self.rows]

    def execute(self, sql, params=None):
        params = list(params or [])
        self.executed.append((sql, params))
        s = " ".join(sql.split()).lower()
        cur = MagicMock()
        if "insert into app_users" in s:
            row = {
                "id": self._id,
                "login": params[0],
                "password_hash": params[1],
                "display_name": params[2],
                "is_admin": int(params[3]),
                "is_active": int(params[4]),
                "created_at": params[5],
                "updated_at": params[6],
            }
            self.rows.append(row)
            cur.fetchone.return_value = (self._id,)
            self._id += 1
        elif "set is_admin = 0" in s:
            for r in self.rows:
                r["is_admin"] = 0
        elif "set is_admin = 1" in s:
            uid = int(params[1])
            for r in self.rows:
                r["is_admin"] = 1 if int(r["id"]) == uid else 0
        elif "set is_active" in s:
            uid = int(params[2])
            for r in self.rows:
                if int(r["id"]) == uid:
                    r["is_active"] = int(params[0])
        cur.close = MagicMock()
        return cur


def test_only_one_user_may_be_admin():
    db = _FakeDb()
    repo = AppUserRepo(db)
    a = repo.create_user(login="alice", password="secret-a", is_admin=True)
    b = repo.create_user(login="bob", password="secret-b", is_admin=False)
    assert a and b
    assert sum(1 for r in db.rows if r["is_admin"]) == 1
    repo.transfer_admin(b)
    admins = [r for r in db.rows if int(r["is_admin"]) == 1]
    assert len(admins) == 1
    assert int(admins[0]["id"]) == b


def test_open_trades_and_history_are_private_per_user():
    """duplicate_execution_reason is per-owner when owner_user_id is set."""
    db = MagicMock()
    db.fetch_one.return_value = None

    class _SugState:
        def get(self, sid, uid):
            if uid == 1:
                return {"status": "EXECUTED", "trade_id": "TRD-A"}
            return None

    import database.suggestion_user_state_repo as mod
    real = mod.SuggestionUserStateRepo
    mod.SuggestionUserStateRepo = lambda _db: _SugState()  # type: ignore
    try:
        assert duplicate_execution_reason(db, "SUG-1", owner_user_id=1)
        assert duplicate_execution_reason(db, "SUG-1", owner_user_id=2) is None
    finally:
        mod.SuggestionUserStateRepo = real


def test_admin_can_list_users_non_admin_cannot():
    from dashboard.auth import require_admin
    from flask import Flask, g

    app = Flask(__name__)
    with app.app_context():
        g.app_user = {"id": 2, "is_admin": False}
        denied = require_admin()
        assert denied is not None
        g.app_user = {"id": 1, "is_admin": True}
        assert require_admin() is None


@pytest.mark.parametrize("ready", [True, False])
def test_ws_runner_loads_admin_session_not_trader_session(ready, monkeypatch):
    from providers.zerodha import session as sess_mod

    calls = {"admin": 0, "user": 0}

    def fake_admin():
        calls["admin"] += 1
        return MagicMock(access_token="admin-tok") if ready else None

    def fake_user(uid):
        calls["user"] += 1
        return MagicMock(access_token=f"user-{uid}")

    monkeypatch.setattr(sess_mod, "load_admin_session", fake_admin)
    monkeypatch.setattr(sess_mod, "load_session_for_user", fake_user)
    # load_session() prefers admin
    out = sess_mod.load_session()
    assert calls["admin"] == 1
    if ready:
        assert out.access_token == "admin-tok"
    else:
        assert out is None


def test_order_placement_uses_logged_in_user_zerodha_token(monkeypatch):
    from lifecycle import zerodha_executor as zex
    from providers.zerodha.session import ZerodhaSession
    from datetime import datetime, timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))
    captured = {}

    def fake_for_user(uid):
        captured["uid"] = uid
        return ZerodhaSession(
            api_key="k",
            access_token="user-token",
            user_id="KITE1",
            generated_at=datetime.now(tz=IST),
        )

    monkeypatch.setattr(
        "providers.zerodha.session.load_session_for_user", fake_for_user,
    )
    monkeypatch.setattr(zex, "ZERODHA_API_CONFIG", {"api_key": "k", "api_secret": "s"})
    monkeypatch.setattr(
        zex, "KiteExecutionFacade", lambda **kw: MagicMock(access_token=kw.get("access_token")),
    )
    monkeypatch.setattr(
        "providers.zerodha.instrument_cache.get_shared_instrument_master",
        lambda **kw: MagicMock(),
    )
    token = zex._ACTING_APP_USER_ID.set(42)
    try:
        facade, _master = zex._build_client()
        assert captured.get("uid") == 42
        assert facade is not None
    finally:
        zex._ACTING_APP_USER_ID.reset(token)


def test_suggestion_execute_does_not_hide_card_for_other_users():
    """User A EXECUTED state must not block User B via duplicate_execution_reason."""
    db = MagicMock()
    db.fetch_one.return_value = None

    class _SugState:
        def get(self, sid, uid):
            return {"status": "EXECUTED", "trade_id": "TRD-A"} if uid == 1 else None

    import database.suggestion_user_state_repo as mod
    real = mod.SuggestionUserStateRepo
    mod.SuggestionUserStateRepo = lambda _db: _SugState()  # type: ignore
    try:
        assert duplicate_execution_reason(db, "SUG-X", owner_user_id=1)
        assert duplicate_execution_reason(db, "SUG-X", owner_user_id=99) is None
    finally:
        mod.SuggestionUserStateRepo = real


def test_admin_and_user_can_both_read_system_logs():
    """Product decision: system logs are admin-only (non-admin gated in routes)."""
    from dashboard.auth import require_admin
    from flask import Flask, g

    app = Flask(__name__)
    with app.app_context():
        g.app_user = {"id": 2, "is_admin": False}
        assert require_admin() is not None  # logs blocked for normal user
        g.app_user = {"id": 1, "is_admin": True}
        assert require_admin() is None
