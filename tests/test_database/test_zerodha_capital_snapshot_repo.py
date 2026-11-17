"""Tests for ZerodhaCapitalSnapshotRepo change detection."""

from unittest.mock import MagicMock

from database.models import ZerodhaCapitalSnapshotRepo


def _snap(**kwargs):
    base = {
        "available": True,
        "user_id": "AB1",
        "net": 100000.0,
        "usable_balance": 80000.0,
        "available_cash": 75000.0,
        "live_balance": 80000.0,
        "equity": {
            "opening_balance": 90000.0,
            "utilised_debits": 20000.0,
            "utilised_span": 10000.0,
            "utilised_exposure": 0.0,
            "utilised_option_premium": 5000.0,
        },
    }
    base.update(kwargs)
    return base


def test_insert_if_changed_writes_first_row():
    db = MagicMock()
    db.fetch_one.return_value = None
    repo = ZerodhaCapitalSnapshotRepo(db)
    assert repo.insert_if_changed(_snap(), trigger_reason="test") is True
    assert db.execute.called


def test_insert_if_changed_skips_unchanged():
    db = MagicMock()
    db.fetch_one.return_value = {
        "net": 100000.0,
        "usable_balance": 80000.0,
        "available_cash": 75000.0,
        "live_balance": 80000.0,
        "utilised_debits": 20000.0,
        "utilised_span": 10000.0,
        "utilised_option_premium": 5000.0,
    }
    repo = ZerodhaCapitalSnapshotRepo(db)
    assert repo.insert_if_changed(_snap(), trigger_reason="test") is False
    db.execute.assert_not_called()


def test_insert_if_changed_on_idle_move():
    db = MagicMock()
    db.fetch_one.return_value = {
        "net": 100000.0,
        "usable_balance": 80000.0,
        "available_cash": 75000.0,
        "live_balance": 80000.0,
        "utilised_debits": 20000.0,
        "utilised_span": 10000.0,
        "utilised_option_premium": 5000.0,
    }
    repo = ZerodhaCapitalSnapshotRepo(db)
    assert repo.insert_if_changed(
        _snap(usable_balance=70000.0, live_balance=70000.0),
        trigger_reason="withdraw",
    ) is True
    assert db.execute.called


def test_insert_if_changed_ignores_unavailable():
    db = MagicMock()
    repo = ZerodhaCapitalSnapshotRepo(db)
    assert repo.insert_if_changed({"available": False}, trigger_reason="x") is False
    db.execute.assert_not_called()
