"""Tests for lifecycle.zerodha_capital_snapshot."""

from unittest.mock import MagicMock

from lifecycle import zerodha_capital_snapshot as zcs


def test_run_skips_outside_session(mocker):
    mocker.patch("lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=False)
    fetch = mocker.patch("providers.zerodha.account_snapshot.fetch_account_snapshot")
    assert zcs.run_zerodha_capital_snapshot(MagicMock()) == 0
    fetch.assert_not_called()


def test_run_snapshots_each_logged_in_user(mocker):
    mocker.patch("lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=True)
    mocker.patch(
        "database.zerodha_session_repo.ZerodhaSessionRepo.list_user_ids_with_token",
        return_value=[2, 5],
    )
    fetch = mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        side_effect=[
            {"available": True, "capital_persisted": True},
            {"available": True, "capital_persisted": False},
        ],
    )
    assert zcs.run_zerodha_capital_snapshot(MagicMock()) == 1
    assert fetch.call_count == 2
    assert fetch.call_args_list[0].kwargs["app_user_id"] == 2
    assert fetch.call_args_list[1].kwargs["app_user_id"] == 5


def test_run_falls_back_to_admin_when_no_tokens(mocker):
    mocker.patch("lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=True)
    mocker.patch(
        "database.zerodha_session_repo.ZerodhaSessionRepo.list_user_ids_with_token",
        return_value=[],
    )
    fetch = mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        return_value={"available": True, "capital_persisted": True},
    )
    assert zcs.run_zerodha_capital_snapshot(MagicMock()) == 1
    fetch.assert_called_once()
    assert fetch.call_args.kwargs.get("app_user_id") is None


def test_run_returns_zero_when_unchanged(mocker):
    mocker.patch("lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=True)
    mocker.patch(
        "database.zerodha_session_repo.ZerodhaSessionRepo.list_user_ids_with_token",
        return_value=[1],
    )
    mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        return_value={"available": True, "capital_persisted": False},
    )
    assert zcs.run_zerodha_capital_snapshot(MagicMock()) == 0
