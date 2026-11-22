"""Per-user Kite Connect app key/secret resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_resolve_uses_per_user_db_credentials(monkeypatch):
    from providers.zerodha import session as sess

    class FakeRepo:
        def get(self, uid):
            assert uid == 2
            return {"api_key": "trader-key", "api_secret": "trader-secret"}

    class FakeUsers:
        def get_admin(self):
            return {"id": 1}

    class FakeDB:
        def connect(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        "database.connection.SQLServerConnection", lambda: FakeDB(),
    )
    monkeypatch.setattr(
        "database.zerodha_session_repo.ZerodhaSessionRepo",
        lambda db: FakeRepo(),
    )
    monkeypatch.setattr(
        "database.app_user_repo.AppUserRepo",
        lambda db: FakeUsers(),
    )
    key, secret = sess.resolve_kite_app_credentials(2)
    assert key == "trader-key"
    assert secret == "trader-secret"


def test_resolve_refuses_env_fallback_for_non_admin(monkeypatch):
    from providers.zerodha import session as sess

    class FakeRepo:
        def get(self, uid):
            return {}

    class FakeUsers:
        def get_admin(self):
            return {"id": 1}

    class FakeDB:
        def connect(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        "database.connection.SQLServerConnection", lambda: FakeDB(),
    )
    monkeypatch.setattr(
        "database.zerodha_session_repo.ZerodhaSessionRepo",
        lambda db: FakeRepo(),
    )
    monkeypatch.setattr(
        "database.app_user_repo.AppUserRepo",
        lambda db: FakeUsers(),
    )
    monkeypatch.setattr(
        sess, "ZERODHA_API_CONFIG", {"api_key": "env-key", "api_secret": "env-sec"},
        raising=False,
    )
    monkeypatch.setitem(
        __import__("config", fromlist=["ZERODHA_API_CONFIG"]).ZERODHA_API_CONFIG,
        "api_key",
        "env-key",
    )
    monkeypatch.setitem(
        __import__("config", fromlist=["ZERODHA_API_CONFIG"]).ZERODHA_API_CONFIG,
        "api_secret",
        "env-sec",
    )
    with pytest.raises(RuntimeError, match="Users tab"):
        sess.resolve_kite_app_credentials(2)


def test_resolve_env_fallback_for_admin(monkeypatch):
    from providers.zerodha import session as sess
    import config

    class FakeRepo:
        def get(self, uid):
            return {}

    class FakeUsers:
        def get_admin(self):
            return {"id": 1}

    class FakeDB:
        def connect(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        "database.connection.SQLServerConnection", lambda: FakeDB(),
    )
    monkeypatch.setattr(
        "database.zerodha_session_repo.ZerodhaSessionRepo",
        lambda db: FakeRepo(),
    )
    monkeypatch.setattr(
        "database.app_user_repo.AppUserRepo",
        lambda db: FakeUsers(),
    )
    monkeypatch.setitem(config.ZERODHA_API_CONFIG, "api_key", "admin-env-key")
    monkeypatch.setitem(config.ZERODHA_API_CONFIG, "api_secret", "admin-env-sec")
    key, secret = sess.resolve_kite_app_credentials(1)
    assert key == "admin-env-key"
    assert secret == "admin-env-sec"


def test_build_client_uses_session_api_key(monkeypatch):
    from lifecycle import zerodha_executor as zex
    from providers.zerodha.session import ZerodhaSession
    from datetime import datetime, timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))
    captured = {}

    def fake_for_user(uid):
        return ZerodhaSession(
            api_key="user-app-key",
            access_token="tok",
            user_id="KITE2",
            generated_at=datetime.now(tz=IST),
        )

    monkeypatch.setattr(
        "providers.zerodha.session.load_session_for_user", fake_for_user,
    )
    monkeypatch.setattr(
        zex, "KiteExecutionFacade",
        lambda **kw: captured.update(kw) or MagicMock(),
    )
    monkeypatch.setattr(
        "providers.zerodha.instrument_cache.get_shared_instrument_master",
        lambda **kw: MagicMock(),
    )
    token = zex._ACTING_APP_USER_ID.set(7)
    try:
        zex._build_client()
        assert captured.get("api_key") == "user-app-key"
        assert captured.get("access_token") == "tok"
    finally:
        zex._ACTING_APP_USER_ID.reset(token)


def test_session_repo_clear_token_keeps_credentials():
    """Unit-level logic via a tiny fake DB."""
    from database.zerodha_session_repo import ZerodhaSessionRepo

    store = {
        2: {
            "user_id": 2,
            "api_key": "k",
            "api_secret": "s",
            "access_token": "tok",
            "kite_user_id": "AB",
            "generated_at": "2026-01-01",
        }
    }

    class FakeDB:
        def fetch_one(self, sql, params):
            return dict(store.get(int(params[0])) or {})

        def execute(self, sql, params):
            uid = int(params[-1])
            row = store[uid]
            if "access_token = NULL" in sql.replace("\n", " "):
                row["access_token"] = None
                row["kite_user_id"] = None
                row["generated_at"] = None
            return SimpleNamespace(close=lambda: None, rowcount=1)

    repo = ZerodhaSessionRepo(FakeDB())
    assert repo.clear_token(2) is True
    assert store[2]["api_key"] == "k"
    assert store[2]["api_secret"] == "s"
    assert store[2]["access_token"] is None


def test_public_kite_app_never_returns_secrets():
    from database.zerodha_session_repo import ZerodhaSessionRepo

    class FakeDB:
        def fetch_one(self, sql, params):
            return {
                "user_id": 3,
                "api_key": "abcd1234secretkey",
                "api_secret": "super-secret-value",
                "access_token": "tok",
                "kite_user_id": "XY",
            }

    pub = ZerodhaSessionRepo(FakeDB()).public_kite_app(3)
    assert pub["configured"] is True
    assert pub["has_token"] is True
    assert pub["kite_user_id"] == "XY"
    blob = str(pub)
    assert "abcd1234secretkey" not in blob
    assert "super-secret-value" not in blob
    assert "api_key" not in pub
    assert "api_secret" not in pub
    assert "api_key_masked" not in pub
