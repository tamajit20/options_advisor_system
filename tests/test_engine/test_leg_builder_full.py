"""Additional coverage for engine/leg_builder.py — breakevens, max_profit_loss, pop."""
from __future__ import annotations

from datetime import date

import pytest

from contracts import SuggestionLeg
from engine import leg_builder as lb


_LEG_COUNTER = {"n": 0}


def _leg(action, opt, strike, price=10.0, lots=1, lot_size=75):
    _LEG_COUNTER["n"] += 1
    return SuggestionLeg(
        leg_order=_LEG_COUNTER["n"],
        hedge_pair_leg=None,
        symbol="NIFTY",
        expiry_date=date(2026, 5, 14),
        strike=strike, option_type=opt, action=action,
        lots=lots, lot_size=lot_size,
        suggested_price=price,
        suggested_price_low=price * 0.95,
        suggested_price_high=price * 1.05,
        leg_purpose_note="test",
    )


# ---------------------------------------------------------------------------
class TestBreakevens:
    def test_iron_condor(self):
        legs = [
            _leg("SELL", "CE", 23200, 50),
            _leg("BUY",  "CE", 23300, 25),
            _leg("SELL", "PE", 22800, 50),
            _leg("BUY",  "PE", 22700, 25),
        ]
        u, l = lb.breakevens(legs, "IRON_CONDOR")
        # net premium = 50 + 50 - 25 - 25 = 50
        assert u == 23250
        assert l == 22750

    def test_bull_put_spread(self):
        legs = [_leg("SELL", "PE", 22800, 50), _leg("BUY", "PE", 22700, 20)]
        u, l = lb.breakevens(legs, "BULL_PUT_SPREAD")
        assert u is None
        assert l == 22770  # 22800 - 30

    def test_bear_call_spread(self):
        legs = [_leg("SELL", "CE", 23200, 50), _leg("BUY", "CE", 23300, 20)]
        u, l = lb.breakevens(legs, "BEAR_CALL_SPREAD")
        assert l is None
        assert u == 23230  # 23200 + 30

    def test_long_straddle(self):
        legs = [_leg("BUY", "CE", 23000, 50), _leg("BUY", "PE", 23000, 50)]
        u, l = lb.breakevens(legs, "LONG_STRADDLE")
        # debit = 100 → BE = 23000 ± 100
        assert u == 23100
        assert l == 22900

    def test_long_strangle(self):
        legs = [_leg("BUY", "CE", 23200, 30), _leg("BUY", "PE", 22800, 30)]
        u, l = lb.breakevens(legs, "LONG_STRANGLE")
        # debit = 60
        assert u == 23260
        assert l == 22740

    def test_long_call(self):
        legs = [_leg("BUY", "CE", 23200, 50)]
        u, l = lb.breakevens(legs, "LONG_CALL")
        assert l is None
        assert u == 23250

    def test_long_put(self):
        legs = [_leg("BUY", "PE", 22800, 50)]
        u, l = lb.breakevens(legs, "LONG_PUT")
        assert u is None
        assert l == 22750

    def test_bull_call_spread(self):
        legs = [_leg("BUY", "CE", 23000, 80), _leg("SELL", "CE", 23200, 30)]
        u, l = lb.breakevens(legs, "BULL_CALL_SPREAD")
        # debit = 50
        assert l is None
        assert u == 23050

    def test_bear_put_spread(self):
        legs = [_leg("BUY", "PE", 23000, 80), _leg("SELL", "PE", 22800, 30)]
        u, l = lb.breakevens(legs, "BEAR_PUT_SPREAD")
        assert u is None
        assert l == 22950  # 23000 - 50

    def test_iron_butterfly(self):
        legs = [
            _leg("SELL", "CE", 23000, 80),
            _leg("BUY",  "CE", 23200, 20),
            _leg("SELL", "PE", 23000, 80),
            _leg("BUY",  "PE", 22800, 20),
        ]
        u, l = lb.breakevens(legs, "IRON_BUTTERFLY")
        # np = 80 + 80 - 20 - 20 = 120 → 23000 ± 120
        assert u == 23120
        assert l == 22880

    def test_jade_lizard_with_credit_above_call_width(self):
        legs = [
            _leg("SELL", "PE", 22800, 60),
            _leg("SELL", "CE", 23200, 40),
            _leg("BUY",  "CE", 23250, 10),
        ]
        # net premium = 60 + 40 - 10 = 90, call_width = 50 → upper BE = None
        u, l = lb.breakevens(legs, "JADE_LIZARD")
        assert u is None
        assert l == 22710

    def test_returns_none_for_unknown(self):
        u, l = lb.breakevens([], "UNKNOWN")
        assert u is None and l is None


# ---------------------------------------------------------------------------
class TestMaxProfitLoss:
    def test_credit_strategy_iron_condor(self):
        legs = [
            _leg("SELL", "CE", 23200, 50),
            _leg("BUY",  "CE", 23300, 25),
            _leg("SELL", "PE", 22800, 50),
            _leg("BUY",  "PE", 22700, 25),
        ]
        mp, ml = lb.max_profit_loss(legs, "IRON_CONDOR")
        # max_profit = 50, max_loss = 100 - 50 = 50
        assert mp == 50
        assert ml == 50

    def test_debit_spread(self):
        legs = [_leg("BUY", "CE", 23000, 80), _leg("SELL", "CE", 23200, 30)]
        mp, ml = lb.max_profit_loss(legs, "BULL_CALL_SPREAD")
        # debit = 50, width = 200, max_profit = 150, max_loss = 50
        assert mp == 150
        assert ml == 50

    def test_long_premium_unbounded_profit(self):
        legs = [_leg("BUY", "CE", 23000, 80)]
        mp, ml = lb.max_profit_loss(legs, "LONG_CALL")
        assert mp == float("inf")
        assert ml == 80

    def test_jade_lizard(self):
        legs = [
            _leg("SELL", "PE", 22800, 60),
            _leg("SELL", "CE", 23200, 40),
            _leg("BUY",  "CE", 23250, 10),
        ]
        mp, ml = lb.max_profit_loss(legs, "JADE_LIZARD")
        # net premium = 90, downside_loss = 22800-90 = 22710 capped to positive
        assert mp == 90
        assert ml > 0

    def test_unknown_returns_zero(self):
        mp, ml = lb.max_profit_loss([], "UNKNOWN")
        assert mp == 0.0 and ml == 0.0


# ---------------------------------------------------------------------------
class TestEstimatePop:
    def test_no_short_no_long_returns_50(self):
        assert lb.estimate_pop([], 23000.0, 14, 0.18) == 50.0

    def test_short_legs_pop_in_range(self):
        legs = [_leg("SELL", "CE", 23200, 50), _leg("SELL", "PE", 22800, 50)]
        pop = lb.estimate_pop(legs, 23000.0, 14, 0.18)
        assert 0.0 <= pop <= 100.0

    def test_long_only_uses_long_delta(self):
        legs = [_leg("BUY", "CE", 23200, 50)]
        pop = lb.estimate_pop(legs, 23000.0, 14, 0.18)
        assert 0.0 <= pop <= 100.0
