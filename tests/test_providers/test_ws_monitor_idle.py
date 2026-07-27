"""Tests for idle ws_status snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from providers.ws_monitor import write_idle_snapshot


def test_write_idle_snapshot(tmp_path: Path):
    path = tmp_path / "ws_status.json"
    write_idle_snapshot(path, detail="waiting for login")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["connection_state"] == "waiting_for_login"
    assert data["runner_state"] == "waiting_for_login"
    assert data["last_error"] == "waiting for login"
    assert data["subscribed_tokens"] == 0
