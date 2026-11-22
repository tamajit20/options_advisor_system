"""Zerodha Final/Peak is stored once and reused without re-calling Kite."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import dashboard.server as server


@pytest.fixture
def app(mocker):
    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = None
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
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


def test_preview_returns_stored_without_kite(client, mocker):
    mocker.patch("dashboard.server._zerodha_execution_auth_required", return_value=None)
    mocker.patch(
        "dashboard.server._stored_zerodha_margin_preview",
        return_value={
            "margin_final_required": 99000,
            "margin_peak_required": 110000,
            "margin_required": 110000,
            "from_db": True,
            "legs": [],
        },
    )
    mocker.patch(
        "dashboard.server._instrument_overlaps_for_suggestion",
        return_value=[],
    )
    kite = mocker.patch("lifecycle.zerodha_executor.preview_suggestion_execution")
    resp = client.post("/api/suggestion/SUG-1/zerodha-preview", json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["from_db"] is True
    assert body["preview"]["margin_final_required"] == 99000
    kite.assert_not_called()


def test_preview_persists_after_kite_quote(client, mocker):
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
    assert kwargs["suggestion_id"] == "SUG-2"
    assert kwargs["margin_final"] == 88000
    assert kwargs["lots"] == 1


def test_persist_margin_quote_commits(mocker):
    """Regression: @_with_db closes without commit — must commit here."""
    db = mocker.Mock()
    sug = mocker.patch("dashboard.server.SuggestionRepo")
    sug.return_value.save_zerodha_margin_once.return_value = True
    trd = mocker.patch("dashboard.server.TradeRepo")
    trd.return_value.latest_for_suggestion.return_value = {"trade_id": "TRD-1"}
    trd.return_value.save_zerodha_margin_once.return_value = True
    server._persist_zerodha_margin_quote(
        db,
        suggestion_id="SUG-1",
        trade_id=None,
        margin_final=50000,
        margin_peak=60000,
        lots=1,
    )
    db.commit.assert_called_once()