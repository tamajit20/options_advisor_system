"""tests/test_utils.py

Unit tests for utility helpers.

Currently exercises the Phase 2c provenance helpers
(`market_state_at`, `ENGINE_VERSION`). Other helpers in `utils.py`
remain covered indirectly through their consumers.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from utils import ENGINE_VERSION, market_state_at


class TestEngineVersion:
    def test_engine_version_is_non_empty_string(self):
        assert isinstance(ENGINE_VERSION, str)
        assert ENGINE_VERSION.strip() != ""


class TestMarketStateAt:
    @pytest.mark.parametrize(
        "h,m,expected",
        [
            (0,  0, "PRE_OPEN"),
            (8, 59, "PRE_OPEN"),
            (9, 14, "PRE_OPEN"),
            (9, 15, "OPEN_VOLATILE"),
            (9, 29, "OPEN_VOLATILE"),
            (9, 30, "OPEN_STABLE"),
            (12, 0, "OPEN_STABLE"),
            (14, 59, "OPEN_STABLE"),
            (15, 0, "CLOSE_AUCTION"),
            (15, 29, "CLOSE_AUCTION"),
            (15, 30, "CLOSE_AUCTION"),
            (15, 31, "POST_CLOSE"),
            (23, 59, "POST_CLOSE"),
        ],
    )
    def test_states_across_trading_day(self, h, m, expected):
        now = datetime(2025, 6, 10, h, m, 0)
        assert market_state_at(now) == expected
