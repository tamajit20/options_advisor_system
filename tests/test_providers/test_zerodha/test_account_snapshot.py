"""Tests for providers/zerodha/account_snapshot.py"""

from unittest.mock import MagicMock

import pytest

from providers.zerodha import account_snapshot as acct


@pytest.fixture(autouse=True)
def clear_cache():
    acct.invalidate_account_cache()
    yield
    acct.invalidate_account_cache()


def test_normalize_account_snapshot():
    profile = {
        "user_id": "AB1234",
        "user_name": "Test User",
        "email": "test@example.com",
        "broker": "ZERODHA",
        "user_type": "individual",
    }
    margins = {
        "equity": {
            "enabled": True,
            "net": 150000.5,
            "available": {"cash": 120000.0, "live_balance": 118500.25, "opening_balance": 120000.0},
            "utilised": {"debits": 1500.0, "span": 800.0, "exposure": 0, "option_premium": 700.0},
        },
        "commodity": {"enabled": False, "net": 0, "available": {}, "utilised": {}},
    }
    out = acct.normalize_account_snapshot(profile, margins)
    assert out["user_name"] == "Test User"
    assert out["usable_balance"] == 118500.25
    assert out["available_cash"] == 120000.0
    assert out["net"] == 150000.5
    assert out["equity"]["utilised_span"] == 800.0


def test_fetch_account_snapshot_no_session(mocker):
    mocker.patch("providers.zerodha.account_snapshot.load_session", return_value=None)
    out = acct.fetch_account_snapshot()
    assert out["available"] is False
    assert out["reason"] == "no_valid_session"


def test_fetch_account_snapshot_success(mocker):
    session = MagicMock(access_token="tok", user_id="AB1234")
    mocker.patch("providers.zerodha.account_snapshot.load_session", return_value=session)
    mocker.patch("providers.zerodha.account_snapshot.is_token_valid", return_value=True)
    mocker.patch.dict("config.ZERODHA_API_CONFIG", {"api_key": "k"}, clear=False)

    facade = MagicMock()
    facade.profile.return_value = {
        "user_id": "AB1234",
        "user_name": "Test User",
        "email": "u@example.com",
        "broker": "ZERODHA",
    }
    facade.margins.return_value = {
        "equity": {
            "net": 50000,
            "available": {"cash": 50000, "live_balance": 49500},
            "utilised": {"debits": 500},
        }
    }
    mocker.patch("providers.zerodha.account_snapshot._build_facade", return_value=facade)

    out = acct.fetch_account_snapshot(force_refresh=True)
    assert out["available"] is True
    assert out["user_name"] == "Test User"
    assert out["usable_balance"] == 49500.0
    assert out["cached"] is False

    cached = acct.fetch_account_snapshot(force_refresh=False)
    assert cached["cached"] is True
    assert facade.profile.call_count == 1


def test_fetch_account_snapshot_force_refresh_bypasses_cache(mocker):
    session = MagicMock(access_token="tok")
    mocker.patch("providers.zerodha.account_snapshot.load_session", return_value=session)
    mocker.patch("providers.zerodha.account_snapshot.is_token_valid", return_value=True)
    mocker.patch.dict("config.ZERODHA_API_CONFIG", {"api_key": "k"}, clear=False)

    facade = MagicMock()
    facade.profile.return_value = {"user_id": "X", "user_name": "A"}
    facade.margins.return_value = {
        "equity": {"net": 1, "available": {"cash": 1, "live_balance": 1}, "utilised": {}}
    }
    mocker.patch("providers.zerodha.account_snapshot._build_facade", return_value=facade)

    acct.fetch_account_snapshot(force_refresh=True)
    acct.fetch_account_snapshot(force_refresh=True)
    assert facade.profile.call_count == 2
