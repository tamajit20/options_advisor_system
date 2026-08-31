"""Unit tests for engine.live_expectation — live PoP / EV on open trades."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from utils import now_ist

from engine.leg_builder import estimate_pop
from contracts import SuggestionLeg
from engine.live_expectation import (
    assess_direction_fit,
    enrich_trade_outlook,
    expiry_ev_note,
    hold_vs_close_advice,
    legs_from_fills,
    live_trade_outlook,
    normalize_atm_iv,
    outlook_horizon,
    parse_entry_regime,
    resolve_market_inputs,
)


def _strangle_legs():
    exp = now_ist().date() + timedelta(days=10)
    return [
        {"leg_order": 1, "action": "SELL", "strike": 23000.0, "option_type": "CE",
         "expiry_date": exp, "fill_price": 100.0, "lots": 1, "lot_size": 50},
        {"leg_order": 2, "action": "SELL", "strike": 23000.0, "option_type": "PE",
         "expiry_date": exp, "fill_price": 100.0, "lots": 1, "lot_size": 50},
    ]


def _ic_legs():
    exp = now_ist().date() + timedelta(days=10)
    return [
        {"leg_order": 1, "action": "SELL", "strike": 23200.0, "option_type": "CE",
         "expiry_date": exp, "fill_price": 80.0, "lots": 1, "lot_size": 50},
        {"leg_order": 2, "action": "BUY", "strike": 23400.0, "option_type": "CE",
         "expiry_date": exp, "fill_price": 35.0, "lots": 1, "lot_size": 50},
        {"leg_order": 3, "action": "SELL", "strike": 22800.0, "option_type": "PE",
         "expiry_date": exp, "fill_price": 80.0, "lots": 1, "lot_size": 50},
        {"leg_order": 4, "action": "BUY", "strike": 22600.0, "option_type": "PE",
         "expiry_date": exp, "fill_price": 35.0, "lots": 1, "lot_size": 50},
    ]


def _calendar_legs(*, near=None, far=None):
    near = near or date(2026, 9, 1)
    far = far or date(2026, 9, 8)
    return [
        {"leg_order": 1, "action": "SELL", "strike": 24000.0, "option_type": "CE",
         "expiry_date": near, "fill_price": 120.0, "lots": 1, "lot_size": 50},
        {"leg_order": 2, "action": "BUY", "strike": 24000.0, "option_type": "CE",
         "expiry_date": far, "fill_price": 280.0, "lots": 1, "lot_size": 50},
    ]


class TestLegsFromFills:
    def test_parses_string_expiry_on_legs(self):
        today = now_ist().date()
        exp = today + timedelta(days=5)
        legs = [
            {"leg_order": 1, "action": "SELL", "strike": 24000.0, "option_type": "CE",
             "expiry_date": exp.isoformat(), "fill_price": 100.0, "lots": 1, "lot_size": 50},
        ]
        parsed = legs_from_fills(legs, underlying="NIFTY", expiry=today + timedelta(days=20))
        assert len(parsed) == 1
        assert parsed[0].expiry_date == exp


class TestOutlookHorizon:
    def test_calendar_uses_near_leg_dte(self):
        today = now_ist().date()
        near = today + timedelta(days=1)
        far = today + timedelta(days=8)
        h = outlook_horizon(
            strategy="CALENDAR_SPREAD",
            legs=_calendar_legs(near=near, far=far),
            fallback_expiry=far,
            as_of=today,
        )
        assert h["near_dte"] == 1
        assert h["far_dte"] == 8
        assert h["outlook_dte"] == 1
        assert h["outlook_expiry"] == near

    @pytest.mark.parametrize("strategy", [
        "IRON_CONDOR",
        "IRON_BUTTERFLY",
        "SHORT_STRANGLE",
        "BULL_PUT_SPREAD",
        "BEAR_CALL_SPREAD",
        "JADE_LIZARD",
        "LONG_STRADDLE",
        "LONG_STRANGLE",
        "LONG_CALL",
        "LONG_PUT",
        "BULL_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
    ])
    def test_single_expiry_strategy_dte_unchanged(self, strategy):
        """Non-calendar strategies share one expiry — DTE must match that expiry."""
        today = now_ist().date()
        exp = today + timedelta(days=12)
        legs = _ic_legs() if "IRON" in strategy or strategy in (
            "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "JADE_LIZARD",
            "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD",
        ) else _strangle_legs()
        for leg in legs:
            leg["expiry_date"] = exp
        h = outlook_horizon(
            strategy=strategy,
            legs=legs,
            fallback_expiry=exp,
            as_of=today,
        )
        assert h["outlook_dte"] == 12
        assert h["outlook_expiry"] == exp
        assert "far_dte" not in h


class TestNormalizeAtmIv:
    def test_decimal_passthrough(self):
        assert normalize_atm_iv(0.18) == 0.18

    def test_percent_converted(self):
        assert normalize_atm_iv(18.0) == 0.18

    def test_missing(self):
        assert normalize_atm_iv(None) is None
        assert normalize_atm_iv(0) is None


class TestLiveTradeOutlook:
    def test_waiting_without_spot(self):
        out = live_trade_outlook(
            legs=_strangle_legs(), strategy="SHORT_STRANGLE",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=None, dte=10, atm_iv=0.18,
            max_profit=10000.0, max_loss=10000.0, entry_pop=70.0,
        )
        assert out["live_pop"] is None
        assert out["live_ev"] is None
        assert "spot" in out["summary"].lower() or "Waiting" in out["summary"]

    def test_waiting_without_iv(self):
        out = live_trade_outlook(
            legs=_strangle_legs(), strategy="SHORT_STRANGLE",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=23000.0, dte=10, atm_iv=None,
            max_profit=10000.0, max_loss=10000.0,
        )
        assert out["live_pop"] is None
        assert out["spot"] == 23000.0

    def test_credit_pop_higher_when_spot_centered(self):
        kwargs = dict(
            legs=_ic_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            dte=10, atm_iv=0.18,
            max_profit=4500.0, max_loss=15500.0, entry_pop=65.0,
        )
        centered = live_trade_outlook(spot=23000.0, **kwargs)
        stressed = live_trade_outlook(spot=23240.0, **kwargs)
        assert centered["live_pop"] is not None
        assert stressed["live_pop"] is not None
        assert centered["live_pop"] > stressed["live_pop"]
        assert centered["live_ev"] > stressed["live_ev"]

    def test_ev_is_two_outcome_blend(self):
        out = live_trade_outlook(
            legs=_strangle_legs(), strategy="SHORT_STRANGLE",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=23000.0, dte=10, atm_iv=0.18,
            max_profit=10000.0, max_loss=20000.0,
        )
        p = out["live_pop"] / 100.0
        assert out["live_ev"] == round(p * 10000.0 + (1 - p) * (-20000.0), 2)

    def test_stance_improving_vs_entry(self):
        out = live_trade_outlook(
            legs=_strangle_legs(), strategy="SHORT_STRANGLE",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=23000.0, dte=10, atm_iv=0.10,
            max_profit=10000.0, max_loss=10000.0, entry_pop=40.0,
        )
        assert out["stance"] == "improving"
        assert out["pop_delta"] is not None and out["pop_delta"] >= 5

    def test_expiry_day_inside_bes_is_certain_win_for_credit(self):
        exp = now_ist().date()
        legs = [
            {"leg_order": 1, "action": "SELL", "strike": 23000.0, "option_type": "CE",
             "expiry_date": exp, "fill_price": 100.0, "lots": 1, "lot_size": 50},
            {"leg_order": 2, "action": "SELL", "strike": 23000.0, "option_type": "PE",
             "expiry_date": exp, "fill_price": 100.0, "lots": 1, "lot_size": 50},
        ]
        out = live_trade_outlook(
            legs=legs, strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=exp,
            spot=23000.0, dte=0, atm_iv=None,
            max_profit=10000.0, max_loss=10000.0,
        )
        # Same-strike shorts: BEs are 23000 ± net credit 200.
        assert out["live_pop"] == 100.0
        assert out["live_ev"] == 10000.0

    def test_expiry_day_outside_bes_is_certain_loss_for_credit(self):
        exp = now_ist().date()
        legs = [
            {"leg_order": 1, "action": "SELL", "strike": 23000.0, "option_type": "CE",
             "expiry_date": exp, "fill_price": 100.0, "lots": 1, "lot_size": 50},
            {"leg_order": 2, "action": "SELL", "strike": 23000.0, "option_type": "PE",
             "expiry_date": exp, "fill_price": 100.0, "lots": 1, "lot_size": 50},
        ]
        out = live_trade_outlook(
            legs=legs, strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=exp,
            spot=24000.0, dte=0, atm_iv=None,
            max_profit=10000.0, max_loss=8000.0,
        )
        assert out["live_pop"] == 0.0
        assert out["live_ev"] == -8000.0
        assert out["em_vs_be"] == "outside"

    def test_spot_change_vs_entry(self):
        out = live_trade_outlook(
            legs=_strangle_legs(), strategy="SHORT_STRANGLE",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=23150.0, dte=7, atm_iv=0.18,
            max_profit=10000.0, max_loss=10000.0,
            entry_spot=23000.0,
        )
        assert out["spot_change"] == 150.0
        assert out["expected_move"] is not None and out["expected_move"] > 0

    def test_direction_aligned_for_centered_iron_condor(self):
        out = live_trade_outlook(
            legs=_ic_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=23000.0, dte=10, atm_iv=0.18,
            max_profit=4500.0, max_loss=15500.0, entry_spot=23000.0,
            data_source="eod",
        )
        assert out["direction_fit"] == "aligned"
        assert out["direction_label"] == "Inside breakevens"
        assert out["data_source"] == "eod"

    def test_iron_condor_uses_breakevens_not_short_strikes(self):
        """Spot can be outside short body but inside breakevens — still aligned."""
        out = live_trade_outlook(
            legs=_ic_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=22750.0, dte=10, atm_iv=0.18,
            max_profit=4500.0, max_loss=15500.0,
        )
        assert out["direction_fit"] == "aligned"
        assert out["direction_label"] == "Inside breakevens"

    def test_iron_condor_past_lower_breakeven(self):
        out = live_trade_outlook(
            legs=_ic_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=22650.0, dte=10, atm_iv=0.18,
            max_profit=4500.0, max_loss=15500.0,
            entry_spot=23000.0,
        )
        assert out["direction_fit"] == "against"
        assert out["direction_label"] == "Past breakeven"
        assert "MTM" in out["direction_detail"] or "structural" in out["direction_detail"].lower()

    def test_calendar_near_dte_and_low_pop_when_past_be(self):
        today = now_ist().date()
        near = today + timedelta(days=1)
        far = today + timedelta(days=8)
        legs = _calendar_legs(near=near, far=far)
        sug = [
            SuggestionLeg(
                leg_order=1, hedge_pair_leg=2, symbol="NIFTY", expiry_date=near,
                strike=24000.0, option_type="CE", action="SELL",
                lots=1, lot_size=50, suggested_price=120.0,
                suggested_price_low=120.0, suggested_price_high=120.0, leg_purpose_note="",
            ),
            SuggestionLeg(
                leg_order=2, hedge_pair_leg=1, symbol="NIFTY", expiry_date=far,
                strike=24000.0, option_type="CE", action="BUY",
                lots=1, lot_size=50, suggested_price=280.0,
                suggested_price_low=280.0, suggested_price_high=280.0, leg_purpose_note="",
            ),
        ]
        pop_far_horizon = estimate_pop(sug, spot=23816.0, dte=8, atm_iv=0.18, strategy="CALENDAR_SPREAD")
        pop_near_horizon = estimate_pop(sug, spot=23816.0, dte=1, atm_iv=0.18, strategy="CALENDAR_SPREAD")
        pop_credit_bug = estimate_pop(sug, spot=23816.0, dte=8, atm_iv=0.18, strategy="SHORT_STRANGLE")
        out = live_trade_outlook(
            legs=legs, strategy="CALENDAR_SPREAD",
            underlying="NIFTY", expiry=far,
            spot=23816.0, dte=8, atm_iv=0.18,
            max_profit=6000.0, max_loss=8000.0, entry_pop=48.0,
        )
        assert out["dte"] == 1
        assert out["far_dte"] == 8
        assert out["direction_fit"] == "against"
        assert out["live_pop"] is not None
        assert out["live_pop"] == pytest.approx(pop_near_horizon, rel=0.01)
        assert pop_near_horizon < pop_credit_bug
        assert out["live_ev"] < 600.0

    def test_long_call_uses_breakeven_not_strike(self):
        legs = [
            {"leg_order": 1, "action": "BUY", "strike": 23000.0, "option_type": "CE",
             "fill_price": 150.0, "lots": 1, "lot_size": 50},
        ]
        fit = assess_direction_fit(
            strategy="LONG_CALL",
            underlying="NIFTY",
            spot=23050.0,
            upper_be=23150.0,
            lower_be=None,
            legs=legs,
        )
        assert fit["direction_fit"] == "against"
        assert "rally" in fit["direction_label"].lower() or fit["direction_fit"] == "against"

    def test_direction_against_for_bull_put_below_short_strike(self):
        legs = [
            {"leg_order": 1, "action": "SELL", "strike": 23200.0, "option_type": "PE",
             "fill_price": 80.0, "lots": 1, "lot_size": 50},
            {"leg_order": 2, "action": "BUY", "strike": 23000.0, "option_type": "PE",
             "fill_price": 35.0, "lots": 1, "lot_size": 50},
        ]
        fit = assess_direction_fit(
            strategy="BULL_PUT_SPREAD",
            underlying="NIFTY",
            spot=22800.0,
            upper_be=23250.0,
            lower_be=23100.0,
            legs=legs,
            spot_change=-200.0,
            entry_spot=23000.0,
        )
        assert fit["direction_fit"] == "against"
        assert "rally" in fit["direction_label"].lower()

    def test_resolve_market_inputs_prefers_eod_when_no_live(self):
        db = MagicMock()
        db.fetch_one.side_effect = [
            None,
            {"close_price": 22950.0, "trade_date": date(2026, 5, 27)},
            None,
        ]
        out = resolve_market_inputs(db, "NIFTY", date(2026, 5, 28))
        assert out["spot"] == 22950.0
        assert out["data_source"] == "eod"


class TestEnrichTradeOutlook:
    def test_be_distance_outside_upper(self):
        base = live_trade_outlook(
            legs=_ic_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=23300.0, dte=10, atm_iv=0.18,
            max_profit=4500.0, max_loss=15500.0, entry_pop=65.0,
        )
        out = enrich_trade_outlook(
            base, current_mtm=1200.0, max_profit=4500.0, max_loss=15500.0,
            legs=_ic_legs(), strategy="IRON_CONDOR", underlying="NIFTY",
            expiry=date(2026, 5, 28), dte=10, include_scenarios=True,
        )
        assert out["be_distance_text"]
        assert out["entry_ev"] is not None
        assert out["close_now_ev"] == 1200.0
        assert len(out.get("scenarios") or []) == 3

    def test_hold_vs_close_suggests_booking_when_mtm_beats_ev(self):
        advice = hold_vs_close_advice(
            current_mtm=6000.0, hold_ev=2000.0, direction_fit="against",
        )
        assert advice and "Closing now" in advice

    def test_hold_vs_close_when_mtm_positive_ev_negative(self):
        advice = hold_vs_close_advice(
            current_mtm=112.0, hold_ev=-6144.0, direction_fit="against",
            strategy="CALENDAR_SPREAD",
        )
        assert advice and "Close now" in advice
        assert "not extra gain" in advice

    def test_expiry_ev_note_for_calendar(self):
        note = expiry_ev_note(
            strategy="CALENDAR_SPREAD",
            max_profit=15540.0,
            max_loss=9270.0,
            near_dte=1,
            current_mtm=112.0,
        )
        assert note and "15,540" in note
        assert "not profit on top" in note
        assert "Close now" in note

    def test_parse_entry_regime_from_conditions(self):
        cond = [
            {"label": "IV rank", "detail": "IV Rank 42.5 — elevated"},
            {"label": "VIX regime", "detail": "VIX regime: NORMAL"},
            {"label": "Trend", "detail": "Trend: SIDEWAYS"},
        ]
        reg = parse_entry_regime(cond)
        assert reg["iv_rank"] == 42.5
        assert reg["vix_regime"] == "NORMAL"
        assert reg["trend"] == "SIDEWAYS"
