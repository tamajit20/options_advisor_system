"""
notifications/channels.py
=========================

Outbound channels for the `Notifier` router (Phase 5).

A *channel* is anything with a `send(notif: Notification) -> bool` method. The
return value is informational only; the router never raises on a channel
failure (a flaky SMTP server must not crash the trade engine).

Two channels are shipped:
    1. `EmailChannel`  — SMTP, TLS optional, multiple recipients.
    2. `TelegramChannel` — Bot API HTTPS POST to `sendMessage`.

Both are constructed from `ALERTS_CONFIG`. The factory `build_default_channels`
returns the list of channels currently enabled, which the router accepts as-is.
"""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, List, Optional, Protocol, Sequence

from contracts import Notification


logger = logging.getLogger(__name__)


# Severity ordering used to compare a channel's floor against an event.
_SEVERITY_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def _severity_at_or_above(level: str, allowed: Iterable[str]) -> bool:
    """True iff `level` ≥ the lowest entry in `allowed`. Tolerant of
    unknown severities — defaults to "always allow"."""
    allowed_levels = [_SEVERITY_ORDER.get(a.upper(), 0) for a in allowed]
    if not allowed_levels:
        return False
    floor = min(allowed_levels)
    here = _SEVERITY_ORDER.get(str(level).upper(), 0)
    return here >= floor


# ---------------------------------------------------------------------------
# Channel protocol
# ---------------------------------------------------------------------------
class Channel(Protocol):
    name: str

    def send(self, notif: Notification) -> bool: ...

    def accepts(self, notif: Notification) -> bool: ...


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
@dataclass
class EmailChannel:
    """SMTP channel. Accepts events whose severity is ≥ the floor configured
    in `ALERTS_CONFIG["email_severities"]`."""

    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: Sequence[str]
    use_tls: bool = True
    severity_floor: Sequence[str] = ("CRITICAL", "ERROR")
    name: str = "email"

    # ------------------------------------------------------------------ checks
    def accepts(self, notif: Notification) -> bool:
        if not self.host or not self.recipients or not self.sender:
            return False
        return _severity_at_or_above(notif.severity, self.severity_floor)

    # ------------------------------------------------------------------ send
    def send(self, notif: Notification) -> bool:
        if not self.accepts(notif):
            return False
        msg = MIMEMultipart("alternative")
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = f"[{notif.severity}] {notif.title}"
        msg.attach(MIMEText(notif.body or "", "plain", "utf-8"))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as srv:
                if self.use_tls:
                    srv.starttls()
                if self.user:
                    srv.login(self.user, self.password)
                srv.sendmail(self.sender, list(self.recipients), msg.as_string())
            logger.info("notifications: email sent to %d recipients (%s)",
                        len(self.recipients), notif.notif_type)
            return True
        except Exception as exc:
            logger.warning("notifications: email send failed (%s): %s",
                           notif.notif_type, exc)
            return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
@dataclass
class TelegramChannel:
    """Telegram Bot API channel. POSTs to `sendMessage` with `parse_mode=HTML`
    so we can use very light formatting (`<b>`, `<i>`, `<code>`) without
    having to MarkdownV2-escape every special character.

    `transport` is a callable matching the signature
        `(url, data: bytes, timeout: float) -> str`
    so tests can inject a fake without monkey-patching urllib globally.
    """

    bot_token: str
    chat_id: str
    severity_floor: Sequence[str] = ("WARNING", "ERROR", "CRITICAL")
    timeout_seconds: float = 5.0
    transport: Optional[object] = None  # callable; defaults to urllib
    name: str = "telegram"

    # ------------------------------------------------------------------ checks
    def accepts(self, notif: Notification) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        return _severity_at_or_above(notif.severity, self.severity_floor)

    # ------------------------------------------------------------------ send
    def send(self, notif: Notification) -> bool:
        if not self.accepts(notif):
            return False
        text = self._format(notif)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "HTML",
            # Disable link previews so price-target alerts don't pull random
            # OG-cards from any URL accidentally pasted into the body.
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        try:
            response = self._post(url, payload)
            ok = self._is_ok(response)
            if ok:
                logger.info("notifications: telegram sent (%s)", notif.notif_type)
            else:
                logger.warning(
                    "notifications: telegram returned non-ok (%s): %s",
                    notif.notif_type, response[:200],
                )
            return ok
        except Exception as exc:
            logger.warning("notifications: telegram send failed (%s): %s",
                           notif.notif_type, exc)
            return False

    # ------------------------------------------------------------------ helpers
    def _post(self, url: str, payload: bytes) -> str:
        if self.transport is not None:
            # Custom transport — used by tests.
            return self.transport(url, payload, self.timeout_seconds)  # type: ignore[misc]
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            return resp.read().decode("utf-8", errors="replace")

    @staticmethod
    def _is_ok(response_text: str) -> bool:
        try:
            data = json.loads(response_text)
        except (ValueError, TypeError):
            return False
        return bool(data.get("ok"))

    @staticmethod
    def _format(notif: Notification) -> str:
        # Keep messages compact — Telegram's hard limit is 4096 chars.
        title = (notif.title or "").strip()
        body = (notif.body or "").strip()
        sev = (notif.severity or "INFO").upper()
        head = f"<b>[{sev}] {title}</b>" if title else f"<b>[{sev}]</b>"
        out = f"{head}\n{body}" if body else head
        return out[:4000]


# ---------------------------------------------------------------------------
# Factory — read ALERTS_CONFIG, return enabled channels
# ---------------------------------------------------------------------------
def build_default_channels() -> List[Channel]:
    """Construct the channels currently enabled by `ALERTS_CONFIG`. A
    disabled channel is simply omitted from the list — the router doesn't
    need to know about it."""
    from config import ALERTS_CONFIG

    channels: List[Channel] = []

    if ALERTS_CONFIG.get("email_enabled"):
        channels.append(EmailChannel(
            host=ALERTS_CONFIG.get("smtp_host", ""),
            port=int(ALERTS_CONFIG.get("smtp_port", 587)),
            user=ALERTS_CONFIG.get("smtp_user", ""),
            password=ALERTS_CONFIG.get("smtp_password", ""),
            sender=ALERTS_CONFIG.get("smtp_from", ""),
            recipients=tuple(ALERTS_CONFIG.get("smtp_to", []) or ()),
            use_tls=bool(ALERTS_CONFIG.get("smtp_use_tls", True)),
            severity_floor=tuple(
                ALERTS_CONFIG.get("email_severities") or ("CRITICAL", "ERROR")
            ),
        ))

    if ALERTS_CONFIG.get("telegram_enabled"):
        channels.append(TelegramChannel(
            bot_token=ALERTS_CONFIG.get("telegram_bot_token", ""),
            chat_id=str(ALERTS_CONFIG.get("telegram_chat_id", "")),
            severity_floor=tuple(
                ALERTS_CONFIG.get("telegram_severities")
                or ("WARNING", "ERROR", "CRITICAL")
            ),
            timeout_seconds=float(ALERTS_CONFIG.get("telegram_timeout_seconds", 5)),
        ))

    return channels
