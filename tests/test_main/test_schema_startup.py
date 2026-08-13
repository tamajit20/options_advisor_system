"""Tests for automatic schema ensure on service startup."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_ensure_schema_on_startup_runs_db_create_and_migrations():
    from main import _ensure_schema_on_startup

    db = MagicMock()
    with patch("database.schema.create_database_if_missing") as create_db, \
         patch("database.connection.SQLServerConnection", return_value=db), \
         patch("database.schema.create_all_tables") as create_tables:
        _ensure_schema_on_startup()

    create_db.assert_called_once()
    db.connect.assert_called_once()
    create_tables.assert_called_once_with(db)
    db.commit.assert_called_once()
    db.close.assert_called_once()
