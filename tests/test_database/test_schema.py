"""Coverage for database/schema.py — pure helpers + create_all_tables."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from database import schema as sc


class TestNormalizeDdl:
    def test_passthrough_for_create_table(self):
        sql = "CREATE TABLE x (id INT)"
        assert sc._normalize_ddl(sql) == sql

    def test_rewrites_create_index_if_not_exists(self):
        sql = "CREATE INDEX IF NOT EXISTS IX_foo ON foo (col)"
        out = sc._normalize_ddl(sql)
        assert "IF NOT EXISTS" in out
        assert "sys.indexes" in out
        assert "IX_foo" in out


class TestListTables:
    def test_returns_expected_tables(self):
        tables = sc.list_tables()
        assert len(tables) == 28
        assert all(t.startswith("options_") for t in tables)
        assert "options_runtime_flags" in tables
        assert "options_intraday_close_snapshot" in tables
        assert "options_trade_level_events" in tables
        assert "options_broker_orders" in tables


class TestCreateAllTables:
    def test_runs_every_ddl(self, mocker):
        db = MagicMock()
        cur = MagicMock()
        db.execute = MagicMock(return_value=cur)
        scout_create = mocker.patch.object(sc, "drop_scout_tables")
        drop_removed = mocker.patch.object(sc, "drop_removed_monitor_tables")
        sc.create_all_tables(db)
        assert db.execute.call_count == len(sc._TABLE_DDL)
        scout_create.assert_called_once_with(db)
        drop_removed.assert_called_once_with(db)


class TestDropRemovedMonitorTables:
    def test_only_drops_whitelisted_arb_and_basis_tables(self):
        db = MagicMock()
        cur = MagicMock()
        db.execute = MagicMock(return_value=cur)
        sc.drop_removed_monitor_tables(db)
        sqls = [c.args[0] for c in db.execute.call_args_list]
        drop_sqls = [s for s in sqls if "DROP TABLE" in s.upper()]
        assert len(drop_sqls) == len(sc._REMOVED_MONITOR_TABLES)
        joined = "\n".join(sqls).lower()
        for name in sc._REMOVED_MONITOR_TABLES:
            assert name in joined
        assert "options_" not in "\n".join(drop_sqls).lower()
        assert "scout_" not in "\n".join(drop_sqls).lower()
        delete_sqls = [s for s in sqls if "DELETE FROM options_runtime_flags" in s]
        assert len(delete_sqls) == 1
        assert "arb_app_enabled" in delete_sqls[0]
        assert "basis_app_enabled" in delete_sqls[0]


class TestDropScoutTables:
    def test_drops_whitelisted_scout_tables_and_flag(self):
        db = MagicMock()
        cur = MagicMock()
        db.execute = MagicMock(return_value=cur)
        sc.drop_scout_tables(db)
        sqls = [c.args[0] for c in db.execute.call_args_list]
        drop_sqls = [s for s in sqls if "DROP TABLE" in s.upper()]
        assert len(drop_sqls) == len(sc._REMOVED_SCOUT_TABLES)
        joined = "\n".join(sqls).lower()
        for name in sc._REMOVED_SCOUT_TABLES:
            assert name in joined
        assert "options_" not in "\n".join(drop_sqls).lower()
        delete_sqls = [s for s in sqls if "DELETE FROM options_runtime_flags" in s]
        assert len(delete_sqls) == 1
        assert "scout_app_enabled" in delete_sqls[0]


class TestCreateDatabaseIfMissing:
    def test_skipped_when_disabled(self, mocker, monkeypatch):
        from database import schema
        monkeypatch.setitem(sc.DATABASE_CONFIG, "create_if_missing", False)
        sql_conn = mocker.patch("database.schema.SQLServerConnection")
        sc.create_database_if_missing()
        sql_conn.assert_not_called()

    def test_creates_when_missing(self, mocker, monkeypatch):
        monkeypatch.setitem(sc.DATABASE_CONFIG, "create_if_missing", True)
        master = MagicMock()
        master.connection = MagicMock()
        master.connection.autocommit = False
        cur = MagicMock()
        master.connection.cursor = MagicMock(return_value=cur)
        cur.fetchone = MagicMock(return_value=None)
        mocker.patch("database.schema.SQLServerConnection", return_value=master)
        sc.create_database_if_missing()
        # CREATE DATABASE should be called when not present
        executed = [c.args[0] for c in cur.execute.call_args_list]
        assert any("CREATE DATABASE" in s for s in executed)

    def test_skips_when_present(self, mocker, monkeypatch):
        monkeypatch.setitem(sc.DATABASE_CONFIG, "create_if_missing", True)
        master = MagicMock()
        master.connection = MagicMock()
        master.connection.autocommit = False
        cur = MagicMock()
        master.connection.cursor = MagicMock(return_value=cur)
        cur.fetchone = MagicMock(return_value=(1,))  # exists
        mocker.patch("database.schema.SQLServerConnection", return_value=master)
        sc.create_database_if_missing()
        executed = [c.args[0] for c in cur.execute.call_args_list]
        assert not any("CREATE DATABASE" in s for s in executed)
