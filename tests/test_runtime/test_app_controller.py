"""Tests for runtime/app_controller.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from database.runtime_flags import FLAG_OPTIONS_ADVISOR_ENABLED
from runtime.app_controller import AppRuntimeController


class _FakeComponent:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
        self.started = False


def test_app_controller_starts_and_stops_options(mocker):
    repo = MagicMock()
    repo.get_bool.side_effect = lambda key, default=False: {
        FLAG_OPTIONS_ADVISOR_ENABLED: True,
    }.get(key, default)

    options = _FakeComponent()
    ctrl = AppRuntimeController(repo, poll_interval_sec=60)
    ctrl.register_options(options)
    ctrl.apply()
    assert options.started is True

    repo.get_bool.side_effect = lambda key, default=False: {
        FLAG_OPTIONS_ADVISOR_ENABLED: False,
    }.get(key, default)
    ctrl.apply()
    assert options.started is False


def test_app_controller_skips_redundant_apply(mocker):
    repo = MagicMock()
    repo.get_bool.return_value = True
    options = _FakeComponent()
    ctrl = AppRuntimeController(repo, poll_interval_sec=60)
    ctrl.register_options(options)
    ctrl.apply()
    options.start = MagicMock(wraps=options.start)
    ctrl.apply()
    options.start.assert_not_called()
