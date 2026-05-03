"""Unit tests for engine.name_generator — trade-name formatting & collision."""
from __future__ import annotations

from datetime import date

import pytest

from engine.name_generator import make_trade_name


class TestMakeTradeName:
    def test_basic_nifty_iron_condor(self):
        name = make_trade_name(
            underlying="NIFTY", strategy="IRON_CONDOR",
            expiry=date(2026, 5, 14),  # Thursday, week 2 of May
            existing_names=[],
        )
        # 14th = ((14-1)//7)+1 = 2 → MAY2-26
        assert name == "NIFTY-CONDOR-MAY2-26"

    def test_banknifty_aliased_to_bnifty(self):
        name = make_trade_name(
            underlying="BANKNIFTY", strategy="BULL_PUT_SPREAD",
            expiry=date(2026, 6, 4),  # week 1
            existing_names=[],
        )
        assert name == "BNIFTY-BPS-JUN1-26"

    def test_finnifty_aliased(self):
        name = make_trade_name(
            underlying="FINNIFTY", strategy="BEAR_CALL_SPREAD",
            expiry=date(2026, 7, 22),  # week 4
            existing_names=[],
        )
        assert name.startswith("FNIFTY-BCS-JUL")

    def test_collision_appends_letter(self):
        base = "NIFTY-CONDOR-MAY2-26"
        name = make_trade_name(
            underlying="NIFTY", strategy="IRON_CONDOR",
            expiry=date(2026, 5, 14), existing_names={base},
        )
        assert name == f"{base}-B"

    def test_multiple_collisions_iterate(self):
        base = "NIFTY-CONDOR-MAY2-26"
        existing = {base, f"{base}-B", f"{base}-C"}
        name = make_trade_name(
            underlying="NIFTY", strategy="IRON_CONDOR",
            expiry=date(2026, 5, 14), existing_names=existing,
        )
        assert name == f"{base}-D"

    def test_unknown_strategy_falls_back_to_compact_code(self):
        name = make_trade_name(
            underlying="NIFTY", strategy="MYSTERY_BOX",
            expiry=date(2026, 5, 14), existing_names=[],
        )
        # Falls back to first 8 chars of stripped name
        assert "MYSTERYB" in name

    @pytest.mark.parametrize("day,week", [(1, 1), (7, 1), (8, 2), (14, 2),
                                           (15, 3), (21, 3), (22, 4), (28, 4),
                                           (29, 5)])
    def test_week_of_month_logic(self, day, week):
        name = make_trade_name(
            underlying="NIFTY", strategy="IRON_CONDOR",
            expiry=date(2026, 5, day), existing_names=[],
        )
        assert f"MAY{week}-26" in name
