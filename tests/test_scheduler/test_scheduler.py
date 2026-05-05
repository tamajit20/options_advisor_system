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


def test_multi_window_live_suggestion_jobs_registered():
    """Phase 3 — #1: extra intraday windows (09:45, 13:00, 14:30) must
    all register and dispatch to ``job_live_suggestion``."""
    keys = {
        "live_suggestion_engine_0945",
        "live_suggestion_engine_1300",
        "live_suggestion_engine_1430",
    }
    assert keys.issubset(set(sched.JOB_FUNCS.keys()))
    for k in keys:
        assert sched.JOB_FUNCS[k] is sched.job_live_suggestion


def test_event_eve_review_job_registered():
    """Phase 3 — #5: event_eve_review must be registered."""
    assert "event_eve_review" in sched.JOB_FUNCS
    assert sched.JOB_FUNCS["event_eve_review"] is sched.job_event_eve_review


def test_zerodha_relogin_reminder_job_registered():
    """Daily re-login reminder must be registered."""
    assert "zerodha_relogin_reminder" in sched.JOB_FUNCS
    assert sched.JOB_FUNCS["zerodha_relogin_reminder"] is sched.job_zerodha_relogin_reminder


# ---------------------------------------------------------------------------
# Phase 3 — #6 data-freshness gate
# ---------------------------------------------------------------------------
class TestDataFreshnessGate:
    def test_skips_when_upstream_data_missing(self, patched_db, mocker):
        # Upstream chain-status passes (SUCCESS) but data probe fails.
        sched._LAST_STATUS["fo_bhav_download"] = "SUCCESS"
        mocker.patch.object(sched, "_check_data_freshness",
                            return_value="fo_bhav_download")
        fn = MagicMock(return_value=10)
        sched._run_job("downstream_job", fn, requires=["fo_bhav_download"])
        fn.assert_not_called()
        assert sched._LAST_STATUS["downstream_job"] == "SKIPPED"

    def test_runs_when_data_probe_passes(self, patched_db, mocker):
        sched._LAST_STATUS["fo_bhav_download"] = "SUCCESS"
        mocker.patch.object(sched, "_check_data_freshness", return_value=None)
        fn = MagicMock(return_value=5)
        sched._run_job("downstream_job", fn, requires=["fo_bhav_download"])
        fn.assert_called_once()
        assert sched._LAST_STATUS["downstream_job"] == "SUCCESS"


class TestDataProbes:
    def test_probe_fo_bhav_returns_true_when_today(self, mock_db, mocker):
        from datetime import date
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 5, 4))
        mocker.patch("database.models.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 5, 4))
        assert sched._probe_fo_bhav(mock_db) is True

    def test_probe_fo_bhav_returns_false_when_stale(self, mock_db, mocker):
        from datetime import date
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 5, 4))
        mocker.patch("database.models.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 5, 3))
        assert sched._probe_fo_bhav(mock_db) is False

    def test_probe_iv_calculation_returns_true_when_today(self, mock_db, mocker):
        from datetime import date
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 5, 4))
        mocker.patch("database.models.IvHistoryRepo.latest_trade_date",
                     return_value=date(2026, 5, 4))
        assert sched._probe_iv_calculation(mock_db) is True

    def test_check_data_freshness_skips_unknown_upstreams(self, mock_db):
        # Upstreams with no registered probe should pass through quietly.
        assert sched._check_data_freshness(mock_db, ["unknown_job"]) is None

    def test_check_data_freshness_returns_first_failing(self, mock_db, mocker):
        mocker.patch.object(sched, "_probe_fo_bhav", return_value=False)
        mocker.patch.object(sched, "_probe_iv_calculation", return_value=True)
        result = sched._check_data_freshness(
            mock_db, ["fo_bhav_download", "iv_calculation"],
        )
        assert result == "fo_bhav_download"

    def test_check_data_freshness_treats_probe_exception_as_stale(self, mock_db, mocker):
        mocker.patch.object(sched, "_probe_fo_bhav",
                            side_effect=RuntimeError("DB down"))
        result = sched._check_data_freshness(mock_db, ["fo_bhav_download"])
        assert result == "fo_bhav_download"
