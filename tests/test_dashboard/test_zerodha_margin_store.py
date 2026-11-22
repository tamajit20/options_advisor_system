"""Zerodha Final/Peak is stored once; funds checks are always per-user."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import dashboard.server as server


@pytest.fixture
def app(mocker, mock_dashboard_admin):
    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = None
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    # Preview tests assert owner_user_id == 7 (not the default admin id).
    mocker.patch(
        "dashboard.auth._load_user_from_cookie",
        return_value={
            "id": 7,
            "login": "trader7",
            "display_name": "Trader",
            "is_admin": False,
            "is_active": True,
        },
    )
    app = server.create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_margin_preview_from_row_requires_positive_final():
    assert server._zerodha_margin_preview_from_row(None) is None
    assert server._zerodha_margin_preview_from_row({"zerodha_margin_final": 0}) is None
    out = server._zerodha_margin_preview_from_row({
        "zerodha_margin_final": 125000.5,
        "zerodha_margin_peak": 140000,
        "zerodha_margin_lots": 2,
    })
    assert out["margin_final_required"] == 125000.5
    assert out["margin_peak_required"] == 140000
    assert out["from_db"] is True
    # Stored Final alone must not claim funds OK.
    assert out["margin_ok"] is None


def test_stored_margin_preview_scoped_to_owner(mocker):
    db = MagicMock()
    db.fetch_one.return_value = None
    mocker.patch(
        "dashboard.server.TradeRepo.get",
        return_value={
            "trade_id": "T1",
            "owner_user_id": 99,
            "zerodha_margin_final": 50000,
            "zerodha_margin_peak": 55000,
        },
    )
    # Other user's trade id must not leak margin to owner 7.
    assert server._stored_zerodha_margin_preview(
        db, "SUG-1", trade_id="T1", owner_user_id=7,
    ) is None
    # Same owner: trade margin is returned.
    mocker.patch(
        "dashboard.server.TradeRepo.get",
        return_value={
            "trade_id": "T1",
            "owner_user_id": 7,
            "zerodha_margin_final": 50000,
            "zerodha_margin_peak": 55000,
        },
    )
    out = server._stored_zerodha_margin_preview(
        db, "SUG-1", trade_id="T1", owner_user_id=7,
    )
    assert out is not None
    assert out["margin_final_required"] == 50000
    assert out["margin_ok"] is None


def test_apply_user_funds_gate_blocks_when_short(mocker):
    mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        return_value={"available": True, "net": 50000.0, "usable_balance": 50000.0},
    )
    mocker.patch.dict(
        "config.ZERODHA_EXECUTION_CONFIG",
        {"margin_buffer_pct": 5},
        clear=False,
    )
    preview = {
        "margin_peak_required": 100000.0,
        "margin_final_required": 90000.0,
        "margin_required": 100000.0,
        "from_db": True,
    }
    out = server._apply_user_funds_gate(preview, app_user_id=7)
    assert out["margin_ok"] is False
    assert out["margin_available"] == 50000.0
    assert out["margin_shortfall"] > 0


def test_apply_user_funds_gate_ok_when_covers(mocker):
    mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        return_value={"available": True, "net": 200000.0},
    )
    mocker.patch.dict(
        "config.ZERODHA_EXECUTION_CONFIG",
        {"margin_buffer_pct": 5},
        clear=False,
    )
    preview = {
        "margin_peak_required": 100000.0,
        "margin_final_required": 90000.0,
        "from_db": True,
    }
    out = server._apply_user_funds_gate(preview, app_user_id=7)
    assert out["margin_ok"] is True
    assert out["margin_available"] == 200000.0
    assert out["margin_shortfall"] == 0.0


def test_preview_returns_stored_with_live_funds_gate(client, mocker):
    mocker.patch("dashboard.server._zerodha_execution_auth_required", return_value=None)
    mocker.patch(
        "dashboard.server._stored_zerodha_margin_preview",
        return_value={
            "margin_final_required": 99000,
            "margin_peak_required": 110000,
            "margin_required": 110000,
            "margin_ok": None,
            "from_db": True,
            "legs": [],
        },
    )
    mocker.patch(
        "dashboard.server._instrument_overlaps_for_suggestion",
        return_value=[],
    )
    def _gate(p, uid):
        # Route mutates preview in place (ignores return).
        p["margin_ok"] = True
        p["margin_available"] = 250000.0
        return p

    gate = mocker.patch(
        "dashboard.server._apply_user_funds_gate",
        side_effect=_gate,
    )
    kite = mocker.patch("lifecycle.zerodha_executor.preview_suggestion_execution")
    resp = client.post("/api/suggestion/SUG-1/zerodha-preview", json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["from_db"] is True
    assert body["preview"]["margin_final_required"] == 99000
    assert body["preview"]["margin_ok"] is True
    assert body["preview"]["margin_available"] == 250000.0
    gate.assert_called_once()
    assert gate.call_args.args[1] == 7
    kite.assert_not_called()


def test_preview_persists_per_user_state_not_shared_suggestion(client, mocker):
    mocker.patch("dashboard.server._zerodha_execution_auth_required", return_value=None)
    mocker.patch("dashboard.server._stored_zerodha_margin_preview", return_value=None)
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_margin_quote_ready",
        return_value=True,
    )
    mocker.patch(
        "dashboard.server._instrument_overlaps_for_suggestion",
        return_value=[],
    )

    class _Prev:
        def to_dict(self):
            return {
                "margin_final_required": 88000,
                "margin_peak_required": 95000,
                "margin_required": 95000,
                "margin_ok": True,
                "margin_path_steps": [{"leg_order": 1, "final": 88000}],
                "legs": [],
            }

    mocker.patch(
        "lifecycle.zerodha_executor.preview_suggestion_execution",
        return_value=_Prev(),
    )
    persist = mocker.patch("dashboard.server._persist_zerodha_margin_quote")
    resp = client.post("/api/suggestion/SUG-2/zerodha-preview", json={"lots": 1})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["from_db"] is False
    assert body["preview"]["margin_final_required"] == 88000
    persist.assert_called_once()
    kwargs = persist.call_args.kwargs
    # Persist on per-user state (suggestion_id + owner), not shared suggestion row.
    assert kwargs["suggestion_id"] == "SUG-2"
    assert kwargs["owner_user_id"] == 7
    assert kwargs["margin_final"] == 88000
    assert kwargs["lots"] == 1


def test_persist_margin_quote_commits_user_state(mocker):
    """Regression: @_with_db closes without commit — must commit here."""
    db = mocker.Mock()
    ust = mocker.patch("database.suggestion_user_state_repo.SuggestionUserStateRepo")
    ust.return_value.save_zerodha_margin_once.return_value = True
    trd = mocker.patch("dashboard.server.TradeRepo")
    trd.return_value.save_zerodha_margin_once.return_value = False
    db.fetch_one.return_value = None
    server._persist_zerodha_margin_quote(
        db,
        suggestion_id="SUG-1",
        trade_id=None,
        margin_final=50000,
        margin_peak=60000,
        lots=1,
        owner_user_id=7,
    )
    ust.return_value.save_zerodha_margin_once.assert_called_once()
    db.commit.assert_called_once()


def test_stored_preview_reads_user_state_margin(mocker):
    db = MagicMock()
    db.fetch_one.return_value = None  # no trade
    mocker.patch(
        "database.suggestion_user_state_repo.SuggestionUserStateRepo.get",
        return_value={
            "zerodha_margin_final": 77000,
            "zerodha_margin_peak": 81000,
            "zerodha_margin_lots": 1,
        },
    )
    out = server._stored_zerodha_margin_preview(
        db, "SUG-9", owner_user_id=3, lots=1,
    )
    assert out is not None
    assert out["margin_final_required"] == 77000
    assert out["margin_ok"] is None
