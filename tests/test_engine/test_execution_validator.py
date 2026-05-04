"""
tests/test_engine/test_execution_validator.py
=============================================

Pre-execution gate (engine/execution_validator.py).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from engine.execution_validator import validate_execution


_TODAY = date(2026, 5, 4)
_NOW   = datetime(2026, 5, 4, 9, 16)
_EXPIRY = date(2026, 5, 14)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _sug(**over) -> dict:
    base = {
        "suggestion_id": "SUG-1",
        "status": "PENDING",
        "validator_status": None,
        "entry_date": _TODAY,
        "data_as_of": _NOW - timedelta(minutes=30),
        "spot_at_generation": 23000.0,
    }
    base.update(over)
    return base


def _legs_iron_condor() -> list[dict]:
    # Spot 23000, buffer 1.5% → 345 pts. Short 23500 CE (+2.17%) and
    # 22500 PE (-2.17%) are well outside the buffer.
    return [
        {"leg_order": 1, "strike": 23500.0, "option_type": "CE", "action": "SELL"},
        {"leg_order": 2, "strike": 23600.0, "option_type": "CE", "action": "BUY"},
        {"leg_order": 3, "strike": 22500.0, "option_type": "PE", "action": "SELL"},
        {"leg_order": 4, "strike": 22400.0, "option_type": "PE", "action": "BUY"},
    ]


# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_clean_iron_condor_passes(self):
        r = validate_execution(_sug(), _legs_iron_condor(), now=_NOW, today=_TODAY)
        assert r.ok
        assert r.vetoes == []

    def test_long_only_skips_strike_distance_check(self):
        legs = [
            {"leg_order": 1, "strike": 23000.0, "option_type": "CE", "action": "BUY"},
            {"leg_order": 2, "strike": 23000.0, "option_type": "PE", "action": "BUY"},
        ]
        r = validate_execution(_sug(), legs, now=_NOW, today=_TODAY)
        assert r.ok
        assert r.details.get("strike_distance") == "no short legs"


# ---------------------------------------------------------------------------
class TestStatusCheck:
    @pytest.mark.parametrize("status", ["EXECUTED", "IGNORED", "EXPIRED"])
    def test_non_pending_is_blocked(self, status):
        r = validate_execution(_sug(status=status), _legs_iron_condor(),
                               now=_NOW, today=_TODAY)
        assert not r.ok
        assert any("not PENDING" in v for v in r.vetoes)


# ---------------------------------------------------------------------------
class TestValidatorStamp:
    def test_stale_0935_blocks(self):
        r = validate_execution(
            _sug(validator_status="STALE_0935"),
            _legs_iron_condor(), now=_NOW, today=_TODAY,
        )
        assert not r.ok
        assert any("STALE_0935" in v for v in r.vetoes)

    @pytest.mark.parametrize("vs", [None, "NOT_VALIDATED", "STILL_GOOD_0935"])
    def test_other_stamps_pass(self, vs):
        r = validate_execution(
            _sug(validator_status=vs),
            _legs_iron_condor(), now=_NOW, today=_TODAY,
        )
        assert r.ok


# ---------------------------------------------------------------------------
class TestEntryDate:
    def test_past_entry_date_blocks(self):
        r = validate_execution(
            _sug(entry_date=_TODAY - timedelta(days=2)),
            _legs_iron_condor(), now=_NOW, today=_TODAY,
        )
        assert not r.ok
        assert any("entry_date" in v and "past" in v for v in r.vetoes)

    def test_future_entry_date_passes(self):
        r = validate_execution(
            _sug(entry_date=_TODAY + timedelta(days=3)),
            _legs_iron_condor(), now=_NOW, today=_TODAY,
        )
        assert r.ok


# ---------------------------------------------------------------------------
class TestFreshness:
    def test_old_data_blocks(self):
        r = validate_execution(
            _sug(data_as_of=_NOW - timedelta(hours=10)),
            _legs_iron_condor(), now=_NOW, today=_TODAY,
        )
        assert not r.ok
        assert any("old" in v for v in r.vetoes)

    def test_missing_data_as_of_warns_not_blocks(self):
        r = validate_execution(
            _sug(data_as_of=None),
            _legs_iron_condor(), now=_NOW, today=_TODAY,
        )
        assert r.ok
        assert any("data_as_of" in w for w in r.warnings)


# ---------------------------------------------------------------------------
class TestStrikeDistance:
    def test_short_pe_too_close_to_spot_blocks(self):
        # spot=23000, buffer=1.5% → 345 pts. Short PE at 22900 only 100 pts away.
        legs = [
            {"leg_order": 1, "strike": 22900.0, "option_type": "PE", "action": "SELL"},
            {"leg_order": 2, "strike": 22800.0, "option_type": "PE", "action": "BUY"},
        ]
        r = validate_execution(_sug(), legs, now=_NOW, today=_TODAY)
        assert not r.ok
        assert any("strike too close" in v for v in r.vetoes)

    def test_short_ce_above_spot_within_buffer_blocks(self):
        legs = [
            {"leg_order": 1, "strike": 23100.0, "option_type": "CE", "action": "SELL"},
        ]
        r = validate_execution(_sug(), legs, now=_NOW, today=_TODAY)
        assert not r.ok
        assert any("strike too close" in v for v in r.vetoes)

    def test_short_pe_above_spot_blocks_wrong_side(self):
        # short PE strike > spot is structurally wrong — distance is negative
        legs = [
            {"leg_order": 1, "strike": 23500.0, "option_type": "PE", "action": "SELL"},
        ]
        r = validate_execution(_sug(), legs, now=_NOW, today=_TODAY)
        assert not r.ok
        assert any("strike too close" in v for v in r.vetoes)

    def test_short_ce_well_above_spot_passes(self):
        # 23500 vs spot 23000 → 500 pts = 2.17% > 1.5%
        legs = [
            {"leg_order": 1, "strike": 23500.0, "option_type": "CE", "action": "SELL"},
        ]
        r = validate_execution(_sug(), legs, now=_NOW, today=_TODAY)
        assert r.ok

    def test_missing_spot_warns_not_blocks(self):
        r = validate_execution(
            _sug(spot_at_generation=None),
            _legs_iron_condor(), now=_NOW, today=_TODAY,
        )
        assert r.ok
        assert any("spot_at_generation" in w for w in r.warnings)


# ---------------------------------------------------------------------------
class TestKillSwitch:
    def test_disabled_validator_short_circuits(self, mocker):
        from engine import execution_validator as ev
        mocker.patch.dict(
            ev.STRATEGY_CONFIG,
            {"execution_validator_enabled": False},
        )
        # Even a deliberately broken suggestion passes when disabled.
        bad = _sug(
            status="EXECUTED",
            validator_status="STALE_0935",
            entry_date=_TODAY - timedelta(days=10),
        )
        r = ev.validate_execution(bad, _legs_iron_condor(),
                                  now=_NOW, today=_TODAY)
        assert r.ok
        assert r.details.get("skipped") is True


# ---------------------------------------------------------------------------
class TestMultipleVetoes:
    def test_collects_all_failures(self):
        bad = _sug(
            status="IGNORED",
            validator_status="STALE_0935",
            entry_date=_TODAY - timedelta(days=1),
        )
        r = validate_execution(bad, _legs_iron_condor(),
                               now=_NOW, today=_TODAY)
        assert not r.ok
        assert len(r.vetoes) >= 3
        # `reason()` joins them with semicolons
        assert ";" in r.reason()
