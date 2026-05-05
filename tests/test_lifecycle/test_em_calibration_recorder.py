"""Tests for lifecycle/em_calibration_recorder.py"""
from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock

import pytest

from lifecycle import em_calibration_recorder as rec


# ---------------------------------------------------------------------------
def _candidate(**over):
    base = {
        "suggestion_id": "SUG-1",
        "underlying": "NIFTY",
        "generated_on": date(2026, 4, 21),
        "expiry_date": date(2026, 4, 30),
        "dte": 9,
        "spot_at_generation": 25000.0,
        "data_date": date(2026, 4, 21),
    }
    base.update(over)
    return base


@pytest.fixture
def patched_repos(mocker, mock_db):
    """Patch the repo classes used by the recorder so the test controls
    every DB read/write through plain MagicMocks."""
    em_repo = MagicMock()
    iv_repo = MagicMock()
    spot_repo = MagicMock()
    mocker.patch.object(rec, "EmCalibrationRepo", return_value=em_repo)
    mocker.patch.object(rec, "IvHistoryRepo", return_value=iv_repo)
    mocker.patch.object(rec, "SpotEodRepo", return_value=spot_repo)
    return mock_db, em_repo, iv_repo, spot_repo


# ---------------------------------------------------------------------------
class TestRecordSettledExpiries:
    def test_no_candidates_returns_zero(self, patched_repos):
        db, em_repo, *_ = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = []
        assert rec.record_settled_expiries(db, date(2026, 4, 30)) == 0
        em_repo.insert_one.assert_not_called()

    def test_inserts_row_with_expected_fields(self, patched_repos):
        db, em_repo, iv_repo, spot_repo = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = [_candidate()]
        iv_repo.latest_for.return_value = [{"atm_iv": 0.16}]
        spot_repo.for_date.return_value = {"close_price": 25400.0}

        n = rec.record_settled_expiries(db, date(2026, 4, 30))

        assert n == 1
        em_repo.insert_one.assert_called_once()
        row = em_repo.insert_one.call_args.args[0]
        assert row["suggestion_id"] == "SUG-1"
        assert row["underlying"] == "NIFTY"
        assert row["dte_band"] == "8-21"  # dte=9
        assert row["spot_at_entry"] == 25000.0
        assert row["spot_at_expiry"] == 25400.0
        assert row["atm_iv_at_entry"] == pytest.approx(0.16)
        # expected = 25000 * 0.16 * sqrt(9/365)
        em_expected = 25000 * 0.16 * math.sqrt(9 / 365.0)
        assert row["expected_move"] == pytest.approx(em_expected)
        assert row["realised_move"] == pytest.approx(400.0)
        assert row["realised_ratio"] == pytest.approx(400.0 / em_expected)

    def test_skip_when_atm_iv_missing(self, patched_repos):
        db, em_repo, iv_repo, spot_repo = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = [_candidate()]
        iv_repo.latest_for.return_value = []  # no rows
        spot_repo.for_date.return_value = {"close_price": 25400.0}

        assert rec.record_settled_expiries(db, date(2026, 4, 30)) == 0
        em_repo.insert_one.assert_not_called()

    def test_skip_when_atm_iv_zero(self, patched_repos):
        db, em_repo, iv_repo, spot_repo = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = [_candidate()]
        iv_repo.latest_for.return_value = [{"atm_iv": 0.0}]
        spot_repo.for_date.return_value = {"close_price": 25400.0}

        assert rec.record_settled_expiries(db, date(2026, 4, 30)) == 0
        em_repo.insert_one.assert_not_called()

    def test_skip_when_spot_close_missing(self, patched_repos):
        db, em_repo, iv_repo, spot_repo = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = [_candidate()]
        iv_repo.latest_for.return_value = [{"atm_iv": 0.16}]
        spot_repo.for_date.return_value = None

        assert rec.record_settled_expiries(db, date(2026, 4, 30)) == 0
        em_repo.insert_one.assert_not_called()

    def test_skip_when_dte_zero(self, patched_repos):
        db, em_repo, iv_repo, spot_repo = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = [_candidate(dte=0)]
        iv_repo.latest_for.return_value = [{"atm_iv": 0.16}]
        spot_repo.for_date.return_value = {"close_price": 25400.0}

        assert rec.record_settled_expiries(db, date(2026, 4, 30)) == 0
        em_repo.insert_one.assert_not_called()

    def test_per_candidate_failure_does_not_abort_batch(self, patched_repos):
        db, em_repo, iv_repo, spot_repo = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = [
            _candidate(suggestion_id="SUG-A"),
            _candidate(suggestion_id="SUG-B"),
        ]
        iv_repo.latest_for.return_value = [{"atm_iv": 0.16}]
        spot_repo.for_date.return_value = {"close_price": 25400.0}

        # First insert raises; second should still be attempted.
        em_repo.insert_one.side_effect = [Exception("boom"), None]
        n = rec.record_settled_expiries(db, date(2026, 4, 30))

        assert n == 1
        assert em_repo.insert_one.call_count == 2

    def test_averages_multiple_atm_iv_rows(self, patched_repos):
        db, em_repo, iv_repo, spot_repo = patched_repos
        em_repo.settled_suggestions_pending_calibration.return_value = [_candidate()]
        # AVG(0.14, 0.18) = 0.16
        iv_repo.latest_for.return_value = [
            {"atm_iv": 0.14}, {"atm_iv": 0.18},
        ]
        spot_repo.for_date.return_value = {"close_price": 25400.0}

        rec.record_settled_expiries(db, date(2026, 4, 30))
        row = em_repo.insert_one.call_args.args[0]
        assert row["atm_iv_at_entry"] == pytest.approx(0.16)
