"""Tests for batched log-retention deletes."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from database.retention import (
    DELETE_BATCH_SIZE,
    delete_older_than,
    ensure_log_delete_indexes,
)


def _cur(n: int) -> MagicMock:
    c = MagicMock()
    c.rowcount = n
    return c


class TestDeleteOlderThan:
    def test_single_batch_when_rowcount_below_size(self):
        db = MagicMock()
        db.execute.return_value = _cur(42)
        n = delete_older_than(
            db, "options_system_logs", "logged_at", date(2026, 6, 1),
            batch_size=5000,
        )
        assert n == 42
        assert db.execute.call_count == 1
        sql, params = db.execute.call_args[0]
        assert "DELETE TOP (5000) FROM options_system_logs" in sql
        assert "logged_at < ?" in sql
        assert params == [datetime(2026, 6, 1)]
        db.commit.assert_called_once()

    def test_second_batch_then_stop(self):
        db = MagicMock()
        db.execute.side_effect = [_cur(100), _cur(42)]
        n = delete_older_than(
            db, "options_system_logs", "logged_at", date(2026, 6, 1),
            batch_size=100,
        )
        assert n == 142
        assert db.execute.call_count == 2
        assert db.commit.call_count == 2

    def test_empty_table_commits_once_and_stops(self):
        db = MagicMock()
        db.execute.return_value = _cur(0)
        n = delete_older_than(
            db, "options_job_log", "started_at", date(2026, 6, 1),
            batch_size=5000,
        )
        assert n == 0
        assert db.execute.call_count == 1

    def test_unknown_rowcount_does_not_loop(self):
        db = MagicMock()
        db.execute.return_value = _cur(-1)
        n = delete_older_than(
            db, "options_system_logs", "logged_at", date(2026, 6, 1),
        )
        assert n == 0
        assert db.execute.call_count == 1

    def test_rejects_unsafe_identifier(self):
        db = MagicMock()
        with pytest.raises(ValueError, match="unsafe"):
            delete_older_than(db, "logs; DROP", "logged_at", date(2026, 6, 1))

    def test_default_batch_size(self):
        assert DELETE_BATCH_SIZE == 5000


class TestEnsureLogDeleteIndexes:
    def test_creates_missing_logged_at_index(self):
        db = MagicMock()
        db.execute.return_value = MagicMock()
        ensure_log_delete_indexes(db)
        sqls = [c.args[0] for c in db.execute.call_args_list]
        joined = "\n".join(sqls)
        assert "IX_options_system_logs_logged_at" in joined
        assert "IX_options_notifications_created" in joined
        assert "IX_options_zerodha_jobs_created" in joined
        assert db.commit.call_count == 3

    def test_continues_after_index_error(self):
        db = MagicMock()
        db.execute.side_effect = [RuntimeError("lock"), MagicMock(), MagicMock()]
        ensure_log_delete_indexes(db)
        assert db.execute.call_count == 3
        db.rollback.assert_called()
