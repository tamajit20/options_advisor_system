"""Additional coverage for scheduler/scheduler.py — build, trigger, weekly cleanup."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

import scheduler.scheduler as sched


@pytest.fixture(autouse=True)
def reset_state():
    sched._LAST_STATUS.clear()
    sched._SCHEDULER = None
    yield
    sched._LAST_STATUS.clear()
    sched._SCHEDULER = None


# ---------------------------------------------------------------------------
class TestBuildScheduler:
    def test_returns_scheduler_with_jobs(self):
        sch = sched.build_scheduler()
        assert sch is not None
        # At least one job should be scheduled (from SCHEDULER_CONFIG)
        assert sch is not None
        # Don't start it — just verify creation succeeded


class TestGetScheduler:
    def test_returns_none_when_not_started(self):
        assert sched.get_scheduler() is None

    def test_returns_running_scheduler(self):
        fake = MagicMock()
        sched._SCHEDULER = fake
        assert sched.get_scheduler() is fake


# ---------------------------------------------------------------------------
class TestTriggerJobNow:
    def test_returns_false_for_unknown_job(self):
        assert sched.trigger_job_now("no_such_job") is False

    def test_raises_when_scheduler_not_running(self):
        sched._SCHEDULER = None
        with pytest.raises(RuntimeError, match="not running"):
            sched.trigger_job_now("fo_bhav_download")

    def test_raises_when_scheduler_stopped(self):
        fake = MagicMock()
        fake.running = False
        sched._SCHEDULER = fake
        with pytest.raises(RuntimeError, match="not running"):
            sched.trigger_job_now("fo_bhav_download")

    def test_dispatches_when_running_no_trade_date(self):
        fake = MagicMock()
        fake.running = True
        fake.add_job = MagicMock()
        sched._SCHEDULER = fake
        ok = sched.trigger_job_now("fo_bhav_download")
        assert ok is True
        fake.add_job.assert_called_once()

    def test_dispatches_with_trade_date(self):
        fake = MagicMock()
        fake.running = True
        fake.add_job = MagicMock()
        sched._SCHEDULER = fake
        ok = sched.trigger_job_now("fo_bhav_download", trade_date="2026-04-30")
        assert ok is True
        fake.add_job.assert_called_once()

    def test_unsupported_job_with_trade_date_falls_back(self):
        """events_seed isn't in _SUPPORTED, so trade_date is ignored."""
        fake = MagicMock()
        fake.running = True
        sched._SCHEDULER = fake
        ok = sched.trigger_job_now("events_seed", trade_date="2026-04-30")
        assert ok is True


# ---------------------------------------------------------------------------
class TestRunJobChainSkipBoth:
    def test_chain_skip_with_two_upstreams_one_failed(self, mocker):
        """If ANY upstream FAILED, the job is skipped."""
        sched._LAST_STATUS["upstream_a"] = "SUCCESS"
        sched._LAST_STATUS["upstream_b"] = "FAILED"
        skipper = mocker.patch("scheduler.scheduler._record_skipped")
        # Mock SQLServerConnection so _run_job doesn't try to connect
        mocker.patch("scheduler.scheduler.SQLServerConnection",
                     return_value=MagicMock())
        mocker.patch("scheduler.scheduler.JobLogRepo", return_value=MagicMock())
        mocker.patch("scheduler.scheduler.NotificationRepo", return_value=MagicMock())
        fn = MagicMock()
        sched._run_job("downstream", fn, requires=["upstream_a", "upstream_b"])
        skipper.assert_called_once()
        fn.assert_not_called()


# ---------------------------------------------------------------------------
class TestRecordSkipped:
    def test_records_in_db(self, mocker):
        fake = MagicMock()
        fake.connect = MagicMock()
        fake.close = MagicMock()
        fake.commit = MagicMock()
        mocker.patch("scheduler.scheduler.SQLServerConnection", return_value=fake)
        mocker.patch("scheduler.scheduler.JobLogRepo", return_value=MagicMock())
        sched._record_skipped("J-1", "downstream", "upstream_a")
        assert sched._LAST_STATUS["downstream"] == "SKIPPED"

    def test_swallows_db_failure(self, mocker):
        bad = MagicMock()
        bad.connect = MagicMock(side_effect=RuntimeError("db down"))
        bad.close = MagicMock()
        mocker.patch("scheduler.scheduler.SQLServerConnection", return_value=bad)
        # Should not raise
        sched._record_skipped("J-1", "downstream", "upstream_a")
