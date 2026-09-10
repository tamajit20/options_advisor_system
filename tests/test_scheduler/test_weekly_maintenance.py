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


def _patch_log_cleanup_repos(mocker, *, logs=0, jobs=0, notifs=0, mtm=0, zjobs=0):
    mocker.patch("database.retention.ensure_log_delete_indexes")
    log_repo = MagicMock()
    log_repo.delete_older_than.return_value = logs
    job_log_repo = MagicMock()
    job_log_repo.delete_older_than.return_value = jobs
    mtm_repo = MagicMock()
    mtm_repo.archive_non_active.return_value = mtm
    notif_repo = MagicMock()
    notif_repo.delete_older_than.return_value = notifs
    job_repo = MagicMock()
    job_repo.delete_older_than.return_value = zjobs
    mocker.patch("database.log_repo.LogRepo", return_value=log_repo)
    mocker.patch("database.log_repo.JobLogRepo", return_value=job_log_repo)
    mocker.patch("database.models.TradeMtmSnapshotRepo", return_value=mtm_repo)
    mocker.patch("database.models.NotificationRepo", return_value=notif_repo)
    mocker.patch(
        "database.zerodha_execution_job_repo.ZerodhaExecutionJobRepo",
        return_value=job_repo,
    )
    return log_repo, job_log_repo, notif_repo, mtm_repo, job_repo


class TestWeeklyLogCleanup:
    @pytest.fixture(autouse=True)
    def _shrink_log(self, mocker):
        self.shrink = mocker.patch(
            "lifecycle.sql_backup.shrink_transaction_log_quietly",
            return_value=128,
        )

    def test_deletes_system_and_job_logs_only(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 9, 5))
        log_repo, job_log_repo, notif_repo, mtm_repo, job_repo = (
            _patch_log_cleanup_repos(
                mocker, logs=11, jobs=22, notifs=5, mtm=3, zjobs=0,
            )
        )
        broker_cls = mocker.patch("database.broker_order_repo.BrokerOrderRepo")

        sched.job_weekly_log_cleanup()
        cleanup = captured["fn"]
        n = cleanup(patched_db)

        assert n == 41
        assert log_repo.delete_older_than.call_count == 1
        assert job_log_repo.delete_older_than.call_count == 1
        notif_repo.delete_older_than.assert_called_once()
        mtm_repo.archive_non_active.assert_called_once()
        job_repo.delete_older_than.assert_called_once()
        broker_cls.assert_not_called()
        patched_db.commit.assert_called_once()
        self.shrink.assert_called_once_with(patched_db)

    def test_log_cleanup_retention_days(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 9, 5))
        log_repo, job_log_repo, notif_repo, _, job_repo = _patch_log_cleanup_repos(
            mocker,
        )

        sched.job_weekly_log_cleanup()
        captured["fn"](patched_db)

        expected = date(2026, 8, 29)  # delete_keep_days = 7
        assert log_repo.delete_older_than.call_args[0][0] == expected
        assert job_log_repo.delete_older_than.call_args[0][0] == expected
        assert notif_repo.delete_older_than.call_args[0][0] == expected
        assert job_repo.delete_older_than.call_args[0][0] == expected

    def test_raises_odbc_timeout_then_restores(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 9, 5))
        patched_db.connection.timeout = 60
        _patch_log_cleanup_repos(mocker)
        seen = {}

        def _ensure(db):
            seen["timeout"] = db.connection.timeout

        mocker.patch("database.retention.ensure_log_delete_indexes", side_effect=_ensure)

        sched.job_weekly_log_cleanup()
        captured["fn"](patched_db)

        assert seen["timeout"] == 1800
        assert patched_db.connection.timeout == 60

    def test_still_succeeds_if_log_shrink_is_a_noop(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        mocker.patch("scheduler.scheduler.today_ist", return_value=date(2026, 9, 5))
        self.shrink.return_value = 0
        _patch_log_cleanup_repos(mocker, logs=2, jobs=3, notifs=1, mtm=0, zjobs=0)

        sched.job_weekly_log_cleanup()
        n = captured["fn"](patched_db)
        assert n == 6
        self.shrink.assert_called_once_with(patched_db)


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


class TestWeeklyCleanupRetired:
    def test_inner_fn_raises_instead_of_deleting(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        fo = mocker.patch("database.models.FoEodRepo")
        sched.job_weekly_cleanup()
        with pytest.raises(RuntimeError, match="weekly_cleanup is retired"):
            captured["fn"](patched_db)
        fo.assert_not_called()


class TestDbBackup:
    def test_delegates_to_sql_backup(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        run_hot = mocker.patch(
            "lifecycle.sql_backup.run_hot_backup",
            return_value="/app/backups/OptionsAdvisorDB-1.bak",
        )

        sched.job_db_backup()
        n = captured["fn"](patched_db)

        run_hot.assert_called_once_with(patched_db)
        assert n == 1


class TestArchiveExportJob:
    def test_delegates_to_run_archive_export_and_commits(self, patched_db, mocker):
        captured = _capture_cleanup_fn(mocker)
        run_export = mocker.patch(
            "lifecycle.archive_export.run_archive_export",
            return_value=1,
        )

        sched.job_archive_export()
        n = captured["fn"](patched_db)

        run_export.assert_called_once_with(patched_db)
        patched_db.commit.assert_called_once()
        assert n == 1


class TestArchiveOrchestrator:
    def test_copies_commits_then_shrinks(self, mocker):
        db = MagicMock()
        mocker.patch(
            "lifecycle.archive_orchestrator.today_ist",
            return_value=date(2026, 9, 11),
        )
        weekly = mocker.patch(
            "lifecycle.archive_orchestrator.run_weekly_archive",
            return_value=7,
        )
        shrink = mocker.patch(
            "lifecycle.sql_backup.shrink_transaction_log_quietly",
            return_value=128,
        )
        from lifecycle.archive_orchestrator import run_archive

        n = run_archive(db)
        assert n == 7
        weekly.assert_called_once_with(db, date(2026, 9, 11))
        db.commit.assert_called_once()
        shrink.assert_called_once_with(db)
        assert weekly.call_count == 1
        assert db.commit.call_count == 1
