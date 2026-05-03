"""Additional coverage for engine/strategy_selector.py — full matrix + assemble_suggestion."""
from __future__ import annotations

from datetime import date, datetime
from typing import List

import pytest

from contracts import ConfidenceCheck, ConfidenceResult, MarketIndicators
from engine import strategy_selector as ss
from exceptions import StrategyVeto


def _make_indicators(trend="SIDEWAYS", pcr=1.0, iv_premium=1.10):
    return MarketIndicators(
        symbol="NIFTY", as_of=date(2026, 4, 30), spot=23000.0,
        pcr=pcr, max_pain=23000.0, atr_14=200.0, trend=trend,
        vix_close=15.0, vix_regime="STABLE",
        oi_walls_call=[23200.0], oi_walls_put=[22800.0],
        expected_move=300.0, hv_20=0.16, iv_premium=iv_premium,
        fii_net_futures=10000.0, adx_14=25.0,
        sma20_slope_pct=0.10, sma_diff_pct=0.10,
    )


# ---------------------------------------------------------------------------
class TestSelectStrategyWritingRegime:
    def test_high_iv_sideways_returns_iron_condor(self):
        ind = _make_indicators(trend="SIDEWAYS", iv_premium=1.0)
        assert ss.select_strategy(iv_rank=60.0, trend="SIDEWAYS", indicators=ind) == "IRON_CONDOR"

    def test_very_high_iv_sideways_with_premium_returns_butterfly(self):
        ind = _make_indicators(trend="SIDEWAYS", iv_premium=1.5)
        assert ss.select_strategy(iv_rank=80.0, trend="SIDEWAYS", indicators=ind) == "IRON_BUTTERFLY"

    def test_high_iv_bullish_returns_bps(self):
        ind = _make_indicators(trend="BULLISH", pcr=0.8)
        assert ss.select_strategy(iv_rank=60.0, trend="BULLISH", indicators=ind) == "BULL_PUT_SPREAD"

    def test_high_iv_strong_bullish_returns_jade_lizard(self):
        ind = _make_indicators(trend="BULLISH", pcr=0.40)  # strong bullish (< 0.55)
        assert ss.select_strategy(iv_rank=60.0, trend="BULLISH", indicators=ind) == "JADE_LIZARD"

    def test_high_iv_bearish_returns_bcs(self):
        ind = _make_indicators(trend="BEARISH")
        assert ss.select_strategy(iv_rank=60.0, trend="BEARISH", indicators=ind) == "BEAR_CALL_SPREAD"


class TestSelectStrategyBuyingRegime:
    def test_low_iv_sideways_returns_long_straddle(self):
        ind = _make_indicators(trend="SIDEWAYS")
        assert ss.select_strategy(iv_rank=15.0, trend="SIDEWAYS", indicators=ind) == "LONG_STRADDLE"

    def test_low_iv_bullish_returns_long_strangle(self):
        ind = _make_indicators(trend="BULLISH", pcr=0.8)
        assert ss.select_strategy(iv_rank=25.0, trend="BULLISH", indicators=ind) == "LONG_STRANGLE"

    def test_very_low_iv_strong_bullish_returns_long_call(self):
        ind = _make_indicators(trend="BULLISH", pcr=0.40)
        assert ss.select_strategy(iv_rank=15.0, trend="BULLISH", indicators=ind) == "LONG_CALL"

    def test_very_low_iv_strong_bearish_returns_long_put(self):
        ind = _make_indicators(trend="BEARISH", pcr=1.80)
        assert ss.select_strategy(iv_rank=15.0, trend="BEARISH", indicators=ind) == "LONG_PUT"

    def test_low_iv_bearish_returns_long_strangle(self):
        ind = _make_indicators(trend="BEARISH", pcr=1.20)
        assert ss.select_strategy(iv_rank=25.0, trend="BEARISH", indicators=ind) == "LONG_STRANGLE"


class TestSelectStrategyMidRegime:
    def test_mid_iv_bullish_returns_bull_call_spread(self):
        ind = _make_indicators(trend="BULLISH")
        assert ss.select_strategy(iv_rank=40.0, trend="BULLISH", indicators=ind) == "BULL_CALL_SPREAD"

    def test_mid_iv_bearish_returns_bear_put_spread(self):
        ind = _make_indicators(trend="BEARISH")
        assert ss.select_strategy(iv_rank=40.0, trend="BEARISH", indicators=ind) == "BEAR_PUT_SPREAD"

    def test_mid_iv_sideways_vetoes(self):
        ind = _make_indicators(trend="SIDEWAYS")
        with pytest.raises(StrategyVeto, match="mid-zone"):
            ss.select_strategy(iv_rank=40.0, trend="SIDEWAYS", indicators=ind)


# ---------------------------------------------------------------------------
def _all_pass_confidence():
    checks = [ConfidenceCheck(label=f"c{i}", status="PASS", detail="") for i in range(7)]
    return ConfidenceResult(checks=checks, failed_reasons=[], score=7, total=7,
                            all_passed=True)


def _failing_confidence():
    checks = [ConfidenceCheck(label="c1", status="FAIL", detail="bad")]
    return ConfidenceResult(checks=checks, failed_reasons=["bad"], score=0,
                            total=7, all_passed=False)


class TestAssembleSuggestion:
    def test_vetoes_when_confidence_failed(self, sample_chain, sample_indicators):
        with pytest.raises(StrategyVeto, match="Confidence"):
            ss.assemble_suggestion(
                suggestion_id="S-1", underlying="NIFTY",
                expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
                spot=23000.0, chain=sample_chain,
                indicators=sample_indicators,
                confidence=_failing_confidence(),
                iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
            )

    def test_vetoes_when_chain_empty(self, sample_indicators):
        with pytest.raises(StrategyVeto, match="Empty"):
            ss.assemble_suggestion(
                suggestion_id="S-1", underlying="NIFTY",
                expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
                spot=23000.0, chain=[],
                indicators=sample_indicators,
                confidence=_all_pass_confidence(),
                iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
            )

    def test_assembles_iron_condor(self, sample_chain, sample_indicators, mocker):
        from config import STRATEGY_CONFIG
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0})
        sug = ss.assemble_suggestion(
            suggestion_id="S-1", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=sample_indicators,
            confidence=_all_pass_confidence(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
        )
        assert sug.suggestion_id == "S-1"
        assert sug.strategy == "IRON_CONDOR"
        assert sug.strategy_type == "WRITING"
        assert len(sug.legs) == 4

    def test_strategy_override_bypasses_selector(self, sample_chain, sample_indicators, mocker):
        from config import STRATEGY_CONFIG
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0})
        sug = ss.assemble_suggestion(
            suggestion_id="S-2", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=sample_indicators,
            confidence=_all_pass_confidence(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
            strategy_override="BULL_PUT_SPREAD",
        )
        assert sug.strategy == "BULL_PUT_SPREAD"

    def test_explain_renders_for_credit_strategy(self, sample_chain, sample_indicators, mocker):
        from config import STRATEGY_CONFIG
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0})
        sug = ss.assemble_suggestion(
            suggestion_id="S-3", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=sample_indicators,
            confidence=_all_pass_confidence(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
        )
        # plain_english should mention the strategy and entry section
        assert "ENTRY" in sug.plain_english
        assert "TIMELINE" in sug.plain_english
