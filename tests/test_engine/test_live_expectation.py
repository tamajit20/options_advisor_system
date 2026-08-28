"""Unit tests for engine.live_expectation — live PoP / EV on open trades."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from engine.live_expectation import (
    assess_direction_fit,
    live_trade_outlook,
    normalize_atm_iv,
    resolve_market_inputs,
)


def _strangle_legs():
    return [
        {"leg_order": 1, "action": "SELL", "strike": 23000.0, "option_type": "CE",
         "fill_price": 100.0, "lots": 1, "lot_size": 50},
        {"leg_order": 2, "action": "SELL", "strike": 23000.0, "option_type": "PE",
         "fill_price": 100.0, "lots": 1, "lot_size": 50},
    ]


def _ic_legs():
    return [
        {"leg_order": 1, "action": "SELL", "strike": 23200.0, "option_type": "CE",
         "fill_price": 80.0, "lots": 1, "lot_size": 50},
        {"leg_order": 2, "action": "BUY", "strike": 23400.0, "option_type": "CE",
         "fill_price": 35.0, "lots": 1, "lot_size": 50},
        {"leg_order": 3, "action": "SELL", "strike": 22800.0, "option_type": "PE",
         "fill_price": 80.0, "lots": 1, "lot_size": 50},
        {"leg_order": 4, "action": "BUY", "strike": 22600.0, "option_type": "PE",
         "fill_price": 35.0, "lots": 1, "lot_size": 50},
    ]


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
        out = live_trade_outlook(
            legs=_strangle_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=date(2026, 5, 28),
            spot=23000.0, dte=0, atm_iv=None,
            max_profit=10000.0, max_loss=10000.0,
        )
        # Same-strike shorts: BEs are 23000 ± net credit 200.
        assert out["live_pop"] == 100.0
        assert out["live_ev"] == 10000.0

    def test_expiry_day_outside_bes_is_certain_loss_for_credit(self):
        out = live_trade_outlook(
            legs=_strangle_legs(), strategy="IRON_CONDOR",
            underlying="NIFTY", expiry=date(2026, 5, 28),
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
        assert out["direction_label"] == "Range-friendly"
        assert out["data_source"] == "eod"

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
