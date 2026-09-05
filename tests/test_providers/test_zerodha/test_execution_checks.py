"""Tests for providers/zerodha/execution_checks.py"""

from unittest.mock import MagicMock

import pytest

from providers.zerodha.execution_checks import (
    build_order_margin_params,
    check_exposure_conflicts,
)
from providers.zerodha.instruments import Instrument
from datetime import date


def _inst(sym: str) -> Instrument:
    return Instrument(
        instrument_token=1,
        exchange_token=1,
        tradingsymbol=sym,
        name="NIFTY",
        expiry=date(2026, 5, 28),
        strike=23000.0,
        tick_size=0.05,
        lot_size=50,
        instrument_type="CE",
        segment="NFO-OPT",
        exchange="NFO",
    )


def test_exposure_blocks_duplicate_long_entry():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": 50}],
    }
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = check_exposure_conflicts(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert not out.ok


def test_exposure_allows_buy_to_cover_short():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": -50}],
    }
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = check_exposure_conflicts(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert out.ok


def test_exposure_skipped_for_supplement():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": 50}],
    }
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = check_exposure_conflicts(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
        allow_existing_positions=True,
    )
    assert out.ok
    facade.positions.assert_not_called()


def test_margin_params_reject_zero_qty():
    from dataclasses import replace
    inst = replace(_inst("NIFTY26MAY23000CE"), lot_size=0)
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 0}]
    with pytest.raises(ValueError, match="quantity"):
        build_order_margin_params(
            legs, {1: inst},
            transaction_fn=lambda leg: "BUY",
            limit_fn=lambda lo, inst, txn: 100.0,
            product="NRML",
            variety="regular",
        )
