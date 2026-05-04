"""
tests/test_providers/test_zerodha/test_session.py
=================================================

Tests for `providers.zerodha.session` — token persistence + validity rules.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from providers.zerodha.session import (
    SESSION_FILENAME,
    ZerodhaSession,
    clear_session,
    is_token_valid,
    load_session,
    save_session,
)


_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def session_path(tmp_path) -> Path:
    return tmp_path / SESSION_FILENAME


@pytest.fixture
def fresh_session() -> ZerodhaSession:
    return ZerodhaSession(
        api_key="key123",
        access_token="tok456",
        user_id="AB1234",
        generated_at=datetime(2026, 5, 4, 9, 0, tzinfo=_IST),
    )


# ---- round-trip ----------------------------------------------------------

def test_save_then_load(session_path, fresh_session):
    save_session(fresh_session, path=session_path)
    loaded = load_session(path=session_path)
    assert loaded is not None
    assert loaded.api_key == fresh_session.api_key
    assert loaded.access_token == fresh_session.access_token
    assert loaded.user_id == fresh_session.user_id
    assert loaded.generated_at == fresh_session.generated_at


def test_load_returns_none_when_missing(session_path):
    assert load_session(path=session_path) is None


def test_load_handles_corrupt_file(session_path):
    session_path.write_text("not json at all")
    assert load_session(path=session_path) is None


def test_load_handles_missing_keys(session_path):
    session_path.write_text(json.dumps({"api_key": "x"}))
    assert load_session(path=session_path) is None


def test_clear_removes_file(session_path, fresh_session):
    save_session(fresh_session, path=session_path)
    assert session_path.exists()
    assert clear_session(path=session_path) is True
    assert not session_path.exists()


def test_clear_when_missing_returns_false(session_path):
    assert clear_session(path=session_path) is False


def test_save_creates_parent_dir(tmp_path, fresh_session):
    nested = tmp_path / "deep" / "nest" / SESSION_FILENAME
    save_session(fresh_session, path=nested)
    assert nested.exists()


# ---- token validity ------------------------------------------------------

def test_token_valid_same_day_before_reset(fresh_session):
    """Token generated 09:00 IST is valid at 14:00 IST same day."""
    now = datetime(2026, 5, 4, 14, 0, tzinfo=_IST)
    assert is_token_valid(fresh_session, now=now) is True


def test_token_invalid_after_06_next_day(fresh_session):
    """Token generated 09:00 IST is invalid at 06:00 IST next day."""
    now = datetime(2026, 5, 5, 6, 0, tzinfo=_IST)
    assert is_token_valid(fresh_session, now=now) is False


def test_token_valid_just_before_06_next_day(fresh_session):
    now = datetime(2026, 5, 5, 5, 59, 59, tzinfo=_IST)
    assert is_token_valid(fresh_session, now=now) is True


def test_token_invalid_when_none():
    assert is_token_valid(None) is False


def test_token_invalid_when_blank():
    s = ZerodhaSession(
        api_key="k", access_token="", user_id="x",
        generated_at=datetime(2026, 5, 4, 9, 0, tzinfo=_IST),
    )
    assert is_token_valid(s) is False


def test_token_generated_pre_06_expires_same_day():
    """Edge case — if you somehow have a token generated at 04:00 IST, it
    expires at 06:00 IST the SAME day, not the next day."""
    s = ZerodhaSession(
        api_key="k", access_token="tok", user_id="x",
        generated_at=datetime(2026, 5, 4, 4, 0, tzinfo=_IST),
    )
    assert is_token_valid(s, now=datetime(2026, 5, 4, 5, 30, tzinfo=_IST)) is True
    assert is_token_valid(s, now=datetime(2026, 5, 4, 6, 0, tzinfo=_IST)) is False
