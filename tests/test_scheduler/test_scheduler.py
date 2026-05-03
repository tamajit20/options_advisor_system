"""Tests for scheduler/scheduler.py — chain-skip logic + job wrapper behaviour."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import scheduler.scheduler as sched


@pytest.fixture(autouse=True)
def reset_last_status():
    """Each test starts with a clean job-state tracker."""
    sched._LAST_STATUS.clear()
    yield
    sched._LAST_STATUS.clear()


@pytest.fixture
def patched_db(mocker):
    """Patch SQLServerConnection so _run_job doesn't open a real connection."""
    fake = MagicMock()
    fake.connect = MagicMock(return_value=None)
    fake.close = MagicMock(return_value=None)
    fake.commit = MagicMock(return_value=None)
    fake.rollback = MagicMock(return_value=None)
    mocker.patch("scheduler.scheduler.SQLServerConnection", return_value=fake)
    # Patch repos used inside _run_job
    mocker.patch("scheduler.scheduler.JobLogRepo", return_value=MagicMock())
    mocker.patch("scheduler.scheduler.NotificationRepo", return_value=MagicMock())
    return fake


class TestMakeJobId:
    def test_format(self, mocker):
        from datetime import date
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 5, 4))
        assert sched._make_job_id("fo_bhav") == "fo_bhav-20260504"


class TestRunJob:
    def test_marks_success_when_fn_returns_normally(self, patched_db):
        fn = MagicMock(return_value=42)
        sched._run_job("test_job", fn)
        assert sched._LAST_STATUS["test_job"] == "SUCCESS"
        fn.assert_called_once()

    def test_marks_failed_on_exception(self, patched_db):
        fn = MagicMock(side_effect=RuntimeError("boom"))
        sched._run_job("bad_job", fn)
        assert sched._LAST_STATUS["bad_job"] == "FAILED"

    def test_skips_when_upstream_failed(self, patched_db, mocker):
        sched._LAST_STATUS["upstream_job"] = "FAILED"
        skipper = mocker.patch("scheduler.scheduler._record_skipped")
        fn = MagicMock()
        sched._run_job("downstream_job", fn, requires=["upstream_job"])
        skipper.assert_called_once()
        fn.assert_not_called()

    def test_runs_when_upstream_succeeded(self, patched_db):
        sched._LAST_STATUS["upstream_job"] = "SUCCESS"
        fn = MagicMock(return_value=10)
        sched._run_job("downstream_job", fn, requires=["upstream_job"])
        fn.assert_called_once()
        assert sched._LAST_STATUS["downstream_job"] == "SUCCESS"

    def test_skips_when_upstream_critical(self, patched_db, mocker):
        sched._LAST_STATUS["upstream_job"] = "CRITICAL"
        skipper = mocker.patch("scheduler.scheduler._record_skipped")
        fn = MagicMock()
        sched._run_job("downstream_job", fn, requires=["upstream_job"])
        skipper.assert_called_once()
        fn.assert_not_called()

    def test_runs_when_upstream_unknown(self, patched_db):
        """If the upstream job hasn't run yet (no entry in _LAST_STATUS),
        we should still run — chain-skip only kicks in on an explicit FAIL."""
        fn = MagicMock(return_value=0)
        sched._run_job("downstream_job", fn, requires=["never_ran"])
        fn.assert_called_once()


class TestJobFuncsRegistry:
    def test_all_jobs_registered(self):
        expected = {
            "fo_bhav_download", "spot_bhav_download", "vix_download", "fii_download",
            "iv_calculation", "suggestion_engine", "simulation_update", "exit_engine",
            "events_seed", "weekly_cleanup",
        }
        assert expected.issubset(set(sched.JOB_FUNCS.keys()))

    def test_all_registered_jobs_are_callable(self):
        for name, fn in sched.JOB_FUNCS.items():
            assert callable(fn), f"{name} is not callable"
