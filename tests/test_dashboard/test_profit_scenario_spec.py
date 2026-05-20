"""
Parity spec for dashboard buildProfitScenario() (dashboard/static/dashboard.js).

The trade-page profit zone and suggestion-page Ideal scenario must agree on
*direction* (above / below / between / outside / pin). This module encodes
that contract against engine/leg_builder.py breakevens() so regressions are
caught without a JS test runner.

BEAR_PUT_SPREAD regression (TRD-20260520-001): max profit when spot <= short
put strike — NOT "stays above short put" (that template is BULL_PUT_SPREAD).
"""

from __future__ import annotations

from datetime import date

import pytest

from contracts import SuggestionLeg
from engine import leg_builder


def _leg(order, action, strike, opt, price=100.0):
    return SuggestionLeg(
        leg_order=order,
        hedge_pair_leg=None,
        symbol="NIFTY",
        expiry_date=date(2026, 6, 1),
        strike=float(strike),
        option_type=opt,
        action=action,
        lots=1,
        lot_size=75,
        suggested_price=price,
        suggested_price_low=price * 0.95,
        suggested_price_high=price * 1.05,
        leg_purpose_note="",
    )


class TestProfitDirectionSpec:
    """Engine breakevens imply where max profit lives at expiry."""

    def test_bear_put_spread_profit_below_short_put(self):
        legs = [
            _leg(1, "BUY", 23650, "PE", 270),
            _leg(2, "SELL", 23300, "PE", 120),
        ]
        upper, lower = leg_builder.breakevens(legs, "BEAR_PUT_SPREAD")
        short_put = 23300.0
        assert upper is None
        assert lower is not None
        # Lower BE = long_put − debit (between short and long strikes).
        # Max profit at expiry when spot <= short put; profit starts below lower BE.
        assert lower > short_put
        mp, ml = leg_builder.max_profit_loss(legs, "BEAR_PUT_SPREAD")
        assert mp > 0 and ml > 0

    def test_bull_call_spread_profit_above_short_call(self):
        legs = [
            _leg(1, "BUY", 23000, "CE", 200),
            _leg(2, "SELL", 23200, "CE", 120),
        ]
        upper, lower = leg_builder.breakevens(legs, "BULL_CALL_SPREAD")
        short_call = 23200.0
        assert lower is None
        assert upper is not None
        assert upper < short_call  # BE below short call; max profit at/above short call

    def test_bull_put_spread_profit_above_short_put(self):
        legs = [
            _leg(1, "SELL", 23400, "PE", 150),
            _leg(2, "BUY", 23000, "PE", 80),
        ]
        upper, lower = leg_builder.breakevens(legs, "BULL_PUT_SPREAD")
        short_put = 23400.0
        assert upper is None
        assert lower is not None
        assert lower < short_put  # profit when spot >= short_put (above lower BE)

    def test_bear_call_spread_profit_below_short_call(self):
        legs = [
            _leg(1, "SELL", 23800, "CE", 150),
            _leg(2, "BUY", 24100, "CE", 80),
        ]
        upper, lower = leg_builder.breakevens(legs, "BEAR_CALL_SPREAD")
        short_call = 23800.0
        assert lower is None
        assert upper is not None
        assert upper > short_call  # profit when spot <= short_call

    def test_iron_condor_profit_between_short_strikes(self):
        legs = [
            _leg(1, "SELL", 23200, "PE", 80),
            _leg(2, "BUY", 22900, "PE", 40),
            _leg(3, "SELL", 24100, "CE", 80),
            _leg(4, "BUY", 24400, "CE", 40),
        ]
        np_ = leg_builder.net_premium(legs)
        upper, lower = leg_builder.breakevens(legs, "IRON_CONDOR")
        assert upper == pytest.approx(24100 + np_)
        assert lower == pytest.approx(23200 - np_)
        assert lower < 23200 < 24100 < upper

    def test_long_straddle_profit_outside_breakevens(self):
        strike = 23500.0
        legs = [
            _leg(1, "BUY", strike, "CE", 150),
            _leg(2, "BUY", strike, "PE", 140),
        ]
        upper, lower = leg_builder.breakevens(legs, "LONG_STRADDLE")
        debit = 290.0
        assert upper == pytest.approx(strike + debit)
        assert lower == pytest.approx(strike - debit)
        assert lower < strike < upper
