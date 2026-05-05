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
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0, "strategy_min_credit_to_width_ratio": {}})
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
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0, "strategy_min_credit_to_width_ratio": {}})
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
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0, "strategy_min_credit_to_width_ratio": {}})
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


# ---------------------------------------------------------------------------
# Per-strategy IV/HV ceiling (mirrors iv_butterfly_min_prem pattern).
# Verifies that yesterday's IRON_BUTTERFLY gating is untouched and that the
# new naked-long cap fires only for the listed strategies.
# ---------------------------------------------------------------------------
class TestPerStrategyIvPremiumCap:
    def _ind(self, iv_premium: float, trend: str = "SIDEWAYS"):
        return _make_indicators(trend=trend, iv_premium=iv_premium)

    def test_long_straddle_vetoed_when_iv_premium_above_cap(self,
                                                            sample_chain):
        # Buying regime (iv_rank=15) + iv_premium=1.30 > 1.20 cap → veto
        ind = self._ind(iv_premium=1.30, trend="SIDEWAYS")
        with pytest.raises(StrategyVeto, match="strategy_iv_premium_buy_max"):
            ss.assemble_suggestion(
                suggestion_id="S-LS-1", underlying="NIFTY",
                expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
                spot=23000.0, chain=sample_chain,
                indicators=ind,
                confidence=_all_pass_confidence(),
                iv_rank=15.0, atm_iv=0.18, lots=1, lot_size=75,
                strategy_override="LONG_STRADDLE",
            )

    def test_long_straddle_passes_when_iv_premium_within_cap(self,
                                                              sample_chain,
                                                              mocker):
        # Buying regime + iv_premium=1.10 ≤ 1.20 cap → no veto from old cap.
        # Patch out the new per-strategy buy_pass map so this test continues to
        # exercise ONLY the legacy `strategy_iv_premium_buy_max` gate.
        from config import STRATEGY_CONFIG
        mocker.patch.dict(STRATEGY_CONFIG, {"strategy_iv_premium_buy_pass": {}})
        ind = self._ind(iv_premium=1.10, trend="SIDEWAYS")
        sug = ss.assemble_suggestion(
            suggestion_id="S-LS-2", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=15.0, atm_iv=0.18, lots=1, lot_size=75,
            strategy_override="LONG_STRADDLE",
        )
        assert sug.strategy == "LONG_STRADDLE"

    def test_long_call_vetoed_above_cap(self, sample_chain):
        ind = self._ind(iv_premium=1.30, trend="BULLISH")
        with pytest.raises(StrategyVeto, match="strategy_iv_premium_buy_max"):
            ss.assemble_suggestion(
                suggestion_id="S-LC-1", underlying="NIFTY",
                expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
                spot=23000.0, chain=sample_chain,
                indicators=ind,
                confidence=_all_pass_confidence(),
                iv_rank=15.0, atm_iv=0.18, lots=1, lot_size=75,
                strategy_override="LONG_CALL",
            )

    def test_iron_condor_unaffected_by_naked_long_cap(self, sample_chain,
                                                      mocker):
        """IRON_CONDOR is NOT in strategy_iv_premium_buy_max → no per-strategy
        veto. Yesterday's behaviour preserved."""
        from config import STRATEGY_CONFIG
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0, "strategy_min_credit_to_width_ratio": {}})
        # iv_premium=1.30 (above naked-long cap), in writing regime → unaffected
        ind = self._ind(iv_premium=1.30, trend="SIDEWAYS")
        sug = ss.assemble_suggestion(
            suggestion_id="S-IC-1", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
        )
        assert sug.strategy == "IRON_CONDOR"

    def test_bull_call_spread_unaffected(self, sample_chain, mocker):
        """Debit verticals are NOT in the per-strategy cap map."""
        from config import STRATEGY_CONFIG
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0, "strategy_min_credit_to_width_ratio": {}})
        ind = self._ind(iv_premium=1.40, trend="BULLISH")
        sug = ss.assemble_suggestion(
            suggestion_id="S-BCS-1", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=40.0, atm_iv=0.18, lots=1, lot_size=75,
            strategy_override="BULL_CALL_SPREAD",
        )
        assert sug.strategy == "BULL_CALL_SPREAD"

    def test_writing_regime_skips_naked_long_cap(self, sample_chain):
        """Cap applies only in BUYING regime (iv_rank < buying_max).
        High iv_rank → strategy override allowed even with high iv_premium."""
        ind = self._ind(iv_premium=1.50, trend="SIDEWAYS")
        # iv_rank=60 → writing regime; per-strategy cap should not fire
        # even though we override to LONG_STRADDLE (artificial scenario)
        sug = ss.assemble_suggestion(
            suggestion_id="S-LS-W", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
            strategy_override="LONG_STRADDLE",
        )
        assert sug.strategy == "LONG_STRADDLE"

    def test_butterfly_gate_still_works(self):
        """Yesterday's iv_butterfly_min_prem is independent of new map."""
        # iv_rank > 70 + iv_premium=1.50 → IRON_BUTTERFLY (yesterday's path)
        ind = self._ind(iv_premium=1.50, trend="SIDEWAYS")
        assert ss.select_strategy(iv_rank=80.0, trend="SIDEWAYS",
                                   indicators=ind) == "IRON_BUTTERFLY"
        # iv_rank > 70 but iv_premium=1.20 < 1.40 → fall back to IC
        ind2 = self._ind(iv_premium=1.20, trend="SIDEWAYS")
        assert ss.select_strategy(iv_rank=80.0, trend="SIDEWAYS",
                                   indicators=ind2) == "IRON_CONDOR"


# ---------------------------------------------------------------------------
# Per-strategy buy_pass soft veto (review item #8 follow-up).
# `strategy_iv_premium_buy_pass` was previously edge_score-only; promoted to
# a soft veto when iv_premium > buy_pass × (1 + tolerance) in the buying regime.
# ---------------------------------------------------------------------------
class TestPerStrategyBuyPassVeto:
    def _ind(self, iv_premium: float, trend: str = "SIDEWAYS"):
        return _make_indicators(trend=trend, iv_premium=iv_premium)

    def test_long_straddle_vetoed_above_buy_pass_threshold(self, sample_chain):
        # buy_pass=0.85, tolerance=0.15 → ceiling = 0.978. iv_premium=1.05 > ceiling.
        ind = self._ind(iv_premium=1.05, trend="SIDEWAYS")
        with pytest.raises(StrategyVeto, match="buy_pass threshold"):
            ss.assemble_suggestion(
                suggestion_id="S-BP-1", underlying="NIFTY",
                expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
                spot=23000.0, chain=sample_chain,
                indicators=ind,
                confidence=_all_pass_confidence(),
                iv_rank=15.0, atm_iv=0.18, lots=1, lot_size=75,
                strategy_override="LONG_STRADDLE",
            )

    def test_long_straddle_passes_at_buy_pass_threshold(self, sample_chain):
        # iv_premium=0.95 < 0.978 ceiling → no veto.
        ind = self._ind(iv_premium=0.95, trend="SIDEWAYS")
        sug = ss.assemble_suggestion(
            suggestion_id="S-BP-2", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=15.0, atm_iv=0.18, lots=1, lot_size=75,
            strategy_override="LONG_STRADDLE",
        )
        assert sug.strategy == "LONG_STRADDLE"

    def test_iron_condor_unaffected_by_buy_pass(self, sample_chain, mocker):
        """IRON_CONDOR is NOT in strategy_iv_premium_buy_pass → veto skipped.
        Strategy isolation: tightening buy_pass for naked longs does not bleed
        into credit strategies."""
        from config import STRATEGY_CONFIG
        mocker.patch.dict(STRATEGY_CONFIG, {"min_credit_to_width_ratio": 0.0,
                                            "strategy_min_credit_to_width_ratio": {}})
        ind = self._ind(iv_premium=1.30, trend="SIDEWAYS")
        sug = ss.assemble_suggestion(
            suggestion_id="S-BP-IC", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
        )
        assert sug.strategy == "IRON_CONDOR"

    def test_writing_regime_skips_buy_pass_veto(self, sample_chain):
        """buy_pass veto only fires in buying regime (iv_rank < buying_max)."""
        ind = self._ind(iv_premium=1.50, trend="SIDEWAYS")
        sug = ss.assemble_suggestion(
            suggestion_id="S-BP-W", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,  # writing regime
            strategy_override="LONG_STRADDLE",
        )
        assert sug.strategy == "LONG_STRADDLE"

    def test_per_strategy_thresholds_isolated(self, sample_chain, mocker):
        """LONG_CALL has buy_pass=0.90, LONG_STRADDLE 0.85 — at iv_premium=1.00,
        ceiling for LONG_CALL is 1.035 (passes), for LONG_STRADDLE is 0.978
        (vetoes). Tightening one strategy's threshold cannot affect another."""
        ind = self._ind(iv_premium=1.00, trend="BULLISH")
        # LONG_CALL passes — below its 1.035 ceiling.
        sug = ss.assemble_suggestion(
            suggestion_id="S-BP-LC", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=sample_chain,
            indicators=ind,
            confidence=_all_pass_confidence(),
            iv_rank=15.0, atm_iv=0.18, lots=1, lot_size=75,
            strategy_override="LONG_CALL",
        )
        assert sug.strategy == "LONG_CALL"
        # LONG_STRADDLE vetoes — above its 0.978 ceiling.
        with pytest.raises(StrategyVeto, match="buy_pass threshold"):
            ss.assemble_suggestion(
                suggestion_id="S-BP-LS", underlying="NIFTY",
                expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
                spot=23000.0, chain=sample_chain,
                indicators=self._ind(iv_premium=1.00, trend="SIDEWAYS"),
                confidence=_all_pass_confidence(),
                iv_rank=15.0, atm_iv=0.18, lots=1, lot_size=75,
                strategy_override="LONG_STRADDLE",
            )


# ---------------------------------------------------------------------------
# Future-scope stub — review item #10 expected-move calibration validator.
# ---------------------------------------------------------------------------
@pytest.mark.future
@pytest.mark.skip(reason="future: realised-move vs expected-move calibration validator (FUTURE_ENHANCEMENT_SCOPES.md → Data Quality)")
def test_expected_move_calibration_warning_when_realised_exceeds_expected():
    """After 4+ expiries, if realised |close-close| moves consistently exceed
    the expected_move envelope by >25% for a given (underlying, dte_band),
    the suggestion should carry a calibration warning chip and optionally
    apply a per-underlying expected_move multiplier. Will require a new
    `options_em_calibration` table populated at expiry settlement."""
    pass
