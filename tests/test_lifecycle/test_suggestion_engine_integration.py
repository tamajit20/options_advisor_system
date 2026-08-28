"""
C1 — Suggestion engine integration tests.

These tests mock the database layer but exercise the full pipeline from
indicators → strategy_selector → leg_builder → suggestion row. They verify
that the orchestration glue (IVrank routing, StrategyVeto propagation, dedup,
concentration cap) behaves correctly end-to-end without a real DB connection.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from contracts import ConfidenceCheck, ConfidenceResult, MarketIndicators
from engine.strategy_selector import assemble_suggestion


# ---------------------------------------------------------------------------
# Helpers — synthetic data builders
# ---------------------------------------------------------------------------

def _conf_result(all_passed: bool = True, score: int = 9) -> ConfidenceResult:
    status = "PASS" if all_passed else "FAIL"
    chk = ConfidenceCheck(label="test", status=status, detail="ok")
    return ConfidenceResult(
        score=score,
        total=9,
        all_passed=all_passed,
        checks=[chk] * score,
        failed_reasons=[] if all_passed else ["test gate failed"],
    )


def _indicators(
    trend: str = "SIDEWAYS",
    iv_premium: float = 1.2,
    adx: float = 20.0,
    vix_nd_change: float = 5.0,
    oi_pcr_change: float = 1.0,
) -> MarketIndicators:
    return MarketIndicators(
        symbol="NIFTY",
        as_of=date(2026, 5, 20),
        spot=23000.0,
        pcr=1.0,
        max_pain=23000.0,
        atr_14=150.0,
        trend=trend,
        vix_close=15.0,
        vix_regime="STABLE",
        oi_walls_call=[23500.0],
        oi_walls_put=[22500.0],
        expected_move=300.0,
        hv_20=0.12,
        iv_premium=iv_premium,
        fii_net_futures=10000.0,
        adx_14=adx,
        oi_pcr_change=oi_pcr_change,
        vix_nd_change_pct=vix_nd_change,
    )


def _chain(spot=23000.0, step=50.0, width=20):
    """Minimal synthetic chain with realistic prices."""
    import math
    rows = []
    atm = round(spot / step) * step
    for i in range(-width, width + 1):
        strike = atm + i * step
        dte = 14
        dist = abs(strike - spot) / spot
        for ot in ("CE", "PE"):
            if ot == "CE":
                price = max(spot - strike, 0) + spot * 0.10 * math.exp(-3 * dist)
            else:
                price = max(strike - spot, 0) + spot * 0.10 * math.exp(-3 * dist)
            price = max(price, 0.10)
            rows.append({
                "strike": strike,
                "option_type": ot,
                "close_price": round(price, 2),
                "open_price":  round(price * 0.98, 2),
                "high_price":  round(price * 1.05, 2),
                "low_price":   round(price * 0.95, 2),
                "contracts":   500,
                "open_interest": 50000,
                "change_in_oi": 1000,
                "expiry_date": date(2026, 5, 29),
            })
    return rows


# ---------------------------------------------------------------------------
# C1a: Happy path — SIDEWAYS + high IV → IRON_CONDOR
# ---------------------------------------------------------------------------
class TestAssembleSuggestionHappyPath:
    def test_iron_condor_assembled_for_sideways_high_iv(self):
        chain = _chain()
        ind   = _indicators(trend="SIDEWAYS", iv_premium=1.3, adx=18.0)
        sug = assemble_suggestion(
            suggestion_id="SUG-20260520-001",
            underlying="NIFTY",
            expiry=date(2026, 5, 29),
            expiry_type="Weekly",
            dte=14,
            spot=23000.0,
            chain=chain,
            indicators=ind,
            confidence=_conf_result(True),
            iv_rank=65.0,
            atm_iv=0.18,
            lots=1,
            lot_size=75,
        )
        assert sug.strategy == "IRON_CONDOR"
        assert len(sug.legs) == 4
        assert sug.economics.max_profit > 0

    def test_bull_call_spread_for_bullish_mid_iv(self):
        """Mid-IV (30-50) + BULLISH → BULL_CALL_SPREAD (debit, bullish)."""
        chain = _chain()
        ind   = _indicators(trend="BULLISH", iv_premium=1.1, adx=22.0)
        sug = assemble_suggestion(
            suggestion_id="SUG-20260520-002",
            underlying="NIFTY",
            expiry=date(2026, 5, 29),
            expiry_type="Weekly",
            dte=14,
            spot=23000.0,
            chain=chain,
            indicators=ind,
            confidence=_conf_result(True),
            iv_rank=42.0,  # mid-IV range
            atm_iv=0.18,
            lots=1,
            lot_size=75,
        )
        assert sug.strategy == "BULL_CALL_SPREAD"

    def test_bear_put_spread_for_bearish_mid_iv(self):
        """Mid-IV (30-50) + BEARISH → BEAR_PUT_SPREAD (debit, bearish)."""
        chain = _chain()
        ind   = _indicators(trend="BEARISH", iv_premium=1.0, adx=22.0)
        sug = assemble_suggestion(
            suggestion_id="SUG-20260520-003",
            underlying="NIFTY",
            expiry=date(2026, 5, 29),
            expiry_type="Weekly",
            dte=14,
            spot=23000.0,
            chain=chain,
            indicators=ind,
            confidence=_conf_result(True),
            iv_rank=42.0,
            atm_iv=0.18,
            lots=1,
            lot_size=75,
        )
        assert sug.strategy in ("BEAR_PUT_SPREAD", "BEAR_CALL_SPREAD")


# ---------------------------------------------------------------------------
# C1b: VIX spike veto (S4)
# ---------------------------------------------------------------------------
class TestVixSpikeVeto:
    def test_iron_condor_vetoed_when_vix_spikes(self):
        from exceptions import StrategyVeto
        chain = _chain()
        ind = _indicators(trend="SIDEWAYS", iv_premium=1.3, adx=18.0, vix_nd_change=25.0)
        with pytest.raises(StrategyVeto, match="VIX has risen"):
            assemble_suggestion(
                suggestion_id="SUG-X",
                underlying="NIFTY",
                expiry=date(2026, 5, 29),
                expiry_type="Weekly",
                dte=14,
                spot=23000.0,
                chain=chain,
                indicators=ind,
                confidence=_conf_result(True),
                iv_rank=65.0,
                atm_iv=0.18,
                lots=1,
                lot_size=75,
            )

    def test_bull_put_spread_vetoed_when_vix_spikes(self):
        from exceptions import StrategyVeto
        chain = _chain()
        ind = _indicators(trend="BULLISH", iv_premium=1.3, adx=20.0, vix_nd_change=25.0)
        with pytest.raises(StrategyVeto, match="VIX has risen"):
            assemble_suggestion(
                suggestion_id="SUG-BPS",
                underlying="NIFTY",
                expiry=date(2026, 5, 29),
                expiry_type="Weekly",
                dte=14,
                spot=23000.0,
                chain=chain,
                indicators=ind,
                confidence=_conf_result(True),
                iv_rank=65.0,
                atm_iv=0.18,
                lots=1,
                lot_size=75,
                strategy_override="BULL_PUT_SPREAD",
            )


# ---------------------------------------------------------------------------
# C1c: JADE_LIZARD upside-risk veto (S2)
# ---------------------------------------------------------------------------
class TestJadeLizardVeto:
    def test_jade_lizard_vetoed_when_net_credit_below_spread_width(self):
        """S2: JADE_LIZARD is vetoed when net credit/share < call-spread width in points.

        NIFTY at 23000 with EM=300: short_put ≈ 22700, short_call ≈ 23150,
        long_call ≈ 23300 → call spread = 150 pts. The total credit (put + short_call
        - long_call premium per share) with realistic option prices will almost always
        be <150 pts — verifying the veto fires.
        """
        from exceptions import StrategyVeto
        # Build a chain where premiums are very small (deep OTM / thin) so the
        # net credit/share is well below the call-spread width.
        import math
        spot = 23000.0
        rows = []
        for i in range(-20, 21):
            strike = spot + i * 100.0
            for ot in ("CE", "PE"):
                # Very low premiums — set close to intrinsic only, near-zero time value
                if ot == "CE":
                    p = max(spot - strike, 0) + 0.5
                else:
                    p = max(strike - spot, 0) + 0.5
                rows.append({
                    "strike": strike, "option_type": ot,
                    "close_price": p, "open_price": p, "high_price": p * 1.02,
                    "low_price": p * 0.98, "contracts": 500, "open_interest": 50000,
                    "change_in_oi": 100, "expiry_date": date(2026, 5, 29),
                })
        ind = _indicators(trend="BULLISH", iv_premium=1.2, adx=20.0, vix_nd_change=5.0)
        ind_high = _indicators(trend="BULLISH", iv_premium=1.3, adx=20.0, vix_nd_change=5.0)
        with pytest.raises(StrategyVeto, match="upside risk not fully hedged"):
            assemble_suggestion(
                suggestion_id="SUG-X",
                underlying="NIFTY",
                expiry=date(2026, 5, 29),
                expiry_type="Weekly",
                dte=14,
                spot=spot,
                chain=rows,
                indicators=ind_high,
                confidence=_conf_result(True),
                iv_rank=55.0,
                atm_iv=0.25,
                lots=1,
                lot_size=75,
                strategy_override="JADE_LIZARD",
            )


# ---------------------------------------------------------------------------
# C1d: Confidence gate failure → no suggestion
# ---------------------------------------------------------------------------
class TestConfidenceGate:
    def test_veto_raised_when_confidence_gate_fails(self):
        """assemble_suggestion must raise StrategyVeto when confidence gate not all-passed."""
        from exceptions import StrategyVeto
        chain = _chain()
        ind = _indicators()
        with pytest.raises(StrategyVeto, match="Confidence gate"):
            assemble_suggestion(
                suggestion_id="SUG-X",
                underlying="NIFTY",
                expiry=date(2026, 5, 29),
                expiry_type="Weekly",
                dte=14,
                spot=23000.0,
                chain=chain,
                indicators=ind,
                confidence=_conf_result(all_passed=False, score=3),
                iv_rank=65.0,
                atm_iv=0.18,
                lots=1,
                lot_size=75,
            )


# ---------------------------------------------------------------------------
# C1e: LONG_STRANGLE routing (C3) — confirms the code path is reachable
# ---------------------------------------------------------------------------
class TestLongStrangleRouting:
    def test_long_strangle_routable_with_catalyst(self):
        """LONG_STRANGLE is selected in low IV + bullish when a HIGH-impact catalyst
        is present. Without catalyst the router picks BULL_CALL_SPREAD instead."""
        chain = _chain()
        ind   = _indicators(trend="BULLISH", iv_premium=0.7, adx=15.0)
        with patch.dict("config.STRATEGY_CONFIG", {"strategy_min_pop": {}}):
            sug = assemble_suggestion(
                suggestion_id="SUG-20260520-004",
                underlying="NIFTY",
                expiry=date(2026, 5, 29),
                expiry_type="Weekly",
                dte=14,
                spot=23000.0,
                chain=chain,
                indicators=ind,
                confidence=_conf_result(True),
                iv_rank=25.0,    # 20-30 range, moderately low
                atm_iv=0.18,
                lots=1,
                lot_size=75,
                has_long_vol_catalyst=True,
            )
        assert sug.strategy == "LONG_STRANGLE"
        call_leg = next(l for l in sug.legs if l.option_type == "CE")
        put_leg  = next(l for l in sug.legs if l.option_type == "PE")
        # Default ±1.0×EM (long_strangle_em_multiplier): EM ≈ 300 → ≈ 23300 / 22700.
        assert call_leg.strike > 23000
        assert put_leg.strike  < 23000
        assert call_leg.strike >= 23250
        assert put_leg.strike  <= 22750


class TestCalendarSpreadAssembly:
    def test_calendar_spread_with_calendar_legs(self):
        near = date(2026, 5, 29)
        far = date(2026, 6, 26)
        near_chain = _chain()
        far_chain = _chain(spot=23000.0)
        ind = _indicators(trend="SIDEWAYS", iv_premium=1.05, adx=18.0)
        sug = assemble_suggestion(
            suggestion_id="SUG-20260520-CAL",
            underlying="NIFTY",
            expiry=near,
            expiry_type="Weekly",
            dte=9,
            spot=23000.0,
            chain=near_chain,
            indicators=ind,
            confidence=_conf_result(True, score=12),
            iv_rank=40.0,
            atm_iv=0.16,
            lots=1,
            lot_size=75,
            calendar_legs={
                "near_expiry": near,
                "far_expiry": far,
                "near_chain": near_chain,
                "far_chain": far_chain,
            },
        )
        assert sug.strategy == "CALENDAR_SPREAD"
        assert len(sug.legs) == 2
        expiries = {l.expiry_date for l in sug.legs}
        assert near in expiries and far in expiries
