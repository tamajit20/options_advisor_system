"""Tests for runtime/app_controller.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from database.runtime_flags import (
    FLAG_ARB_APP_ENABLED,
    FLAG_OPTIONS_ADVISOR_ENABLED,
    FLAG_SCOUT_APP_ENABLED,
)
from runtime.app_controller import AppRuntimeController


class _FakeComponent:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.started = False


def test_app_controller_starts_and_stops_scout(mocker):
    repo = MagicMock()
    repo.get_bool.side_effect = lambda key, default=False: {
        FLAG_OPTIONS_ADVISOR_ENABLED: True,
        FLAG_SCOUT_APP_ENABLED: True,
        FLAG_ARB_APP_ENABLED: True,
    }.get(key, default)

    scout = _FakeComponent()
    ctrl = AppRuntimeController(repo, poll_interval_sec=60)
    ctrl.register_scout(scout)
    ctrl.apply()
    assert scout.started is True

    repo.get_bool.side_effect = lambda key, default=False: {
        FLAG_OPTIONS_ADVISOR_ENABLED: True,
        FLAG_SCOUT_APP_ENABLED: False,
        FLAG_ARB_APP_ENABLED: True,
    }.get(key, default)
    ctrl.apply()
    assert scout.started is False


def test_app_controller_skips_redundant_apply(mocker):
    repo = MagicMock()
    repo.get_bool.return_value = True
    scout = _FakeComponent()
    ctrl = AppRuntimeController(repo, poll_interval_sec=60)
    ctrl.register_scout(scout)
    ctrl.apply()
    scout.start = MagicMock(wraps=scout.start)
    ctrl.apply()
    scout.start.assert_not_called()
