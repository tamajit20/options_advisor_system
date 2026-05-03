"""Additional coverage for database/log_repo.py — readers + DBLogHandler internals."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from database import log_repo as lr


# ---------------------------------------------------------------------------
class TestDBLogHandlerLifecycle:
    def test_start_stop_idempotent(self):
        h = lr.DBLogHandler()
        h.start()
        # Calling start again should be safe
        h.start()
        h.stop(timeout=1.0)

    def test_stop_drains_queue(self, mocker):
        h = lr.DBLogHandler(batch_size=2, flush_interval_sec=0.1)
        flushed = mocker.patch.object(h, "_flush")
        h.start()
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
        h.emit(rec)
        h.emit(rec)
        time.sleep(0.3)
        h.stop(timeout=2.0)
        # Should have called _flush at least once
        assert flushed.called


class TestDBLogHandlerFlush:
    def test_flush_handles_db_failure_gracefully(self, mocker):
        h = lr.DBLogHandler()
        bad = MagicMock()
        bad.connect = MagicMock()
        bad.executemany = MagicMock(side_effect=RuntimeError("db dead"))
        bad.rollback = MagicMock()
        bad.close = MagicMock()
        mocker.patch("database.log_repo.SQLServerConnection", return_value=bad)
        # Should NOT raise
        h._flush([{"logged_at": datetime.now(), "level": "INFO", "module": "x",
                   "job_id": None, "message": "m", "exception": None,
                   "context_json": None}])

    def test_flush_commits_on_success(self, mocker):
        h = lr.DBLogHandler()
        good = MagicMock()
        mocker.patch("database.log_repo.SQLServerConnection", return_value=good)
        h._flush([{"logged_at": datetime.now(), "level": "INFO", "module": "x",
                   "job_id": None, "message": "m", "exception": None,
                   "context_json": None}])
        good.commit.assert_called_once()


class TestInstallShutdown:
    def test_install_returns_handler_and_idempotent(self, mocker):
        # Prevent any background thread issues by no-op'ing start
        mocker.patch.object(lr.DBLogHandler, "start", lambda self: None)
        mocker.patch.object(lr.DBLogHandler, "stop", lambda self, timeout=5.0: None)
        # Reset singleton
        lr._HANDLER_SINGLETON = None
        h1 = lr.install_db_logging()
        h2 = lr.install_db_logging()
        assert h1 is h2
        lr.shutdown_db_logging()
        assert lr._HANDLER_SINGLETON is None


# ---------------------------------------------------------------------------
class TestLogRepo:
    def test_fetch_with_no_filters(self, mock_db):
        mock_db.fetch_all.return_value = [{"id": 1, "level": "INFO"}]
        repo = lr.LogRepo(mock_db)
        rows = repo.fetch()
        assert len(rows) == 1
        # SQL should not have WHERE
        sql = mock_db.fetch_all.call_args[0][0]
        assert "WHERE" not in sql.upper()

    def test_fetch_with_all_filters(self, mock_db):
        repo = lr.LogRepo(mock_db)
        repo.fetch(level="ERROR", module="engine", job_id="J-1",
                   since=datetime(2026, 5, 1), search="boom",
                   limit=50, offset=10)
        sql, params = mock_db.fetch_all.call_args[0]
        assert "level = ?" in sql
        assert "module LIKE ?" in sql
        assert "job_id = ?" in sql
        assert "logged_at >= ?" in sql
        assert "ERROR" in params
        assert "J-1" in params

    def test_counts_by_level(self, mock_db):
        mock_db.fetch_all.return_value = [
            {"level": "INFO", "n": 100},
            {"level": "WARNING", "n": 5},
        ]
        repo = lr.LogRepo(mock_db)
        result = repo.counts_by_level(since_hours=24)
        assert result == {"INFO": 100, "WARNING": 5}


# ---------------------------------------------------------------------------
class TestJobLogRepo:
    def test_start_inserts_running(self, mock_db):
        repo = lr.JobLogRepo(mock_db)
        repo.start("J-1", "fo_bhav")
        # 2 calls: DELETE then INSERT
        assert mock_db.execute.call_count == 2

    def test_finish_updates_status(self, mock_db):
        repo = lr.JobLogRepo(mock_db)
        repo.finish("J-1", "SUCCESS", rows_processed=42)
        sql = mock_db.execute.call_args[0][0]
        assert "UPDATE" in sql.upper()
        assert "SUCCESS" in mock_db.execute.call_args[0][1]

    def test_finish_truncates_long_error(self, mock_db):
        repo = lr.JobLogRepo(mock_db)
        long_err = "x" * 9000
        repo.finish("J-1", "FAILED", error_message=long_err)
        params = mock_db.execute.call_args[0][1]
        # error_message is the 4th param
        err = params[3]
        assert err is not None
        assert len(err) == 8000

    def test_latest_returns_rows(self, mock_db):
        mock_db.fetch_all.return_value = [{"job_id": "J-1"}]
        repo = lr.JobLogRepo(mock_db)
        rows = repo.latest(limit=10)
        assert rows[0]["job_id"] == "J-1"

    def test_latest_status_per_job(self, mock_db):
        mock_db.fetch_all.return_value = [{"job_name": "fo_bhav", "status": "SUCCESS"}]
        repo = lr.JobLogRepo(mock_db)
        rows = repo.latest_status_per_job()
        assert rows[0]["status"] == "SUCCESS"

    def test_last_status_returns_string(self, mock_db):
        mock_db.fetch_one.return_value = {"status": "RUNNING"}
        repo = lr.JobLogRepo(mock_db)
        assert repo.last_status("fo_bhav") == "RUNNING"

    def test_last_status_returns_none_when_empty(self, mock_db):
        mock_db.fetch_one.return_value = None
        repo = lr.JobLogRepo(mock_db)
        assert repo.last_status("fo_bhav") is None
