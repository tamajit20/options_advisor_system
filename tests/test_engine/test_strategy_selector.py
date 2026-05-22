"""Unit tests for engine.strategy_selector — the 11-strategy decision tree.

Critical regression net: any change to the dispatch matrix should be caught here.

NOTE: One known issue surfaces here intentionally — see FUTURE_ENHANCEMENT_SCOPES.md
"LONG_STRANGLE strategy is dead code". The current selector still routes to it for
buying-regime + directional trends *unless* IV is very low (<20) AND PCR is strong.
We assert the *current* behaviour; the FUTURE-marked test below documents the
expected fix.
"""
from __future__ import annotations

from datetime import date
from dataclasses import replace

import pytest

from contracts import MarketIndicators
from engine.strategy_selector import select_strategy
from exceptions import StrategyVeto


def _ind(pcr: float = 1.0, iv_premium: float = 1.10) -> MarketIndicators:
    """Helper — build a MarketIndicators with custom PCR/iv_premium."""
    return MarketIndicators(
        symbol="NIFTY", as_of=date(2026, 4, 30), spot=23000.0,
        pcr=pcr, max_pain=23000.0, atr_14=200.0, trend="SIDEWAYS",
        vix_close=15.0, vix_regime="STABLE",
        oi_walls_call=[], oi_walls_put=[], expected_move=300.0,
        hv_20=0.16, iv_premium=iv_premium, fii_net_futures=0.0,
        adx_14=25.0, sma20_slope_pct=0.10, sma_diff_pct=0.10,
    )


class TestWritingRegime:
    """IV Rank > 50 → writing strategies."""

    def test_high_iv_sideways_returns_iron_condor(self):
        assert select_strategy(iv_rank=60.0, trend="SIDEWAYS",
                               indicators=_ind(iv_premium=1.10)) == "IRON_CONDOR"

    def test_very_high_iv_with_premium_returns_iron_butterfly(self):
        assert select_strategy(iv_rank=75.0, trend="SIDEWAYS",
                               indicators=_ind(iv_premium=1.50)) == "IRON_BUTTERFLY"

    def test_very_high_iv_low_premium_falls_back_to_condor(self):
        # iv_rank > 70 but iv_premium < 1.40 → not enough fear premium for IB
        assert select_strategy(iv_rank=75.0, trend="SIDEWAYS",
                               indicators=_ind(iv_premium=1.20)) == "IRON_CONDOR"

    def test_high_iv_bullish_strong_pcr_returns_jade_lizard(self):
        assert select_strategy(iv_rank=60.0, trend="BULLISH",
                               indicators=_ind(pcr=0.50)) == "JADE_LIZARD"

    def test_high_iv_bullish_mild_returns_bps(self):
        assert select_strategy(iv_rank=60.0, trend="BULLISH",
                               indicators=_ind(pcr=0.85)) == "BULL_PUT_SPREAD"

    def test_high_iv_bearish_returns_bcs(self):
        assert select_strategy(iv_rank=60.0, trend="BEARISH",
                               indicators=_ind(pcr=1.30)) == "BEAR_CALL_SPREAD"


class TestBuyingRegime:
    """IV Rank < 30 → buying strategies."""

    def test_low_iv_sideways_returns_long_straddle(self):
        assert select_strategy(iv_rank=20.0, trend="SIDEWAYS",
                               indicators=_ind()) == "LONG_STRADDLE"

    def test_very_low_iv_strong_bullish_returns_long_call(self):
        # IV < 20 + PCR < 0.55 → naked long
        assert select_strategy(iv_rank=15.0, trend="BULLISH",
                               indicators=_ind(pcr=0.50)) == "LONG_CALL"

    def test_very_low_iv_strong_bearish_returns_long_put(self):
        assert select_strategy(iv_rank=15.0, trend="BEARISH",
                               indicators=_ind(pcr=1.70)) == "LONG_PUT"

    def test_low_iv_mild_bullish_returns_long_strangle(self):
        # IV < 30 but ≥ 20, mild PCR → strangle (note: leg_builder uses ±0.5 EM)
        assert select_strategy(iv_rank=25.0, trend="BULLISH",
                               indicators=_ind(pcr=0.85)) == "LONG_STRANGLE"


class TestMidIvRegime:
    """30 ≤ IV Rank ≤ 50 — debit spreads (BCAL/BPUT) or veto on sideways."""

    def test_mid_iv_bullish_returns_bull_call_spread(self):
        assert select_strategy(iv_rank=40.0, trend="BULLISH",
                               indicators=_ind()) == "BULL_CALL_SPREAD"

    def test_mid_iv_bearish_returns_bear_put_spread(self):
        assert select_strategy(iv_rank=40.0, trend="BEARISH",
                               indicators=_ind()) == "BEAR_PUT_SPREAD"

    def test_mid_iv_sideways_returns_calendar_spread(self):
        # P4: mid-IV + SIDEWAYS is now handled by CALENDAR_SPREAD instead of vetoing.
        result = select_strategy(iv_rank=40.0, trend="SIDEWAYS", indicators=_ind())
        assert result == "CALENDAR_SPREAD"


class TestUnknownTrend:
    def test_writing_unknown_trend_raises(self):
        with pytest.raises(StrategyVeto):
            select_strategy(iv_rank=60.0, trend="WTF", indicators=_ind())

    def test_buying_unknown_trend_raises(self):
        with pytest.raises(StrategyVeto):
            select_strategy(iv_rank=20.0, trend="WTF", indicators=_ind())


# ---------------------------------------------------------------------------
# FUTURE-SCOPE PLACEHOLDERS — paired with FUTURE_ENHANCEMENT_SCOPES.md entries
# ---------------------------------------------------------------------------

def test_iron_condor_blocked_when_vix_rising_3day():
    """S4: IC vetoed when VIX has risen >20% over last 3 sessions."""
    # NOTE: VIX spike veto is enforced in assemble_suggestion (uses indicators.vix_nd_change_pct).
    # select_strategy only returns the strategy name. The full assemble_suggestion
    # veto is tested in test_suggestion_engine_integration.py::TestVixSpikeVeto.
    # Here we just confirm select_strategy itself does NOT raise (veto is downstream).
    result = select_strategy(iv_rank=65.0, trend="SIDEWAYS", indicators=_ind())
    assert result == "IRON_CONDOR"


def test_long_strangle_routing_reachable_in_mid_iv_boundary():
    """C3: LONG_STRANGLE is selected in the buying regime boundary (iv_rank ~25, BULLISH)."""
    # iv_rank 25 is below iv_rank_buying_max (30) and at / above iv_naked_long_max (20),
    # so LONG_CALL is NOT selected — LONG_STRANGLE is the fallback.
    result = select_strategy(iv_rank=25.0, trend="BULLISH", indicators=_ind())
    assert result == "LONG_STRANGLE"


def test_mid_iv_sideways_now_returns_calendar_spread():
    """P4 implemented: mid-IV + sideways returns CALENDAR_SPREAD (no more veto)."""
    result = select_strategy(iv_rank=40.0, trend="SIDEWAYS", indicators=_ind())
    assert result == "CALENDAR_SPREAD"
