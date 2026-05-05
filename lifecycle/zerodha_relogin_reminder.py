"""
lifecycle/zerodha_relogin_reminder.py
=====================================

Daily Zerodha re-login reminder. Runs at 06:00 IST Mon-Fri (well before
market open at 09:15). Kite Connect access tokens expire at 06:00 IST
every morning, so the operator must run `python main.py --zerodha-login`
each trading day to mint a fresh token before the WebSocket runner can
connect.

This job is **non-automating**: it does NOT log in for you. It only
posts a CRITICAL notification with the exact steps to follow when the
persisted session is missing or expired. The notification is gated by
the `sl_alerts` runtime flag (operator-critical bucket) but uses
`bypass_flags=True` because a missing token blocks the entire live-data
pipeline — the user must always see this.

Behavior:
- If `OPT_PROVIDERS != "zerodha"` or `OPT_ZERODHA_ENABLED=false`,
  this job is a no-op (returns 0).
- If a valid session exists, no notification is fired.
- If the session is missing or expired, one CRITICAL
  `ZERODHA_RELOGIN_REQUIRED` notification is inserted with login steps.
"""

from __future__ import annotations

import logging
from typing import Optional

from contracts import Notification
from database.connection import SQLServerConnection
from database.models import NotificationRepo
from utils import now_ist

logger = logging.getLogger(__name__)


# Body text shown to the operator. Kept here (not in config.py) because
# it's stable copy, not a tunable setting.
_LOGIN_STEPS = (
    "Zerodha Kite access token has expired (tokens reset at 06:00 IST "
    "daily). Live data + WebSocket will fail until you re-login.\n\n"
    "Steps:\n"
    "1. Run: docker compose run --rm options_advisor python main.py --zerodha-login\n"
    "2. Open the printed Kite login URL in your browser.\n"
    "3. Log in with your Zerodha credentials + TOTP and approve the app.\n"
    "4. After redirect, copy the value of `request_token=...` from the URL.\n"
    "5. Paste it at the prompt. Token will be saved to "
    "data/zerodha_session.json (valid until ~06:00 IST tomorrow).\n"
    "6. Restart the WS runner: docker compose restart ws_runner"
)


def run_zerodha_relogin_reminder(
    db: SQLServerConnection,
    *,
    config_provider: Optional[str] = None,
    config_enabled: Optional[bool] = None,
) -> int:
    """Insert a `ZERODHA_RELOGIN_REQUIRED` notification when the persisted
    Kite session is missing/expired. Returns 1 if a reminder was inserted,
    else 0.

    `config_provider` / `config_enabled` are injectable for tests. When
    None, values are read from `config.PROVIDERS_CONFIG` and
    `config.ZERODHA_API_CONFIG`.
    """
    if config_provider is None or config_enabled is None:
        from config import PROVIDERS_CONFIG, ZERODHA_API_CONFIG
        if config_provider is None:
            config_provider = (PROVIDERS_CONFIG.get("active") or "").lower()
        if config_enabled is None:
            config_enabled = bool(ZERODHA_API_CONFIG.get("enabled", True))

    if config_provider != "zerodha" or not config_enabled:
        logger.info(
            "zerodha_relogin_reminder: skipped (provider=%s enabled=%s)",
            config_provider, config_enabled,
        )
        return 0

    # Lazy import — avoids loading session module when zerodha is off.
    from providers.zerodha.session import is_token_valid, load_session
    session = load_session()
    if is_token_valid(session):
        logger.info("zerodha_relogin_reminder: session valid — no reminder needed")
        return 0

    notif = NotificationRepo(db)
    notif.insert(Notification(
        created_at=now_ist(),
        notif_type="ZERODHA_RELOGIN_REQUIRED",
        severity="CRITICAL",
        title="Zerodha re-login required for today's session",
        body=_LOGIN_STEPS,
    ))
    db.commit()
    logger.warning("zerodha_relogin_reminder: CRITICAL reminder inserted")
    return 1
