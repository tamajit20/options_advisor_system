"""Unit tests for engine.indicators — pure market context calcs."""
from __future__ import annotations

import math

import pytest

from engine.indicators import (
    adx,
    atr,
    expected_move,
    hv_20,
    max_pain,
    oi_walls,
    pcr,
    trend,
    vix_regime,
)


class TestPcr:
    def test_zero_call_oi_returns_none(self):
        rows = [{"option_type": "PE", "strike": 23000.0, "open_interest": 1000}]
        assert pcr(rows) is None

    def test_balanced_oi_returns_one(self):
        rows = [
            {"option_type": "CE", "strike": 23000.0, "open_interest": 1000},
            {"option_type": "PE", "strike": 23000.0, "open_interest": 1000},
        ]
        assert pcr(rows) == 1.0

    def test_skewed_to_puts(self):
        rows = [
            {"option_type": "CE", "strike": 23000.0, "open_interest": 100},
            {"option_type": "PE", "strike": 23000.0, "open_interest": 200},
        ]
        assert pcr(rows) == 2.0


class TestMaxPain:
    def test_empty_chain_returns_zero(self):
        assert max_pain([]) == 0.0

    def test_chain_with_oi(self):
        # Heavy OI at 23000 → max pain near 23000
        rows = []
        for s in [22900.0, 23000.0, 23100.0]:
            rows.append({"strike": s, "option_type": "CE",
                         "open_interest": 5000 if s == 23000 else 100})
            rows.append({"strike": s, "option_type": "PE",
                         "open_interest": 5000 if s == 23000 else 100})
        assert max_pain(rows) == 23000.0


class TestOiWalls:
    def test_returns_top_strikes_by_oi(self):
        rows = [
            {"strike": 23000.0, "option_type": "CE", "open_interest": 100},
            {"strike": 23200.0, "option_type": "CE", "open_interest": 9000},
            {"strike": 23100.0, "option_type": "CE", "open_interest": 5000},
            {"strike": 22900.0, "option_type": "PE", "open_interest": 8000},
            {"strike": 22800.0, "option_type": "PE", "open_interest": 4000},
        ]
        cw, pw = oi_walls(rows, top_n=2)
        assert cw == [23200.0, 23100.0]
        assert pw == [22900.0, 22800.0]


class TestAtr:
    def test_insufficient_history_returns_none(self):
        rows = [{"high_price": 23100, "low_price": 22900, "close_price": 23000}]
        assert atr(rows, period=14) is None

    def test_constant_range_gives_consistent_atr(self):
        # Each day: 200-pt range, no gap
        rows = [{"high_price": 100 + i, "low_price": -100 + i, "close_price": float(i)}
                for i in range(20)]
        result = atr(rows, period=14)
        assert result is not None and result > 0


class TestTrend:
    def test_short_history_returns_sideways(self):
        rows = [{"high_price": 23000, "low_price": 22900, "close_price": 23000}
                for _ in range(15)]
        assert trend(rows) == "SIDEWAYS"

    def test_strong_uptrend_classifies_bullish(self, spot_history_bullish):
        # 60-day clear uptrend
        assert trend(spot_history_bullish) == "BULLISH"

    def test_strong_downtrend_classifies_bearish(self, spot_history_bearish):
        assert trend(spot_history_bearish) == "BEARISH"

    def test_flat_history_classifies_sideways(self, spot_history_sideways):
        assert trend(spot_history_sideways) == "SIDEWAYS"


class TestAdx:
    def test_insufficient_history_returns_none(self):
        rows = [{"high_price": 100, "low_price": 90, "close_price": 95}] * 10
        assert adx(rows, period=14) is None

    def test_strong_uptrend_has_higher_adx_than_chop(self,
                                                    spot_history_bullish,
                                                    spot_history_sideways):
        bull_adx = adx(spot_history_bullish, period=14)
        flat_adx = adx(spot_history_sideways, period=14)
        assert bull_adx is not None and flat_adx is not None
        assert bull_adx > flat_adx


class TestVixRegime:
    def test_empty_or_single_returns_stable(self):
        assert vix_regime([]) == "STABLE"
        assert vix_regime([{"close_price": 15.0}]) == "STABLE"

    def test_small_change_returns_stable(self):
        assert vix_regime([{"close_price": 15.0}, {"close_price": 15.2}]) == "STABLE"

    def test_5pct_jump_returns_rising(self):
        assert vix_regime([{"close_price": 15.0}, {"close_price": 15.9}]) == "RISING"

    def test_10pct_jump_returns_spiking(self):
        assert vix_regime([{"close_price": 15.0}, {"close_price": 16.7}]) == "SPIKING"


class TestExpectedMove:
    def test_zero_inputs_return_zero(self):
        assert expected_move(0, 0.18, 14) == 0.0
        assert expected_move(23000, 0, 14) == 0.0
        assert expected_move(23000, 0.18, 0) == 0.0

    def test_typical_nifty_em(self):
        em = expected_move(23000, 0.18, 14)
        assert 700 < em < 900  # ~ 23000 × 0.18 × √(14/365) ≈ 810


class TestHv20:
    def test_short_history_returns_none(self):
        rows = [{"close_price": 23000.0} for _ in range(10)]
        assert hv_20(rows) is None

    def test_constant_prices_return_zero(self):
        rows = [{"close_price": 23000.0} for _ in range(30)]
        assert hv_20(rows) == pytest.approx(0.0, abs=1e-9)

    def test_known_volatility(self):
        # ~1% daily moves → annualised should be ≈ 1% × √252 ≈ 0.16
        import math
        prices = [23000.0]
        for i in range(30):
            prices.append(prices[-1] * (1.01 if i % 2 == 0 else 0.99))
        rows = [{"close_price": p} for p in prices]
        result = hv_20(rows)
        assert result is not None and 0.10 < result < 0.25
