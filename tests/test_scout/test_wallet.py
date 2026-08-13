"""Tests for Scout wallet gating."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scout.wallet import (
    cap_quantity_for_wallet,
    entry_wallet_block_reason,
    max_deployable_inr,
    wallet_summary,
)


def test_max_deployable_pct_and_reserve():
    settings = {"wallet_utilization_pct": 90, "wallet_reserve_inr": 2000}
    assert max_deployable_inr(20_000, settings) == 18_000
    assert max_deployable_inr(5_000, settings) == 3_000


def test_max_deployable_reserve_only_when_tighter():
    settings = {"wallet_utilization_pct": 100, "wallet_reserve_inr": 2000}
    assert max_deployable_inr(20_000, settings) == 18_000


def test_cap_quantity_for_wallet():
    assert cap_quantity_for_wallet(entry_price=100, quantity=200, free_inr=18000) == 180
    assert cap_quantity_for_wallet(entry_price=100, quantity=50, free_inr=18000) == 50
    assert cap_quantity_for_wallet(entry_price=100, quantity=50, free_inr=500) == 5
    assert cap_quantity_for_wallet(entry_price=100, quantity=50, free_inr=50) == 0


def test_wallet_summary_paper_mode():
    fake_db = MagicMock()
    with patch("scout.wallet.ScoutTradeRepo") as repo_cls:
        repo_cls.return_value.deployed_capital_inr.return_value = 0
        out = wallet_summary(fake_db, {"zerodha_execute_orders": False})
    assert out["error"] == "paper_mode"
    assert out["live"] is False


def test_entry_wallet_block_when_insufficient():
    fake_db = MagicMock()
    settings = {"zerodha_execute_orders": True, "wallet_utilization_pct": 90, "wallet_reserve_inr": 0}
    with patch("scout.wallet.wallet_summary") as ws:
        ws.return_value = {
            "live": True,
            "balance_inr": 20000,
            "max_deployable_inr": 18000,
            "deployed_inr": 18000,
            "free_inr": 0,
            "error": None,
        }
        reason = entry_wallet_block_reason(
            fake_db, entry_price=100, quantity=10, settings=settings,
        )
    assert reason is not None
    assert "insufficient" in reason.lower()


def test_entry_wallet_allows_when_free():
    fake_db = MagicMock()
    settings = {"zerodha_execute_orders": True}
    with patch("scout.wallet.wallet_summary") as ws:
        ws.return_value = {
            "live": True,
            "free_inr": 5000,
            "deployed_inr": 13000,
            "max_deployable_inr": 18000,
            "balance_inr": 20000,
            "error": None,
        }
        assert entry_wallet_block_reason(
            fake_db, entry_price=100, quantity=10, settings=settings,
        ) is None
