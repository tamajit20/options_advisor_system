"""Tests for database/connection.py — driver pick, connection string, retry, context manager."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import database.connection as conn_mod
from database.connection import SQLServerConnection, _pick_driver


class TestPickDriver:
    def test_prefers_odbc18(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["SQL Server", "ODBC Driver 17 for SQL Server",
                                   "ODBC Driver 18 for SQL Server"])
        assert _pick_driver() == "ODBC Driver 18 for SQL Server"

    def test_falls_back_to_odbc17(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 17 for SQL Server", "SQL Server"])
        assert _pick_driver() == "ODBC Driver 17 for SQL Server"

    def test_returns_first_preference_when_none_installed(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers", return_value=[])
        # falls through to first preferred (will fail at connect time)
        assert _pick_driver() == "ODBC Driver 18 for SQL Server"


class TestConnectionString:
    def test_sql_auth_includes_uid_pwd(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        c = SQLServerConnection(server="srv", database="db",
                                username="u", password="p")
        cs = c._build_connection_string()
        assert "UID=u" in cs and "PWD=p" in cs
        assert "Trusted_Connection" not in cs

    def test_windows_auth_when_no_username(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        c = SQLServerConnection(server="srv", database="db",
                                username="", password="")
        cs = c._build_connection_string()
        assert "Trusted_Connection=yes" in cs
        assert "UID=" not in cs

    def test_override_database(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        c = SQLServerConnection(server="s", database="orig", username="", password="")
        cs = c._build_connection_string(override_database="other")
        assert "DATABASE=other" in cs

    def test_includes_trust_server_certificate(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        c = SQLServerConnection(server="s", database="d", username="u", password="p")
        assert "TrustServerCertificate=yes" in c._build_connection_string()


class TestLifecycle:
    def test_connect_calls_pyodbc(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        fake_conn = MagicMock()
        m = mocker.patch("database.connection.pyodbc.connect", return_value=fake_conn)
        c = SQLServerConnection("s", "d", "u", "p")
        c.connect()
        assert m.called
        assert c.connection is fake_conn

    def test_close_clears_connection(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        fake = MagicMock()
        mocker.patch("database.connection.pyodbc.connect", return_value=fake)
        c = SQLServerConnection("s", "d", "u", "p")
        c.connect()
        c.close()
        assert c.connection is None
        fake.close.assert_called_once()

    def test_close_handles_already_dead_connection(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        fake = MagicMock()
        fake.close.side_effect = Exception("already closed")
        mocker.patch("database.connection.pyodbc.connect", return_value=fake)
        c = SQLServerConnection("s", "d", "u", "p")
        c.connect()
        c.close()  # must not raise
        assert c.connection is None


class TestTransactionControl:
    def test_rollback_swallows_dead_connection_error(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        fake = MagicMock()
        fake.rollback.side_effect = Exception("dead")
        mocker.patch("database.connection.pyodbc.connect", return_value=fake)
        c = SQLServerConnection("s", "d", "u", "p")
        c.connect()
        c.rollback()  # must not raise

    def test_commit_re_raises(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        fake = MagicMock()
        fake.commit.side_effect = RuntimeError("boom")
        mocker.patch("database.connection.pyodbc.connect", return_value=fake)
        c = SQLServerConnection("s", "d", "u", "p")
        c.connect()
        with pytest.raises(RuntimeError):
            c.commit()


class TestContextManager:
    def test_commits_on_clean_exit(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        fake = MagicMock()
        mocker.patch("database.connection.pyodbc.connect", return_value=fake)
        with SQLServerConnection("s", "d", "u", "p") as c:
            assert c.connection is fake
        fake.commit.assert_called_once()
        fake.close.assert_called_once()

    def test_rolls_back_on_exception(self, mocker):
        mocker.patch("database.connection.pyodbc.drivers",
                     return_value=["ODBC Driver 18 for SQL Server"])
        fake = MagicMock()
        mocker.patch("database.connection.pyodbc.connect", return_value=fake)
        with pytest.raises(ValueError):
            with SQLServerConnection("s", "d", "u", "p"):
                raise ValueError("test")
        fake.rollback.assert_called_once()
        fake.commit.assert_not_called()


class TestWithRetry:
    def test_succeeds_on_first_try(self):
        fn = MagicMock(return_value=42)
        out = SQLServerConnection.with_retry(fn, "x", attempts=3)
        assert out == 42
        assert fn.call_count == 1

    def test_retries_on_operational_error(self, mocker):
        mocker.patch("database.connection.time.sleep", return_value=None)
        import pyodbc
        fn = MagicMock(side_effect=[pyodbc.OperationalError("transient"),
                                    pyodbc.OperationalError("transient"),
                                    "ok"])
        out = SQLServerConnection.with_retry(fn, attempts=3, backoff_seconds=0.0)
        assert out == "ok"
        assert fn.call_count == 3

    def test_raises_after_exhausting_attempts(self, mocker):
        mocker.patch("database.connection.time.sleep", return_value=None)
        import pyodbc
        fn = MagicMock(side_effect=pyodbc.OperationalError("nope"))
        with pytest.raises(pyodbc.OperationalError):
            SQLServerConnection.with_retry(fn, attempts=2, backoff_seconds=0.0)
        assert fn.call_count == 2
