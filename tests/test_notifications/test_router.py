"""
tests/test_notifications/test_router.py
=======================================

Unit tests for the `Notifier` router + shipped channels (Phase 5).

We never make a real SMTP or HTTPS call — channels are exercised via stubs
(transport= or smtplib monkeypatch). The router is exercised against
in-memory fakes.
"""

from __future__ import annotations

import json
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from contracts import Notification
from notifications import (
    Channel,
    EmailChannel,
    Notifier,
    TelegramChannel,
    build_default_channels,
)
from notifications.channels import _severity_at_or_above


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _CapturingChannel:
    name = "capture"

    def __init__(self, *, accepts_all: bool = True, raise_on_send: bool = False):
        self.sent: List[Notification] = []
        self.accepted: List[Notification] = []
        self._accepts_all = accepts_all
        self._raise = raise_on_send

    def accepts(self, notif: Notification) -> bool:
        if self._accepts_all:
            self.accepted.append(notif)
            return True
        return False

    def send(self, notif: Notification) -> bool:
        if self._raise:
            raise RuntimeError("network down")
        self.sent.append(notif)
        return True


class _StubRepo:
    def __init__(self, *, raise_on_insert: bool = False) -> None:
        self.inserted: List[Notification] = []
        self._raise = raise_on_insert

    def insert(self, n: Notification) -> None:
        if self._raise:
            raise RuntimeError("DB blip")
        self.inserted.append(n)


class _FlagsStub:
    def __init__(self, **flags: bool) -> None:
        self._flags = flags

    def get_bool(self, key: str, *, default: bool = True) -> bool:
        return self._flags.get(key, default)


# ---------------------------------------------------------------------------
# Router — persistence
# ---------------------------------------------------------------------------
def test_notify_persists_to_repo():
    repo = _StubRepo()
    notif = Notifier(repo).notify("INFO_EVENT", "INFO", "hi", "body")
    assert len(repo.inserted) == 1
    assert repo.inserted[0].notif_type == "INFO_EVENT"
    assert notif.severity == "INFO"


def test_notify_continues_when_repo_insert_raises():
    repo = _StubRepo(raise_on_insert=True)
    ch = _CapturingChannel()
    Notifier(repo, [ch]).notify("X", "INFO", "hi")
    # Channel still received the event despite the DB failure
    assert len(ch.sent) == 1


# ---------------------------------------------------------------------------
# Router — channel dispatch
# ---------------------------------------------------------------------------
def test_dispatch_to_all_accepting_channels():
    a, b = _CapturingChannel(), _CapturingChannel()
    Notifier(_StubRepo(), [a, b]).notify("X", "ERROR", "boom")
    assert len(a.sent) == 1
    assert len(b.sent) == 1


def test_dispatch_skips_channel_that_rejects():
    accept = _CapturingChannel(accepts_all=True)
    reject = _CapturingChannel(accepts_all=False)
    Notifier(_StubRepo(), [accept, reject]).notify("X", "INFO", "hi")
    assert len(accept.sent) == 1
    assert reject.sent == []


def test_channel_exception_does_not_break_other_channels():
    bad = _CapturingChannel(raise_on_send=True)
    good = _CapturingChannel()
    Notifier(_StubRepo(), [bad, good]).notify("X", "ERROR", "boom")
    assert len(good.sent) == 1


# ---------------------------------------------------------------------------
# Router — runtime flag gating
# ---------------------------------------------------------------------------
def test_sl_alerts_off_suppresses_dispatch_but_persists():
    repo = _StubRepo()
    ch = _CapturingChannel()
    flags = _FlagsStub(sl_alerts=False)
    Notifier(repo, [ch], flag_repo=flags).notify("SL_TRIGGER", "ERROR", "hit")
    assert len(repo.inserted) == 1   # always persisted
    assert ch.sent == []              # but dispatch suppressed


def test_closure_alerts_off_suppresses_perfect_closure():
    ch = _CapturingChannel()
    flags = _FlagsStub(closure_alerts=False)
    Notifier(_StubRepo(), [ch], flag_repo=flags).notify(
        "PERFECT_CLOSURE", "INFO", "leg done"
    )
    assert ch.sent == []


def test_opportunity_alerts_off_suppresses_perfect_entry():
    ch = _CapturingChannel()
    flags = _FlagsStub(opportunity_alerts=False)
    Notifier(_StubRepo(), [ch], flag_repo=flags).notify(
        "PERFECT_ENTRY", "INFO", "back in band"
    )
    assert ch.sent == []


def test_unmapped_type_ignores_flags():
    ch = _CapturingChannel()
    # All categorised flags off — but JOB_FAILURE isn't gated.
    flags = _FlagsStub(sl_alerts=False, closure_alerts=False, opportunity_alerts=False)
    Notifier(_StubRepo(), [ch], flag_repo=flags).notify(
        "JOB_FAILURE", "ERROR", "scheduler"
    )
    assert len(ch.sent) == 1


def test_bypass_flags_overrides_gate():
    ch = _CapturingChannel()
    flags = _FlagsStub(sl_alerts=False)
    Notifier(_StubRepo(), [ch], flag_repo=flags).notify(
        "SL_TRIGGER", "CRITICAL", "operator override", bypass_flags=True
    )
    assert len(ch.sent) == 1


def test_flag_read_error_treated_as_allow():
    class _BrokenFlags:
        def get_bool(self, key, *, default=True):
            raise RuntimeError("flag DB down")

    ch = _CapturingChannel()
    Notifier(_StubRepo(), [ch], flag_repo=_BrokenFlags()).notify(
        "SL_TRIGGER", "ERROR", "hit"
    )
    # Fail-open: a flag DB outage must not silence alerts
    assert len(ch.sent) == 1


# ---------------------------------------------------------------------------
# Severity comparison helper
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("level,allowed,expected", [
    ("INFO",     ["CRITICAL", "ERROR"],            False),
    ("ERROR",    ["CRITICAL", "ERROR"],            True),
    ("CRITICAL", ["CRITICAL", "ERROR"],            True),
    ("WARNING",  ["WARNING", "ERROR", "CRITICAL"], True),
    ("INFO",     ["WARNING"],                      False),
    ("INFO",     [],                               False),  # empty allowed → never
    ("UNKNOWN",  ["INFO"],                         True),   # unknown → 0; floor INFO=1 → 0<1 False
])
def test_severity_at_or_above(level, allowed, expected):
    # Recompute the unknown-severity case more carefully since "UNKNOWN"=0,
    # min(allowed=INFO)=1 → 0>=1 is False. The parametrize line above for
    # UNKNOWN must therefore be False.
    if level == "UNKNOWN":
        expected = False
    assert _severity_at_or_above(level, allowed) is expected


# ---------------------------------------------------------------------------
# EmailChannel
# ---------------------------------------------------------------------------
def _email_chan(**overrides) -> EmailChannel:
    base = dict(
        host="smtp.example", port=587, user="u", password="p",
        sender="from@x.com", recipients=("to@x.com",),
        use_tls=True,
        severity_floor=("CRITICAL", "ERROR"),
    )
    base.update(overrides)
    return EmailChannel(**base)


def test_email_accepts_at_or_above_floor():
    ch = _email_chan()
    err = Notification(created_at=None, notif_type="X", severity="ERROR", title="t", body="b")  # type: ignore[arg-type]
    info = Notification(created_at=None, notif_type="X", severity="INFO", title="t", body="b")  # type: ignore[arg-type]
    assert ch.accepts(err) is True
    assert ch.accepts(info) is False


def test_email_rejects_when_misconfigured():
    ch = _email_chan(host="", recipients=())
    err = Notification(created_at=None, notif_type="X", severity="CRITICAL", title="t", body="b")  # type: ignore[arg-type]
    assert ch.accepts(err) is False


def test_email_send_uses_smtp(mocker):
    ch = _email_chan()
    smtp_class = mocker.patch("notifications.channels.smtplib.SMTP")
    instance = MagicMock()
    smtp_class.return_value.__enter__.return_value = instance
    err = Notification(created_at=None, notif_type="X", severity="ERROR", title="t", body="b")  # type: ignore[arg-type]
    ok = ch.send(err)
    assert ok is True
    smtp_class.assert_called_once_with("smtp.example", 587, timeout=10)
    instance.starttls.assert_called_once()
    instance.login.assert_called_once_with("u", "p")
    instance.sendmail.assert_called_once()


def test_email_send_swallows_errors(mocker):
    ch = _email_chan()
    smtp_class = mocker.patch("notifications.channels.smtplib.SMTP")
    smtp_class.side_effect = OSError("connection refused")
    err = Notification(created_at=None, notif_type="X", severity="CRITICAL", title="t", body="b")  # type: ignore[arg-type]
    assert ch.send(err) is False


# ---------------------------------------------------------------------------
# TelegramChannel
# ---------------------------------------------------------------------------
def _make_telegram_transport(captured: list, response: str = '{"ok": true}'):
    def _transport(url, data, timeout):
        captured.append({"url": url, "data": data, "timeout": timeout})
        return response
    return _transport


def test_telegram_accepts_warning_and_above_by_default():
    ch = TelegramChannel(bot_token="t", chat_id="123")
    warn = Notification(created_at=None, notif_type="X", severity="WARNING", title="t", body="b")  # type: ignore[arg-type]
    info = Notification(created_at=None, notif_type="X", severity="INFO", title="t", body="b")  # type: ignore[arg-type]
    assert ch.accepts(warn) is True
    assert ch.accepts(info) is False


def test_telegram_rejects_when_unconfigured():
    n = Notification(created_at=None, notif_type="X", severity="CRITICAL", title="t", body="b")  # type: ignore[arg-type]
    assert TelegramChannel(bot_token="", chat_id="123").accepts(n) is False
    assert TelegramChannel(bot_token="t", chat_id="").accepts(n) is False


def test_telegram_send_posts_with_html_parse_mode():
    captured: list = []
    ch = TelegramChannel(
        bot_token="BOT", chat_id="CHAT",
        transport=_make_telegram_transport(captured),
    )
    n = Notification(created_at=None, notif_type="SL_TRIGGER", severity="ERROR",
                     title="Stop loss hit", body="NIFTY-PUT short premium 2x")  # type: ignore[arg-type]
    assert ch.send(n) is True
    assert len(captured) == 1
    assert captured[0]["url"].endswith("/botBOT/sendMessage")
    body = captured[0]["data"].decode("utf-8")
    assert "chat_id=CHAT" in body
    assert "parse_mode=HTML" in body
    assert "Stop+loss+hit" in body or "Stop%20loss%20hit" in body


def test_telegram_returns_false_on_non_ok_response():
    ch = TelegramChannel(
        bot_token="BOT", chat_id="CHAT",
        transport=_make_telegram_transport([], response='{"ok": false, "description": "Bad chat"}'),
    )
    n = Notification(created_at=None, notif_type="X", severity="ERROR", title="t", body="b")  # type: ignore[arg-type]
    assert ch.send(n) is False


def test_telegram_returns_false_on_transport_exception():
    def _broken(url, data, timeout):
        raise OSError("DNS down")

    ch = TelegramChannel(bot_token="BOT", chat_id="CHAT", transport=_broken)
    n = Notification(created_at=None, notif_type="X", severity="CRITICAL", title="t", body="b")  # type: ignore[arg-type]
    assert ch.send(n) is False


def test_telegram_truncates_long_messages():
    captured: list = []
    ch = TelegramChannel(
        bot_token="BOT", chat_id="CHAT",
        transport=_make_telegram_transport(captured),
    )
    huge = "x" * 6000
    n = Notification(created_at=None, notif_type="X", severity="ERROR", title="t", body=huge)  # type: ignore[arg-type]
    ch.send(n)
    body = captured[0]["data"].decode("utf-8")
    # urlencoded text= field — find it and length-check
    text_param = next(p for p in body.split("&") if p.startswith("text="))
    # `text=` value cannot be longer than 4000 chars (after urldecoding)
    import urllib.parse as up
    decoded = up.unquote_plus(text_param[len("text="):])
    assert len(decoded) <= 4000


# ---------------------------------------------------------------------------
# build_default_channels factory
# ---------------------------------------------------------------------------
def test_factory_returns_empty_when_all_disabled(monkeypatch):
    from config import ALERTS_CONFIG
    monkeypatch.setitem(ALERTS_CONFIG, "email_enabled", False)
    monkeypatch.setitem(ALERTS_CONFIG, "telegram_enabled", False)
    assert build_default_channels() == []


def test_factory_includes_email_when_enabled(monkeypatch):
    from config import ALERTS_CONFIG
    monkeypatch.setitem(ALERTS_CONFIG, "email_enabled", True)
    monkeypatch.setitem(ALERTS_CONFIG, "telegram_enabled", False)
    monkeypatch.setitem(ALERTS_CONFIG, "smtp_host", "smtp.example")
    monkeypatch.setitem(ALERTS_CONFIG, "smtp_to", ["a@x.com"])
    monkeypatch.setitem(ALERTS_CONFIG, "smtp_from", "b@x.com")
    chans = build_default_channels()
    assert len(chans) == 1
    assert isinstance(chans[0], EmailChannel)


def test_factory_includes_telegram_when_enabled(monkeypatch):
    from config import ALERTS_CONFIG
    monkeypatch.setitem(ALERTS_CONFIG, "email_enabled", False)
    monkeypatch.setitem(ALERTS_CONFIG, "telegram_enabled", True)
    monkeypatch.setitem(ALERTS_CONFIG, "telegram_bot_token", "BOT")
    monkeypatch.setitem(ALERTS_CONFIG, "telegram_chat_id", "CHAT")
    chans = build_default_channels()
    assert len(chans) == 1
    assert isinstance(chans[0], TelegramChannel)
