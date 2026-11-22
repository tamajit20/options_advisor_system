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


def token_valid_until(session: Optional[ZerodhaSession]) -> Optional[datetime]:
    """Next 06:00 IST boundary after which the persisted token expires."""
    if session is None or not session.access_token:
        return None
    return _next_reset_after(session.generated_at)


def is_token_valid(session: Optional[ZerodhaSession], now: Optional[datetime] = None) -> bool:
    """True iff the session token is still within its daily validity window."""
    if session is None or not session.access_token:
        return False
    if now is None:
        now = datetime.now(tz=_IST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_IST)
    until = token_valid_until(session)
    return until is not None and now < until


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------
def _session_from_db_row(row: dict) -> Optional[ZerodhaSession]:
    if not row or not row.get("access_token"):
        return None
    gen = row.get("generated_at")
    if isinstance(gen, str):
        gen = datetime.fromisoformat(gen)
    if gen is None:
        return None
    if getattr(gen, "tzinfo", None) is None:
        gen = gen.replace(tzinfo=_IST)
    from config import ZERODHA_API_CONFIG
    api_key = str(row.get("api_key") or ZERODHA_API_CONFIG.get("api_key") or "")
    return ZerodhaSession(
        api_key=api_key,
        access_token=str(row["access_token"]),
        user_id=str(row.get("kite_user_id") or ""),
        generated_at=gen,
    )


def load_session_from_file(path: Optional[Path] = None) -> Optional[ZerodhaSession]:
    """Read the legacy shared file session."""
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


def load_session_for_user(app_user_id: int) -> Optional[ZerodhaSession]:
    """Load Zerodha token for a specific app user from SQL."""
    try:
        from database.connection import SQLServerConnection
        from database.zerodha_session_repo import ZerodhaSessionRepo
        db = SQLServerConnection()
        db.connect()
        try:
            row = ZerodhaSessionRepo(db).get(int(app_user_id))
            return _session_from_db_row(row or {})
        finally:
            db.close()
    except Exception:
        logger.debug("load_session_for_user(%s) failed", app_user_id, exc_info=True)
        return None


def load_admin_session() -> Optional[ZerodhaSession]:
    """Zerodha session for the sole admin (feed / ws_runner)."""
    try:
        from database.connection import SQLServerConnection
        from database.app_user_repo import AppUserRepo
        db = SQLServerConnection()
        db.connect()
        try:
            admin = AppUserRepo(db).get_admin()
            if admin:
                sess = load_session_for_user(int(admin["id"]))
                if sess is not None:
                    return sess
        finally:
            db.close()
    except Exception:
        logger.debug("load_admin_session failed", exc_info=True)
    # Transition: fall back to shared file until admin row is imported.
    return load_session_from_file()


def load_session(path: Optional[Path] = None) -> Optional[ZerodhaSession]:
    """Default session load — admin/feed preferred, then legacy file.

    Trading paths that need a specific user should call
    ``load_session_for_user`` instead.
    """
    if path is not None:
        return load_session_from_file(path)
    return load_admin_session()


def _restrict_session_file_permissions(path: Path) -> None:
    """Best-effort restrict session file to current user (POSIX chmod, Windows ACL)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        import subprocess
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.getlogin()}:F"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


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
        _restrict_session_file_permissions(p)
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
    removed = False
    try:
        p.unlink()
        removed = True
    except FileNotFoundError:
        removed = False
    # Drop process-local Kite instrument / quote caches so the next login
    # cannot reuse a master built under the old access_token.
    try:
        from providers.zerodha.leg_quotes import invalidate_caches
        invalidate_caches()
    except Exception:
        try:
            from providers.zerodha.instrument_cache import (
                invalidate_shared_instrument_master,
            )
            invalidate_shared_instrument_master()
        except Exception:
            pass
    return removed


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


def save_session_for_user(app_user_id: int, session: ZerodhaSession) -> None:
    """Persist token to SQL for *app_user_id*; also write file if user is admin."""
    from database.connection import SQLServerConnection
    from database.app_user_repo import AppUserRepo
    from database.zerodha_session_repo import ZerodhaSessionRepo

    gen = session.generated_at
    gen_naive = gen.replace(tzinfo=None) if getattr(gen, "tzinfo", None) else gen
    db = SQLServerConnection()
    db.connect()
    try:
        ZerodhaSessionRepo(db).upsert(
            int(app_user_id),
            access_token=session.access_token,
            kite_user_id=session.user_id or "",
            api_key=session.api_key,
            generated_at=gen_naive,
        )
        admin = AppUserRepo(db).get_admin()
        is_admin = bool(admin and int(admin["id"]) == int(app_user_id))
        db.commit()
    finally:
        db.close()
    if is_admin:
        save_session(session)


def exchange_request_token(
    request_token: str,
    *,
    app_user_id: Optional[int] = None,
) -> ZerodhaSession:
    """Exchange a Kite ``request_token`` for an access_token and persist it.

    When *app_user_id* is set, store under ``zerodha_sessions`` for that user
    (and mirror to file if they are admin). Otherwise write the legacy file only.
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
    if app_user_id is not None:
        save_session_for_user(int(app_user_id), session)
    else:
        save_session(session)
    logger.info(
        "zerodha session minted: kite_user=%s app_user_id=%s generated_at=%s",
        session.user_id, app_user_id, session.generated_at.isoformat(),
    )
    try:
        from providers.zerodha.ws_runner_control import notify_session_updated
        notify_session_updated()
    except Exception:
        logger.exception("ws_runner wake after login failed (non-fatal)")
    return session

