"""Unit tests for engine.exit_engine — daily exit decision matrix."""
from __future__ import annotations

from datetime import datetime

import pytest

from engine.exit_engine import evaluate_exit


def _legs(short_strike=23200, long_strike=23300, fill_short=50.0, fill_long=20.0,
          lots=1, lot_size=75):
    """Single-side bear-call-spread legs for testing."""
    return [
        {"action": "SELL", "strike": short_strike, "option_type": "CE",
         "fill_price": fill_short, "lots": lots, "lot_size": lot_size},
        {"action": "BUY", "strike": long_strike, "option_type": "CE",
         "fill_price": fill_long, "lots": lots, "lot_size": lot_size},
    ]


def _chain(short_strike=23200, long_strike=23300, short_mid=10.0, long_mid=4.0):
    return [
        {"strike": short_strike, "option_type": "CE", "mid_price": short_mid},
        {"strike": long_strike,  "option_type": "CE", "mid_price": long_mid},
    ]


class TestExitEngine:
    def test_dte_zero_returns_expire(self):
        result = evaluate_exit(
            trade_id="T1", legs=_legs(), current_chain=_chain(),
            entry_net_credit=2250.0, max_profit_rs=2250.0, max_loss_rs=5250.0,
            sl_level_per_share=None, days_to_expiry=0,
            strategy="BEAR_CALL_SPREAD",
        )
        assert result.decision == "EXPIRE"

    def test_take_profit_at_50pct_for_bear_call_spread(self):
        # Entry credit = (50-20)*75 = 2250. Current cost to close = (10-4)*75 = 450
        # Current PnL = 2250 + (-(10*75) + (4*75)) = 2250 - 450 = 1800. Max profit = 2250
        # 1800 / 2250 = 80% > 50% TP threshold → TAKE_PROFIT
        result = evaluate_exit(
            trade_id="T1", legs=_legs(), current_chain=_chain(short_mid=10, long_mid=4),
            entry_net_credit=2250.0, max_profit_rs=2250.0, max_loss_rs=5250.0,
            sl_level_per_share=None, days_to_expiry=10,
            strategy="BEAR_CALL_SPREAD",
        )
        assert result.decision == "TAKE_PROFIT"

    def test_sl_hit_when_loss_exceeds_50pct_max_loss(self):
        # Spot rallied: short call now worth 100, long worth 50 → close cost = (100-50)*75 = 3750
        # PnL = 2250 - 3750 = -1500. SL = 50% × 5250 = 2625. -1500 not yet at SL.
        # Make it worse: close cost (200-100)*75 = 7500 → PnL = -5250 ≤ -2625 → SL_HIT
        result = evaluate_exit(
            trade_id="T1", legs=_legs(),
            current_chain=_chain(short_mid=200, long_mid=100),
            entry_net_credit=2250.0, max_profit_rs=2250.0, max_loss_rs=5250.0,
            sl_level_per_share=None, days_to_expiry=10,
            strategy="BEAR_CALL_SPREAD",
        )
        assert result.decision == "SL_HIT"

    def test_exit_tomorrow_at_dte_1(self):
        # Modest profit, DTE = 1
        result = evaluate_exit(
            trade_id="T1", legs=_legs(),
            current_chain=_chain(short_mid=40, long_mid=18),
            entry_net_credit=2250.0, max_profit_rs=2250.0, max_loss_rs=5250.0,
            sl_level_per_share=None, days_to_expiry=1,
            strategy="BEAR_CALL_SPREAD",
        )
        assert result.decision == "EXIT_TOMORROW"

    def test_time_decay_done_for_credit_spread_at_low_dte(self):
        # Credit spread + DTE ≤ 3 + no SL/TP triggered
        # PnL not at TP (< 50% of max) and not SL → TIME_DECAY_DONE
        result = evaluate_exit(
            trade_id="T1", legs=_legs(),
            current_chain=_chain(short_mid=40, long_mid=18),  # mild profit
            entry_net_credit=2250.0, max_profit_rs=2250.0, max_loss_rs=5250.0,
            sl_level_per_share=None, days_to_expiry=3,
            strategy="BEAR_CALL_SPREAD",
        )
        assert result.decision == "TIME_DECAY_DONE"

    def test_hold_when_in_band(self):
        result = evaluate_exit(
            trade_id="T1", legs=_legs(),
            current_chain=_chain(short_mid=45, long_mid=18),  # mild profit
            entry_net_credit=2250.0, max_profit_rs=2250.0, max_loss_rs=5250.0,
            sl_level_per_share=None, days_to_expiry=10,
            strategy="BEAR_CALL_SPREAD",
        )
        assert result.decision == "HOLD"


# ---------------------------------------------------------------------------
# FUTURE-SCOPE PLACEHOLDERS — see FUTURE_ENHANCEMENT_SCOPES.md
# ---------------------------------------------------------------------------

@pytest.mark.future
@pytest.mark.skip(reason="future: side-aware SL multiplier, asymmetric put/call (FUTURE_ENHANCEMENT_SCOPES.md → Strategy & Regime Coverage)")
def test_put_side_uses_tighter_sl_multiplier():
    """When implemented, put-side breach should trigger SL at 1.25× credit, not 1.5×."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: pre-event forced exit (FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring)")
def test_high_impact_event_within_2_days_forces_pre_event_exit():
    pass
