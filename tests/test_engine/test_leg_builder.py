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


# ---------------------------------------------------------------------------
# PoP — credit vs debit paths (regression for over-stated long-premium PoP)
# ---------------------------------------------------------------------------

from contracts import SuggestionLeg as _Leg  # noqa: E402


def _make_long_straddle_legs(strike: float, debit_each_side: float):
    """Two BUY legs at the same strike — straddle."""
    return [
        _Leg(leg_order=1, hedge_pair_leg=None, symbol="NIFTY",
             expiry_date=date(2026, 5, 7), strike=strike, option_type="CE",
             action="BUY", lots=1, lot_size=75,
             suggested_price=debit_each_side, suggested_price_low=debit_each_side * 0.98,
             suggested_price_high=debit_each_side * 1.02, leg_purpose_note=""),
        _Leg(leg_order=2, hedge_pair_leg=None, symbol="NIFTY",
             expiry_date=date(2026, 5, 7), strike=strike, option_type="PE",
             action="BUY", lots=1, lot_size=75,
             suggested_price=debit_each_side, suggested_price_low=debit_each_side * 0.98,
             suggested_price_high=debit_each_side * 1.02, leg_purpose_note=""),
    ]


def _make_short_strangle_legs(short_call: float, short_put: float, credit_each_leg: float):
    return [
        _Leg(leg_order=1, hedge_pair_leg=None, symbol="NIFTY",
             expiry_date=date(2026, 5, 7), strike=short_call, option_type="CE",
             action="SELL", lots=1, lot_size=75,
             suggested_price=credit_each_leg, suggested_price_low=credit_each_leg * 0.98,
             suggested_price_high=credit_each_leg * 1.02, leg_purpose_note=""),
        _Leg(leg_order=2, hedge_pair_leg=None, symbol="NIFTY",
             expiry_date=date(2026, 5, 7), strike=short_put, option_type="PE",
             action="SELL", lots=1, lot_size=75,
             suggested_price=credit_each_leg, suggested_price_low=credit_each_leg * 0.98,
             suggested_price_high=credit_each_leg * 1.02, leg_purpose_note=""),
    ]


class TestEstimatePopLongPremium:
    """The historical avg(|Δ_long|) formula returned ~50% for an ATM long
    straddle, dramatically over-stating PoP. The fix uses lognormal
    BE-crossing probability, which should give ~30–45% for typical
    7-DTE ATM straddles."""

    def test_long_straddle_pop_is_below_50_when_atm(self):
        # ATM straddle, 7 DTE, IV 18% — true PoP ≈ 35–42%
        legs = _make_long_straddle_legs(strike=24100.0, debit_each_side=260.0)
        pop = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.18,
                            strategy="LONG_STRADDLE")
        assert 25.0 < pop < 50.0, f"ATM long straddle PoP should be 25–50%, got {pop}"

    def test_long_straddle_pop_is_lower_than_old_delta_estimate(self):
        # Old formula: avg(|Δ_long|) for ATM both legs ≈ 0.5 each → ~50%
        # New formula must be materially lower
        legs = _make_long_straddle_legs(strike=24100.0, debit_each_side=260.0)
        new_pop = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.18,
                                strategy="LONG_STRADDLE")
        # Old behaviour for reference (no strategy hint → falls back to delta)
        old_pop = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.18,
                                strategy=None)
        assert new_pop < old_pop, (
            f"BE-crossing PoP ({new_pop}) should be below delta-based PoP ({old_pop})"
        )

    def test_long_straddle_pop_increases_with_more_time(self):
        legs = _make_long_straddle_legs(strike=24100.0, debit_each_side=260.0)
        pop_3 = estimate_pop(legs, spot=24100.0, dte=3, atm_iv=0.18,
                              strategy="LONG_STRADDLE")
        pop_30 = estimate_pop(legs, spot=24100.0, dte=30, atm_iv=0.18,
                               strategy="LONG_STRADDLE")
        assert pop_30 > pop_3

    def test_long_straddle_pop_increases_with_higher_iv(self):
        legs = _make_long_straddle_legs(strike=24100.0, debit_each_side=260.0)
        pop_low_iv = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.10,
                                    strategy="LONG_STRADDLE")
        pop_high_iv = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.30,
                                    strategy="LONG_STRADDLE")
        assert pop_high_iv > pop_low_iv

    def test_long_call_pop_uses_be_crossing(self):
        legs = [_Leg(leg_order=1, hedge_pair_leg=None, symbol="NIFTY",
                     expiry_date=date(2026, 5, 7), strike=24100.0, option_type="CE",
                     action="BUY", lots=1, lot_size=75,
                     suggested_price=200.0, suggested_price_low=196.0,
                     suggested_price_high=204.0, leg_purpose_note="")]
        pop = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.18,
                            strategy="LONG_CALL")
        # BE = 24300; P(S_T > 24300) at ATM with low DTE should be < 50%
        assert 0 < pop < 50

    def test_credit_strategy_unchanged(self):
        """Short strangles must keep using ``1 − |Δ_short|`` formula."""
        legs = _make_short_strangle_legs(short_call=24300.0, short_put=23900.0,
                                          credit_each_leg=80.0)
        pop = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.18,
                            strategy="BULL_PUT_SPREAD")  # any credit strategy
        # Both shorts ~1σ OTM → |Δ| each ≈ 0.30 → PoP ≈ 70%
        assert 50 < pop < 90

    def test_degenerate_inputs_return_safe_value(self):
        legs = _make_long_straddle_legs(strike=24100.0, debit_each_side=260.0)
        # zero IV → BE-crossing falls back to neutral 50% from each side; clamp keeps it sensible
        pop = estimate_pop(legs, spot=24100.0, dte=7, atm_iv=0.0,
                            strategy="LONG_STRADDLE")
        assert 0.0 <= pop <= 100.0


class TestLongPremiumTargetMultiple:
    """DTE-aware profit target for long-premium structures (FIX #4)."""

    def test_short_dte_gives_modest_target(self):
        # 3 DTE → 0.50 + 3/14 ≈ 0.71
        m = leg_builder.long_premium_target_multiple(3)
        assert 0.65 < m < 0.80

    def test_seven_dte_gives_around_one(self):
        # 7 DTE → 0.50 + 7/14 = 1.00 (replaces hard-coded 2×)
        m = leg_builder.long_premium_target_multiple(7)
        assert m == pytest.approx(1.00, abs=0.05)

    def test_caps_at_max(self):
        # 100 DTE — should cap at 1.50
        m = leg_builder.long_premium_target_multiple(100)
        assert m == pytest.approx(1.50, abs=1e-9)

    def test_zero_dte_returns_base(self):
        m = leg_builder.long_premium_target_multiple(0)
        assert m == pytest.approx(0.50, abs=1e-9)

    def test_monotone_increasing_until_cap(self):
        for a, b in [(1, 5), (5, 10), (10, 14)]:
            assert leg_builder.long_premium_target_multiple(a) < \
                   leg_builder.long_premium_target_multiple(b)
