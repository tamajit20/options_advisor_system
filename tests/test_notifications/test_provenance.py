"""
tests/test_notifications/test_provenance.py
===========================================

Phase 2c — Notifier provenance stamping.

Verifies that:
* `Notifier(provider=...)` stamps the provider tag onto every notification.
* The flag-repo's full state is captured as JSON in
  `flag_state_at_dispatch`.
* Caller-supplied `source_event_id` and `tick_age_ms` flow through.
* Failures inside the flag snapshot don't break dispatch (fail-open).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import List

from contracts import Notification
from notifications import Notifier


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
@dataclass
class _FakeRow:
    key: str
    value: object


class _StubFlagRepo:
    def __init__(self, flags=None, *, raise_on_all=False) -> None:
        self._flags = flags or {}
        self._raise_on_all = raise_on_all

    def get_bool(self, key, *, default=True):
        return bool(self._flags.get(key, default))

    def all(self):
        if self._raise_on_all:
            raise RuntimeError("flag DB down")
        return [_FakeRow(k, v) for k, v in self._flags.items()]


class _CapturingRepo:
    def __init__(self) -> None:
        self.inserted: List[Notification] = []

    def insert(self, n):
        self.inserted.append(n)


# ---------------------------------------------------------------------------
# Provider tag
# ---------------------------------------------------------------------------
def test_notifier_stamps_provider_on_every_event():
    repo = _CapturingRepo()
    n = Notifier(repo, provider="zerodha").notify("X", "INFO", "hi")
    assert n.provider == "zerodha"
    assert repo.inserted[0].provider == "zerodha"


def test_notifier_provider_defaults_to_none():
    repo = _CapturingRepo()
    n = Notifier(repo).notify("X", "INFO", "hi")
    assert n.provider is None


# ---------------------------------------------------------------------------
# flag_state_at_dispatch
# ---------------------------------------------------------------------------
def test_flag_snapshot_captured_as_json():
    repo = _CapturingRepo()
    flags = _StubFlagRepo({
        "kill_switch": False,
        "sl_alerts": True,
        "closure_alerts": True,
    })
    n = Notifier(repo, flag_repo=flags).notify("X", "INFO", "hi")
    assert n.flag_state_at_dispatch is not None
    parsed = json.loads(n.flag_state_at_dispatch)
    assert parsed == {
        "kill_switch": False,
        "sl_alerts": True,
        "closure_alerts": True,
    }


def test_flag_snapshot_none_when_no_flag_repo():
    repo = _CapturingRepo()
    n = Notifier(repo).notify("X", "INFO", "hi")
    assert n.flag_state_at_dispatch is None


def test_flag_snapshot_failure_is_swallowed():
    """A flag-DB outage must NOT prevent the notification from being saved."""
    repo = _CapturingRepo()
    flags = _StubFlagRepo({"sl_alerts": True}, raise_on_all=True)
    n = Notifier(repo, flag_repo=flags).notify("X", "INFO", "hi")
    assert n.flag_state_at_dispatch is None
    assert len(repo.inserted) == 1   # still persisted


# ---------------------------------------------------------------------------
# source_event_id + tick_age_ms pass-through
# ---------------------------------------------------------------------------
def test_caller_supplied_event_markers_flow_through():
    repo = _CapturingRepo()
    n = Notifier(repo).notify(
        "SL_TRIGGER", "CRITICAL", "hit", "body",
        source_event_id="tick-12345",
        tick_age_ms=42,
    )
    assert n.source_event_id == "tick-12345"
    assert n.tick_age_ms == 42
    saved = repo.inserted[0]
    assert saved.source_event_id == "tick-12345"
    assert saved.tick_age_ms == 42


def test_event_markers_default_to_none():
    repo = _CapturingRepo()
    n = Notifier(repo).notify("X", "INFO", "hi")
    assert n.source_event_id is None
    assert n.tick_age_ms is None
