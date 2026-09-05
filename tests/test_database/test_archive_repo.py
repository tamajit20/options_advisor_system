"""Tests for database/archive_repo.py — archive moves and DDL bootstrap."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from database.archive_registry import ARCHIVE_TABLE_SPECS
from database.archive_repo import (
    ensure_archive_tables,
    move_spec,
    run_weekly_archive,
)


def _broker_spec():
    return next(s for s in ARCHIVE_TABLE_SPECS if s.hot_table == "options_broker_orders")


class TestEnsureArchiveTables:
    def test_creates_broker_orders_archive_table(self):
        db = MagicMock()
        cur = MagicMock()
        db.execute.return_value = cur
        ensure_archive_tables(db)
        sqls = [call.args[0] for call in db.execute.call_args_list]
        assert any("options_broker_orders_Archive" in sql for sql in sqls)
        assert db.execute.call_count == len(ARCHIVE_TABLE_SPECS)


class TestMoveBrokerOrders:
    def test_move_uses_created_at_and_hot_archive_cutoff(self):
        db = MagicMock()
        ins_cur = MagicMock(rowcount=2)
        del_cur = MagicMock(rowcount=2)
        db.execute.side_effect = [ins_cur, del_cur]

        spec = _broker_spec()
        today = date(2026, 9, 5)
        n = move_spec(db, spec, "batch-abc", today)

        assert n == 2
        assert db.execute.call_count == 2

        ins_sql, ins_params = db.execute.call_args_list[0][0]
        assert "INSERT INTO options_broker_orders_Archive" in ins_sql
        assert "FROM options_broker_orders s" in ins_sql
        assert "s.created_at < ?" in ins_sql
        assert "t.id = s.id" in ins_sql
        assert ins_params[0] == "batch-abc"
        cutoff = ins_params[1]
        assert isinstance(cutoff, datetime)
        assert cutoff == datetime(2025, 9, 5, 0, 0, 0)

        del_sql, del_params = db.execute.call_args_list[1][0]
        assert "DELETE FROM options_broker_orders" in del_sql
        assert "created_at < ?" in del_sql
        assert del_params[0] == cutoff

    def test_move_respects_retention_config_override(self, monkeypatch):
        from config import RETENTION_CONFIG

        monkeypatch.setitem(RETENTION_CONFIG, "hot_archive_keep_days", 30)
        db = MagicMock()
        ins_cur = MagicMock(rowcount=1)
        del_cur = MagicMock(rowcount=1)
        db.execute.side_effect = [ins_cur, del_cur]

        move_spec(db, _broker_spec(), "b1", date(2026, 3, 1))
        _, params = db.execute.call_args_list[0][0]
        assert params[1] == datetime(2026, 1, 30, 0, 0, 0)


class TestRunWeeklyArchive:
    def test_runs_all_specs_in_order(self, mocker):
        db = MagicMock()
        ensure = mocker.patch("database.archive_repo.ensure_archive_tables")
        move = mocker.patch("database.archive_repo.move_spec", return_value=0)
        mocker.patch("database.archive_repo.uuid.uuid4").return_value.hex = "abc123def456"

        total = run_weekly_archive(db, date(2026, 9, 5))

        ensure.assert_called_once_with(db)
        assert move.call_count == len(ARCHIVE_TABLE_SPECS)
        moved_tables = [call.args[1].hot_table for call in move.call_args_list]
        assert "options_broker_orders" in moved_tables
        assert total == 0

    def test_propagates_move_failure(self, mocker):
        db = MagicMock()
        cur = MagicMock()
        db.execute.return_value = cur
        mocker.patch(
            "database.archive_repo.move_spec",
            side_effect=RuntimeError("archive failed"),
        )
        with pytest.raises(RuntimeError, match="archive failed"):
            run_weekly_archive(db, date(2026, 9, 5))
