"""Tests for database/archive_repo.py — archive moves and DDL bootstrap."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from database.archive_registry import ARCHIVE_TABLE_SPECS
from database.archive_repo import (
    archive_copy_insert_sql,
    ensure_archive_tables,
    merge_chunk_insert_sql,
    move_spec,
    run_weekly_archive,
    wrap_identity_insert,
)


def _broker_spec():
    return next(s for s in ARCHIVE_TABLE_SPECS if s.hot_table == "options_broker_orders")


def _fo_spec():
    return next(s for s in ARCHIVE_TABLE_SPECS if s.hot_table == "options_fo_eod")


def _legs_spec():
    return next(s for s in ARCHIVE_TABLE_SPECS if s.hot_table == "options_suggestion_legs")


def _broker_hot_cols():
    return [
        {"col_name": "id"},
        {"col_name": "created_at"},
        {"col_name": "trade_id"},
    ]


def _db_for_move(*, insert_rowcount=2, delete_rowcount=2, has_identity=1):
    db = MagicMock()
    ins_cur = MagicMock(rowcount=insert_rowcount)
    del_cur = MagicMock(rowcount=delete_rowcount)
    db.fetch_all.return_value = _broker_hot_cols()
    db.fetch_one.return_value = {"has_identity": has_identity}
    db.execute.side_effect = [ins_cur, del_cur]
    return db


class TestEnsureArchiveTables:
    def test_creates_broker_orders_archive_table(self):
        db = MagicMock()
        cur = MagicMock()
        db.execute.return_value = cur
        ensure_archive_tables(db)
        sqls = [call.args[0] for call in db.execute.call_args_list]
        assert any("options_broker_orders_Archive" in sql for sql in sqls)
        assert db.execute.call_count == len(ARCHIVE_TABLE_SPECS)
        assert all("COL_LENGTH" in sql and "archive_batch_id" in sql for sql in sqls)


class TestArchiveCopyInsertSql:
    def test_identity_table_uses_column_list_and_identity_insert(self):
        sql = archive_copy_insert_sql(
            hot="options_fo_eod",
            arch="options_fo_eod_Archive",
            hot_columns=["id", "trade_date", "symbol"],
            where_sql="WHERE s.trade_date < ?",
            has_identity=True,
        )
        assert "SET IDENTITY_INSERT options_fo_eod_Archive ON" in sql
        assert "INSERT INTO options_fo_eod_Archive (id, trade_date, symbol, archived_at, archive_batch_id)" in sql
        assert "SELECT s.id, s.trade_date, s.symbol, SYSDATETIME(), ?" in sql
        assert "SELECT s.*" not in sql
        assert sql.strip().endswith("SET IDENTITY_INSERT options_fo_eod_Archive OFF;")

    def test_non_identity_skips_identity_insert(self):
        sql = archive_copy_insert_sql(
            hot="options_vix_history",
            arch="options_vix_history_Archive",
            hot_columns=["trade_date", "close_price"],
            where_sql="WHERE s.trade_date < ?",
            has_identity=False,
        )
        assert "IDENTITY_INSERT" not in sql
        assert "INSERT INTO options_vix_history_Archive (trade_date, close_price, archived_at, archive_batch_id)" in sql

    def test_wrap_identity_insert_turns_off_on_failure(self):
        wrapped = wrap_identity_insert(
            "options_fo_eod_Archive", "INSERT INTO x SELECT 1", has_identity=True,
        )
        assert "BEGIN CATCH" in wrapped
        assert wrapped.count("SET IDENTITY_INSERT options_fo_eod_Archive OFF") == 2


class TestMergeChunkInsertSql:
    def test_builds_identity_safe_dynamic_sql(self):
        sql = merge_chunk_insert_sql(
            "OptionsAdvisorDB_Archive",
            "OptionsAdvisorDB_Archive_Staging",
            "options_fo_eod_Archive",
            ("trade_date", "symbol", "expiry_date", "strike", "option_type"),
        )
        assert "SET IDENTITY_INSERT [OptionsAdvisorDB_Archive].dbo.[options_fo_eod_Archive] ON" in sql
        assert "INSERT INTO [OptionsAdvisorDB_Archive].dbo.[options_fo_eod_Archive]" in sql
        assert "t.[trade_date] = s.[trade_date]" in sql
        assert "SELECT s.* FROM [OptionsAdvisorDB_Archive_Staging].dbo.[options_fo_eod_Archive] s" in sql


class TestMoveBrokerOrders:
    def test_move_uses_created_at_and_hot_archive_cutoff(self):
        db = _db_for_move()
        spec = _broker_spec()
        today = date(2026, 9, 5)
        n = move_spec(db, spec, "batch-abc", today)

        assert n == 2
        assert db.execute.call_count == 2

        ins_sql, ins_params = db.execute.call_args_list[0][0]
        assert "INSERT INTO options_broker_orders_Archive (id, created_at, trade_id, archived_at, archive_batch_id)" in ins_sql
        assert "SET IDENTITY_INSERT options_broker_orders_Archive ON" in ins_sql
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

    def test_fo_eod_move_names_identity_column(self):
        db = MagicMock()
        ins_cur = MagicMock(rowcount=4)
        del_cur = MagicMock(rowcount=4)
        db.fetch_all.return_value = [
            {"col_name": "id"},
            {"col_name": "trade_date"},
            {"col_name": "symbol"},
        ]
        db.fetch_one.return_value = {"has_identity": 1}
        db.execute.side_effect = [ins_cur, del_cur]

        n = move_spec(db, _fo_spec(), "b1", date(2026, 9, 8))
        assert n == 4
        ins_sql = db.execute.call_args_list[0][0][0]
        assert "SET IDENTITY_INSERT options_fo_eod_Archive ON" in ins_sql
        assert "INSERT INTO options_fo_eod_Archive (id, trade_date, symbol, archived_at, archive_batch_id)" in ins_sql
        assert "SELECT s.*" not in ins_sql

    def test_child_move_also_uses_identity_insert(self):
        db = MagicMock()
        ins_cur = MagicMock(rowcount=3)
        del_cur = MagicMock(rowcount=3)
        db.fetch_all.return_value = [{"col_name": "id"}, {"col_name": "suggestion_id"}]
        db.fetch_one.return_value = {"has_identity": 1}
        db.execute.side_effect = [ins_cur, del_cur]

        n = move_spec(db, _legs_spec(), "b1", date(2026, 9, 8))
        assert n == 3
        ins_sql = db.execute.call_args_list[0][0][0]
        assert "SET IDENTITY_INSERT options_suggestion_legs_Archive ON" in ins_sql
        assert "FROM options_suggestion_legs s" in ins_sql
        assert "s.suggestion_id IN" in ins_sql

    def test_move_respects_retention_config_override(self, monkeypatch):
        from config import RETENTION_CONFIG

        monkeypatch.setitem(RETENTION_CONFIG, "hot_archive_keep_days", 30)
        db = _db_for_move(insert_rowcount=1, delete_rowcount=1)

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
