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


def test_long_straddle_thesis_fail_near_expiry():
    """LONG_STRANGLE thesis exit when DTE low and losing ≥ min_loss_fraction."""
    legs = [
        {"action": "BUY", "strike": 23000.0, "option_type": "CE",
         "fill_price": 100.0, "lots": 1, "lot_size": 50},
        {"action": "BUY", "strike": 23000.0, "option_type": "PE",
         "fill_price": 100.0, "lots": 1, "lot_size": 50},
    ]
    entry_debit = -(100 + 100) * 50
    max_loss = abs(entry_debit)
    chain = [
        {"strike": 23000.0, "option_type": "CE", "mid_price": 75.0},
        {"strike": 23000.0, "option_type": "PE", "mid_price": 75.0},
    ]
    result = evaluate_exit(
        trade_id="T-THESIS",
        legs=legs,
        current_chain=chain,
        entry_net_credit=entry_debit,
        max_profit_rs=float("inf"),
        max_loss_rs=max_loss,
        sl_level_per_share=None,
        days_to_expiry=4,
        strategy="LONG_STRANGLE",
    )
    assert result.decision == "THESIS_FAIL"
    assert "thesis window closed" in result.reason


def test_long_straddle_sl_at_hybrid_cap():
    """LONG_STRADDLE SL binds at min(50% debit, ₹10k cap) → ₹10k on BNIFTY-sized debit."""
    legs = [
        {"action": "BUY", "strike": 54900.0, "option_type": "CE",
         "fill_price": 1178.0, "lots": 1, "lot_size": 35},
        {"action": "BUY", "strike": 54900.0, "option_type": "PE",
         "fill_price": 838.0, "lots": 1, "lot_size": 35},
    ]
    entry_debit = -(1178 + 838) * 35
    max_loss = abs(entry_debit)
    chain = [
        {"strike": 54900.0, "option_type": "CE", "mid_price": 900.0},
        {"strike": 54900.0, "option_type": "PE", "mid_price": 750.0},
    ]
    result = evaluate_exit(
        trade_id="T-STRADDLE",
        legs=legs,
        current_chain=chain,
        entry_net_credit=entry_debit,
        max_profit_rs=float("inf"),
        max_loss_rs=max_loss,
        sl_level_per_share=None,
        days_to_expiry=10,
        strategy="LONG_STRADDLE",
    )
    assert result.decision == "SL_HIT"
    assert "10,000" in result.reason or "10000" in result.reason.replace(",", "")


def test_long_straddle_holds_below_hybrid_cap():
    legs = [
        {"action": "BUY", "strike": 54900.0, "option_type": "CE",
         "fill_price": 1178.0, "lots": 1, "lot_size": 35},
        {"action": "BUY", "strike": 54900.0, "option_type": "PE",
         "fill_price": 838.0, "lots": 1, "lot_size": 35},
    ]
    entry_debit = -(1178 + 838) * 35
    chain = [
        {"strike": 54900.0, "option_type": "CE", "mid_price": 1100.0},
        {"strike": 54900.0, "option_type": "PE", "mid_price": 820.0},
    ]
    result = evaluate_exit(
        trade_id="T-STRADDLE",
        legs=legs,
        current_chain=chain,
        entry_net_credit=entry_debit,
        max_profit_rs=float("inf"),
        max_loss_rs=abs(entry_debit),
        sl_level_per_share=None,
        days_to_expiry=10,
        strategy="LONG_STRADDLE",
    )
    assert result.decision == "HOLD"


# ---------------------------------------------------------------------------
# FUTURE-SCOPE PLACEHOLDERS — see FUTURE_ENHANCEMENT_SCOPES.md
# ---------------------------------------------------------------------------

def test_put_side_uses_tighter_sl_fraction():
    """S5: BULL_PUT_SPREAD uses 40% SL fraction (tighter than default 50%)."""
    from engine.sl_threshold import strategy_sl_config

    bps = strategy_sl_config("BULL_PUT_SPREAD")
    bcs = strategy_sl_config("BEAR_CALL_SPREAD")
    assert bps["loss_fraction"] < bcs["loss_fraction"]
    assert bps["loss_fraction"] <= 0.40


def test_jade_lizard_uses_same_tight_sl_as_bull_put_spread():
    """S5: JADE_LIZARD has a naked short put — same tight SL as BULL_PUT_SPREAD."""
    from engine.sl_threshold import strategy_sl_config

    jl = strategy_sl_config("JADE_LIZARD")
    bps = strategy_sl_config("BULL_PUT_SPREAD")
    assert jl["loss_fraction"] == bps["loss_fraction"]
