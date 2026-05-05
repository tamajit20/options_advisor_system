"""
providers/zerodha/session.py
============================

Persistent storage for the daily Kite Connect access_token.

Kite tokens expire at **06:00 IST every day** (or earlier if the user master-logs-out
on Kite Web). We persist the token + issuance timestamp to a JSON file under
`data/zerodha_session.json` so the app survives container restarts during the
trading day without forcing a re-login.

Format:
    {
        "api_key":         "abc123",
        "access_token":    "xyz...",
        "user_id":         "AB1234",
        "generated_at":    "2026-05-04T08:55:23+05:30"   # ISO 8601 with TZ
    }

The file is written atomically (write-to-temp + rename) and chmod 0600 on
POSIX so the token is not world-readable.

Token validity rule: `is_token_valid(s, now)` returns True iff
`now < next 06:00 IST after generated_at`. We also flag tokens generated more
than 24h ago as stale regardless.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, time as _time, timedelta, timezone
from pathlib import Path
from typing import Optional

from config import PATHS


logger = logging.getLogger(__name__)


SESSION_FILENAME = "zerodha_session.json"
_IST = timezone(timedelta(hours=5, minutes=30))
_TOKEN_RESET_HOUR_IST = 6  # Kite expires tokens at 06:00 IST


@dataclass(frozen=True)
class ZerodhaSession:
    api_key: str
    access_token: str
    user_id: str
    generated_at: datetime  # tz-aware (IST)

    def to_json(self) -> str:
        return json.dumps(
            {
                "api_key": self.api_key,
                "access_token": self.access_token,
                "user_id": self.user_id,
                "generated_at": self.generated_at.isoformat(),
            },
            indent=2,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "ZerodhaSession":
        gen = d["generated_at"]
        if isinstance(gen, str):
            gen = datetime.fromisoformat(gen)
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=_IST)
        return cls(
            api_key=d["api_key"],
            access_token=d["access_token"],
            user_id=d.get("user_id", ""),
            generated_at=gen,
        )


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def _session_path() -> Path:
    data_dir = Path(PATHS.get("data_dir", "data"))
    if not data_dir.is_absolute():
        # Resolve relative to the repo root (parent of this file's package).
        data_dir = Path(__file__).resolve().parents[2] / data_dir
    return data_dir / SESSION_FILENAME


# ---------------------------------------------------------------------------
# Validity check
# ---------------------------------------------------------------------------
def _next_reset_after(generated_at: datetime) -> datetime:
    """Return the next 06:00 IST after `generated_at`."""
    g_ist = generated_at.astimezone(_IST)
    today_reset = g_ist.replace(
        hour=_TOKEN_RESET_HOUR_IST, minute=0, second=0, microsecond=0
    )
    if g_ist < today_reset:
        return today_reset
    return today_reset + timedelta(days=1)


def is_token_valid(session: Optional[ZerodhaSession], now: Optional[datetime] = None) -> bool:
    """True iff the session token is still within its daily validity window."""
    if session is None or not session.access_token:
        return False
    if now is None:
        now = datetime.now(tz=_IST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_IST)
    return now < _next_reset_after(session.generated_at)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------
def load_session(path: Optional[Path] = None) -> Optional[ZerodhaSession]:
    """Read the persisted session. Returns None if missing or unreadable."""
    p = path or _session_path()
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return ZerodhaSession.from_dict(data)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("zerodha session unreadable at %s: %s", p, exc)
        return None


def save_session(session: ZerodhaSession, path: Optional[Path] = None) -> Path:
    """Atomically write session to disk and chmod 0600 (POSIX). Returns the path."""
    p = path or _session_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".zerodha_session_", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(session.to_json())
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            # Windows / non-POSIX — best-effort only.
            pass
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def clear_session(path: Optional[Path] = None) -> bool:
    """Delete the persisted session file. Returns True if a file was removed."""
    p = path or _session_path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# OAuth exchange — shared between CLI (`main.py --zerodha-login`) and the
# Flask dashboard's /zerodha/login flow.
# ---------------------------------------------------------------------------
def build_login_url() -> str:
    """Return the Kite Connect login URL for the configured api_key."""
    from config import ZERODHA_API_CONFIG
    from kiteconnect import KiteConnect  # type: ignore[import-not-found]

    api_key = ZERODHA_API_CONFIG.get("api_key", "")
    if not api_key:
        raise RuntimeError("OPT_ZERODHA_API_KEY is not set")
    return KiteConnect(api_key=api_key).login_url()


def exchange_request_token(request_token: str) -> ZerodhaSession:
    """Exchange a Kite ``request_token`` for an access_token and persist it.

    Raises:
        RuntimeError: api credentials missing.
        ValueError:   request_token empty.
        Exception:    kiteconnect.generate_session() failure (token reused / invalid).
    """
    from config import ZERODHA_API_CONFIG
    from kiteconnect import KiteConnect  # type: ignore[import-not-found]

    api_key = ZERODHA_API_CONFIG.get("api_key", "")
    api_secret = ZERODHA_API_CONFIG.get("api_secret", "")
    if not api_key or not api_secret:
        raise RuntimeError("OPT_ZERODHA_API_KEY / OPT_ZERODHA_API_SECRET not set")

    request_token = (request_token or "").strip()
    if not request_token:
        raise ValueError("empty request_token")

    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)

    session = ZerodhaSession(
        api_key=api_key,
        access_token=data["access_token"],
        user_id=data.get("user_id", ""),
        generated_at=datetime.now(tz=_IST),
    )
    save_session(session)
    logger.info(
        "zerodha session minted: user_id=%s generated_at=%s",
        session.user_id, session.generated_at.isoformat(),
    )
    return session

