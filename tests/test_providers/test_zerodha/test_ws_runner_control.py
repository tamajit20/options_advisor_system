"""Tests for ws_runner Docker wake helpers."""

from __future__ import annotations

import json

from providers.zerodha import ws_runner_control as ctl


def test_ensure_ws_runner_running_when_already_running(mocker):
    mocker.patch.object(ctl, "_container_state", return_value="running")
    mocker.patch.object(ctl, "_docker_api")

    out = ctl.ensure_ws_runner_running()

    assert out == {"ok": True, "action": "already_running", "state": "running"}
    ctl._docker_api.assert_not_called()


def test_ensure_ws_runner_starts_exited_container(mocker):
    mocker.patch.object(ctl, "_container_state", return_value="exited")
    mocker.patch.object(ctl, "_docker_api", return_value=(True, ""))

    out = ctl.ensure_ws_runner_running()

    assert out["ok"] is True
    assert out["action"] == "started"
    assert out["previous_state"] == "exited"
    ctl._docker_api.assert_called_once_with("POST", "/containers/stock_ws_runner/start")


def test_ensure_ws_runner_no_docker_socket(mocker):
    mocker.patch.object(ctl, "_container_state", return_value=None)

    out = ctl.ensure_ws_runner_running()

    assert out["ok"] is False
    assert out["action"] == "none"


def test_container_state_parses_docker_json(mocker):
    body = json.dumps({"State": {"Status": "running"}})
    mocker.patch.object(ctl, "_docker_api", return_value=(True, body))

    assert ctl._container_state("stock_ws_runner") == "running"
