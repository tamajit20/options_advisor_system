"""Tests for lifecycle.zerodha_capital_snapshot."""

from unittest.mock import MagicMock

from lifecycle import zerodha_capital_snapshot as zcs


def test_run_skips_outside_session(mocker):
    mocker.patch("lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=False)
    fetch = mocker.patch("providers.zerodha.account_snapshot.fetch_account_snapshot")
    assert zcs.run_zerodha_capital_snapshot(MagicMock()) == 0
    fetch.assert_not_called()


def test_run_returns_one_when_persisted(mocker):
    mocker.patch("lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=True)
    mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        return_value={"available": True, "capital_persisted": True},
    )
    assert zcs.run_zerodha_capital_snapshot(MagicMock()) == 1


def test_run_returns_zero_when_unchanged(mocker):
    mocker.patch("lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=True)
    mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        return_value={"available": True, "capital_persisted": False},
    )
    assert zcs.run_zerodha_capital_snapshot(MagicMock()) == 0
