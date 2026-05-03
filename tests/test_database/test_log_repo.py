"""Tests for database/log_repo.py — DB log handler buffering + flushing."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from database.log_repo import DBLogHandler


def _make_record(level: int = logging.INFO, msg: str = "test") -> logging.LogRecord:
    return logging.LogRecord(
        name="test.module", level=level, pathname=__file__, lineno=1,
        msg=msg, args=None, exc_info=None,
    )


class TestDbLogHandler:
    def test_emit_queues_record(self):
        h = DBLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        h.emit(_make_record(msg="hello"))
        assert h._queue.qsize() == 1
        item = h._queue.get_nowait()
        assert item["message"] == "hello"
        assert item["level"] == "INFO"
        assert item["module"] == "test.module"

    def test_emit_swallows_exception(self):
        """emit must NEVER propagate exceptions back to the caller."""
        h = DBLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        # break the queue so put_nowait fails
        h._queue = MagicMock()
        h._queue.put_nowait.side_effect = RuntimeError("queue dead")
        # Should not raise
        h.emit(_make_record())

    def test_record_to_row_includes_exception_text(self):
        h = DBLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            rec = logging.LogRecord(
                name="t", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="oops", args=None, exc_info=sys.exc_info(),
            )
        row = h._record_to_row(rec)
        assert "ValueError" in row["exception"]
        assert "boom" in row["exception"]

    def test_record_to_row_serialises_ctx(self):
        h = DBLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        rec = _make_record()
        rec.ctx = {"job_id": "J1", "n": 5}
        row = h._record_to_row(rec)
        assert "J1" in row["context_json"]
        assert "n" in row["context_json"]

    def test_record_to_row_includes_job_id_when_set(self):
        h = DBLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        rec = _make_record()
        rec.job_id = "JOB-42"
        row = h._record_to_row(rec)
        assert row["job_id"] == "JOB-42"

    def test_message_truncated_to_8000(self):
        h = DBLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        rec = _make_record(msg="x" * 10000)
        row = h._record_to_row(rec)
        assert len(row["message"]) == 8000

    def test_flush_failure_does_not_raise(self, mocker):
        """If the DB connection fails during flush, the handler must catch it."""
        h = DBLogHandler()
        # Force the SQLServerConnection used internally to blow up
        fake_conn = MagicMock()
        fake_conn.connect.side_effect = Exception("DB unreachable")
        mocker.patch("database.log_repo.SQLServerConnection", return_value=fake_conn)
        # Should not propagate
        h._flush([{"logged_at": None, "level": "INFO", "module": "t",
                   "job_id": None, "message": "m", "exception": None,
                   "context_json": None}])
