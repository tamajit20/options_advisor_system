"""notifications — Phase 5 outbound notification router + channels.

Public API:
    Notifier               — single-call entry-point used by callers.
    build_notifier(db)     — factory wired to NotificationRepo +
                             RuntimeFlagsRepo + enabled channels.
    Channel (Protocol)     — the interface every channel implements.
    EmailChannel,
    TelegramChannel        — shipped channels.
    build_default_channels — channels currently enabled in ALERTS_CONFIG.
"""

from __future__ import annotations

from .channels import (
    Channel,
    EmailChannel,
    TelegramChannel,
    build_default_channels,
)
from .router import Notifier, build_notifier


__all__ = [
    "Notifier",
    "build_notifier",
    "Channel",
    "EmailChannel",
    "TelegramChannel",
    "build_default_channels",
]
