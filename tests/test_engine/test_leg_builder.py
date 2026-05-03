"""Unit tests for engine.leg_builder — strike selection + economics primitives."""
from __future__ import annotations

from datetime import date

import pytest

from engine import leg_builder
from engine.leg_builder import (
    breakevens,
    build_bear_call_spread,
    build_bear_put_spread,
    build_bull_call_spread,
    build_bull_put_spread,
    build_iron_butterfly,
    build_iron_condor,
    build_jade_lizard,
    build_long_call,
    build_long_put,
    build_long_straddle,
    build_long_strangle,
    closest_strike,
    estimate_pop,
    max_profit_loss,
    mid_price,
    net_premium,
    pop_from_delta,
    price_band,
    spread_width,
)


# ---------------------------------------------------------------------------
# Strike + price helpers
# ---------------------------------------------------------------------------

class TestClosestStrike:
    def test_picks_nearest(self):
        assert closest_strike([22950, 23000, 23050], 23010) == 23000

    def test_ties_pick_first(self):
        # min() with stable ordering picks first when distances tie
        assert closest_strike([22950, 23050], 23000) in (22950, 23050)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            closest_strike([], 23000)


class TestMidAndBand:
    def test_uses_settle_when_present(self):
        assert mid_price({"settle_price": 100, "close_price": 95}) == 100

    def test_falls_back_to_close(self):
        assert mid_price({"settle_price": 0, "close_price": 95}) == 95

    def test_band_rounds_2pct(self):
        lo, hi = price_band({"settle_price": 100, "close_price": 95}, band_pct=0.02)
        assert lo == 98.0 and hi == 102.0

    def test_zero_price_yields_zero_band(self):
        assert price_band({"settle_price": 0, "close_price": 0}) == (0.0, 0.0)


class TestPopFromDelta:
    def test_short_pop_inverts_delta(self):
        assert pop_from_delta(0.30, "SELL") == pytest.approx(70.0)

    def test_long_pop_uses_delta(self):
        assert pop_from_delta(0.30, "BUY") == pytest.approx(30.0)

    def test_clamped_0_100(self):
        assert pop_from_delta(1.5, "SELL") == 0.0
        assert pop_from_delta(-1.5, "BUY") == 100.0


# ---------------------------------------------------------------------------
# Strategy builders — structure tests using the synthetic chain fixture
# ---------------------------------------------------------------------------

class TestIronCondor:
    def test_produces_4_legs(self, sample_chain, expiry_date):
        legs = build_iron_condor(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        assert len(legs) == 4
        actions = [l.action for l in legs]
        assert actions.count("SELL") == 2 and actions.count("BUY") == 2

    def test_short_strikes_at_expected_move(self, sample_chain, expiry_date):
        legs = build_iron_condor(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        short_call = next(l for l in legs if l.action == "SELL" and l.option_type == "CE")
        short_put  = next(l for l in legs if l.action == "SELL" and l.option_type == "PE")
        # Short strikes should be ≈ spot ± EM (300 pts)
        assert 23250 <= short_call.strike <= 23350
        assert 22650 <= short_put.strike  <= 22750

    def test_empty_chain_raises(self, expiry_date):
        with pytest.raises(ValueError):
            build_iron_condor(
                underlying="NIFTY", expiry=expiry_date, chain=[],
                spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
            )


class TestSpreads:
    def test_bull_put_2_legs(self, sample_chain, expiry_date):
        legs = build_bull_put_spread(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        assert len(legs) == 2
        assert all(l.option_type == "PE" for l in legs)
        # Long put strike < short put strike (further OTM hedge)
        short = next(l for l in legs if l.action == "SELL")
        long  = next(l for l in legs if l.action == "BUY")
        assert long.strike < short.strike

    def test_bear_call_2_legs(self, sample_chain, expiry_date):
        legs = build_bear_call_spread(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        assert len(legs) == 2
        short = next(l for l in legs if l.action == "SELL")
        long  = next(l for l in legs if l.action == "BUY")
        assert long.strike > short.strike

    def test_bull_call_spread_debit(self, sample_chain, expiry_date):
        legs = build_bull_call_spread(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        assert len(legs) == 2
        # Long is at-the-money, short is further OTM
        long_call  = next(l for l in legs if l.action == "BUY")
        short_call = next(l for l in legs if l.action == "SELL")
        assert short_call.strike > long_call.strike

    def test_bear_put_spread_debit(self, sample_chain, expiry_date):
        legs = build_bear_put_spread(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        long_put  = next(l for l in legs if l.action == "BUY")
        short_put = next(l for l in legs if l.action == "SELL")
        assert short_put.strike < long_put.strike


class TestLongPremium:
    def test_long_straddle_atm_both_legs(self, sample_chain, expiry_date):
        legs = build_long_straddle(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, lots=1, lot_size=75,
        )
        assert len(legs) == 2
        assert legs[0].strike == legs[1].strike == 23000.0
        assert all(l.action == "BUY" for l in legs)

    def test_long_strangle_otm_both_sides(self, sample_chain, expiry_date):
        legs = build_long_strangle(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        assert len(legs) == 2
        long_call = next(l for l in legs if l.option_type == "CE")
        long_put  = next(l for l in legs if l.option_type == "PE")
        assert long_call.strike > 23000 and long_put.strike < 23000

    def test_long_call_single_leg(self, sample_chain, expiry_date):
        legs = build_long_call(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, lots=1, lot_size=75,
        )
        assert len(legs) == 1 and legs[0].action == "BUY" and legs[0].option_type == "CE"

    def test_long_put_single_leg(self, sample_chain, expiry_date):
        legs = build_long_put(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, lots=1, lot_size=75,
        )
        assert len(legs) == 1 and legs[0].action == "BUY" and legs[0].option_type == "PE"


class TestComplexCreditStrategies:
    def test_iron_butterfly_4_legs(self, sample_chain, expiry_date):
        legs = build_iron_butterfly(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        assert len(legs) == 4
        # Both short legs at ATM
        shorts = [l for l in legs if l.action == "SELL"]
        assert all(l.strike == 23000.0 for l in shorts)

    def test_jade_lizard_3_legs(self, sample_chain, expiry_date):
        legs = build_jade_lizard(
            underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
            spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
        )
        assert len(legs) == 3
        # 2 sells, 1 buy
        actions = [l.action for l in legs]
        assert actions.count("SELL") == 2 and actions.count("BUY") == 1


# ---------------------------------------------------------------------------
# Economics primitives (use make_leg fixture for deterministic prices)
# ---------------------------------------------------------------------------

class TestEconomicsPrimitives:
    def test_net_premium_credit(self, make_leg):
        legs = [
            make_leg(1, 22800, "PE", "SELL", price=50),
            make_leg(2, 22700, "PE", "BUY",  price=30),
        ]
        # SELL adds, BUY subtracts → 50 - 30 = 20 net credit
        assert net_premium(legs) == 20.0

    def test_spread_width_pe_only(self, make_leg):
        legs = [
            make_leg(1, 22800, "PE", "SELL"),
            make_leg(2, 22700, "PE", "BUY"),
        ]
        assert spread_width(legs) == 100.0

    def test_spread_width_iron_condor(self, make_leg):
        legs = [
            make_leg(1, 22800, "PE", "SELL"),
            make_leg(2, 22700, "PE", "BUY"),
            make_leg(3, 23200, "CE", "SELL"),
            make_leg(4, 23300, "CE", "BUY"),
        ]
        # Each side width = 100, max = 100
        assert spread_width(legs) == 100.0

    def test_max_profit_loss_iron_condor(self, make_leg):
        legs = [
            make_leg(1, 22800, "PE", "SELL", price=50),
            make_leg(2, 22700, "PE", "BUY",  price=30),
            make_leg(3, 23200, "CE", "SELL", price=55),
            make_leg(4, 23300, "CE", "BUY",  price=35),
        ]
        # Net credit = 50 - 30 + 55 - 35 = 40, width = 100 → max loss = 60
        mp, ml = max_profit_loss(legs, "IRON_CONDOR")
        assert mp == 40.0 and ml == 60.0

    def test_max_profit_loss_long_straddle_unbounded(self, make_leg):
        legs = [
            make_leg(1, 23000, "CE", "BUY", price=120),
            make_leg(2, 23000, "PE", "BUY", price=120),
        ]
        mp, ml = max_profit_loss(legs, "LONG_STRADDLE")
        assert mp == float("inf") and ml == 240.0

    def test_breakevens_iron_condor(self, make_leg):
        legs = [
            make_leg(1, 22800, "PE", "SELL", price=50),
            make_leg(2, 22700, "PE", "BUY",  price=30),
            make_leg(3, 23200, "CE", "SELL", price=55),
            make_leg(4, 23300, "CE", "BUY",  price=35),
        ]
        np_ = 40.0
        upper, lower = breakevens(legs, "IRON_CONDOR")
        assert upper == 23200 + np_  # 23240
        assert lower == 22800 - np_  # 22760


# ---------------------------------------------------------------------------
# FUTURE-SCOPE PLACEHOLDERS — see FUTURE_ENHANCEMENT_SCOPES.md
# ---------------------------------------------------------------------------

@pytest.mark.future
@pytest.mark.skip(reason="future: LONG_STRANGLE should use ±1.0 EM, not ±0.5 EM (FUTURE_ENHANCEMENT_SCOPES.md → Engine Correctness)")
def test_long_strangle_uses_full_expected_move(sample_chain, expiry_date):
    """When fixed, long strangle strikes should sit at ±EM not ±0.5×EM."""
    legs = build_long_strangle(
        underlying="NIFTY", expiry=expiry_date, chain=sample_chain,
        spot=23000.0, expected_move=300.0, lots=1, lot_size=75,
    )
    long_call = next(l for l in legs if l.option_type == "CE")
    long_put  = next(l for l in legs if l.option_type == "PE")
    # Expected after fix: ≈ 23300 / 22700 (spot ± 1.0×EM)
    assert long_call.strike >= 23250
    assert long_put.strike  <= 22750


@pytest.mark.future
@pytest.mark.skip(reason="future: JADE_LIZARD must validate net_credit ≥ call_spread_width (FUTURE_ENHANCEMENT_SCOPES.md → Engine Correctness)")
def test_jade_lizard_vetoes_when_net_credit_below_call_spread_width():
    """When fixed, builder/selector should raise StrategyVeto if upside risk is undefined."""
    pass
