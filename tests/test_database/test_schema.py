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
    def test_returns_19_tables(self):
        tables = sc.list_tables()
        assert len(tables) == 19
        assert all(t.startswith("options_") for t in tables)


class TestCreateAllTables:
    def test_runs_every_ddl(self):
        db = MagicMock()
        cur = MagicMock()
        db.execute = MagicMock(return_value=cur)
        sc.create_all_tables(db)
        # Should be called once per DDL statement
        assert db.execute.call_count == len(sc._TABLE_DDL)


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
