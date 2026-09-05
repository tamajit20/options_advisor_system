"""Tests for weekly archive + log cleanup scheduler jobs."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

import scheduler.scheduler as sched


@pytest.fixture(autouse=True)
def reset_last_status():
    sched._LAST_STATUS.clear()
    yield
    sched._LAST_STATUS.clear()


@pytest.fixture
def patched_db(mocker):
    fake = MagicMock()
    fake.connect = MagicMock(return_value=None)
    fake.close = MagicMock(return_value=None)
    fake.commit = MagicMock(return_value=None)
    fake.rollback = MagicMock(return_value=None)
    mocker.patch("scheduler.scheduler.SQLServerConnection", return_value=fake)
    mocker.patch("scheduler.scheduler.JobLogRepo", return_value=MagicMock())
    mocker.patch("scheduler.scheduler.NotificationRepo", return_value=MagicMock())
    return fake


def _capture_cleanup_fn(mocker):
    captured = {}

    def _run_job(name, fn, **kwargs):
        captured["fn"] = fn

    mocker.patch("scheduler.scheduler._run_job", side_effect=_run_job)
    return captured


class TestWeeklyLogCleanup:
    def test_deletes_system_and_job_logs_only(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 9, 5))

        log_repo = MagicMock()
        log_repo.delete_older_than.return_value = 11
        job_log_repo = MagicMock()
        job_log_repo.delete_older_than.return_value = 22
        mtm_repo = MagicMock()
        mtm_repo.archive_non_active.return_value = 3

        mocker.patch("database.log_repo.LogRepo", return_value=log_repo)
        mocker.patch("database.log_repo.JobLogRepo", return_value=job_log_repo)
        mocker.patch("database.models.TradeMtmSnapshotRepo", return_value=mtm_repo)
        job_repo = MagicMock()
        job_repo.delete_older_than.return_value = 0
        mocker.patch("database.zerodha_execution_job_repo.ZerodhaExecutionJobRepo", return_value=job_repo)
        broker_cls = mocker.patch("database.broker_order_repo.BrokerOrderRepo")

        sched.job_weekly_log_cleanup()
        cleanup = captured["fn"]
        n = cleanup(patched_db)

        assert n == 36
        assert log_repo.delete_older_than.call_count == 1
        assert job_log_repo.delete_older_than.call_count == 1
        mtm_repo.archive_non_active.assert_called_once()
        job_repo.delete_older_than.assert_called_once()
        broker_cls.assert_not_called()
        patched_db.commit.assert_called_once()

    def test_log_cleanup_retention_days(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 9, 5))

        log_repo = MagicMock()
        log_repo.delete_older_than.return_value = 0
        job_log_repo = MagicMock()
        job_log_repo.delete_older_than.return_value = 0
        mtm_repo = MagicMock()
        mtm_repo.archive_non_active.return_value = 0

        mocker.patch("database.log_repo.LogRepo", return_value=log_repo)
        mocker.patch("database.log_repo.JobLogRepo", return_value=job_log_repo)
        mocker.patch("database.models.TradeMtmSnapshotRepo", return_value=mtm_repo)

        sched.job_weekly_log_cleanup()
        captured["fn"](patched_db)

        log_cutoff = log_repo.delete_older_than.call_args[0][0]
        job_cutoff = job_log_repo.delete_older_than.call_args[0][0]
        assert log_cutoff == date(2026, 8, 29)   # 7 days
        assert job_cutoff == date(2026, 8, 29)   # 7 days


class TestWeeklyArchive:
    def test_delegates_to_archive_orchestrator(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        run_archive = mocker.patch(
            "lifecycle.archive_orchestrator.run_archive",
            return_value=42,
        )

        sched.job_weekly_archive()
        n = captured["fn"](patched_db)

        run_archive.assert_called_once_with(patched_db)
        assert n == 42
