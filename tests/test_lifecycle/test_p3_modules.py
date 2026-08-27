"""P3 tests — tick routing edge cases, index backfill, trade greeks."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from providers.tick_routing import (
    PRODUCT_OPTIONS_INDEX,
    resolve_product,
    topic_for_meta,
)
from providers.zerodha.ws_runner import TokenMeta


def test_resolve_product_finnifty_index():
    meta = TokenMeta(symbol="FINNIFTY", is_index=True)
    assert resolve_product(meta) == PRODUCT_OPTIONS_INDEX


def test_resolve_product_unknown_equity_defaults_index():
    meta = TokenMeta(symbol="BPCL")
    assert resolve_product(meta) == PRODUCT_OPTIONS_INDEX
    assert topic_for_meta(meta) == "tick.index"


def test_resolve_product_infers_index_from_symbol_name():
    meta = TokenMeta(symbol="NIFTY")
    assert resolve_product(meta) == PRODUCT_OPTIONS_INDEX


def test_index_spot_backfill_no_symbols_returns_zero(mocker):
    from lifecycle.index_spot_backfill import run_index_spot_backfill

    mocker.patch("lifecycle.index_spot_backfill._configured_indices", return_value=[])
    db = MagicMock()
    assert run_index_spot_backfill(db, use_zerodha=False, use_nse=False) == 0


def test_index_spot_backfill_zerodha_path(mocker):
    from lifecycle.index_spot_backfill import run_index_spot_backfill

    mocker.patch("lifecycle.index_spot_backfill._configured_indices", return_value=["NIFTY"])
    mock_repo = MagicMock()
    mock_repo.upsert_many.return_value = 5
    mocker.patch("lifecycle.index_spot_backfill.SpotEodRepo", return_value=mock_repo)
    mocker.patch(
        "lifecycle.index_spot_backfill.backfill_underlyings",
        return_value={"NIFTY": [MagicMock()]},
    )
    db = MagicMock()
    total = run_index_spot_backfill(db, days=5, end_date=date(2026, 8, 12), use_nse=False)
    assert total == 5
    db.commit.assert_called()


def test_trade_greeks_update_no_open_trades(mocker):
    from lifecycle.trade_greeks_job import run_trade_greeks_update

    mock_trade_repo = MagicMock()
    mock_trade_repo.open_trades.return_value = []
    mocker.patch("lifecycle.trade_greeks_job.TradeRepo", return_value=mock_trade_repo)
    db = MagicMock()
    assert run_trade_greeks_update(db) == 0
