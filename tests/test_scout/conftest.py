"""Fixtures for scout API tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import dashboard.server as server


@pytest.fixture
def app(mocker):
    fake_conn = MagicMock()
    fake_conn.connect = MagicMock(return_value=None)
    fake_conn.close = MagicMock(return_value=None)
    fake_conn.fetch_one.return_value = None
    fake_conn.fetch_all.return_value = []
    mocker.patch("dashboard.server.SQLServerConnection", return_value=fake_conn)
    mocker.patch("scout.routes.SQLServerConnection", return_value=fake_conn)
    mocker.patch("database.scout_models.ScoutSignalRepo.last_signal", return_value=None)
    app = server.create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _clear_scout_sticky_quotes():
    from scout import live_quotes
    live_quotes._STICKY_QUOTES.clear()
    yield
    live_quotes._STICKY_QUOTES.clear()
