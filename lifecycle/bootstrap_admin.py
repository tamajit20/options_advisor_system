"""Create the first admin on empty installs; optional file→DB Zerodha import."""

from __future__ import annotations

import logging
from typing import Optional

from config import DASHBOARD_CONFIG
from database.app_user_repo import AppUserRepo
from database.connection import SQLServerConnection
from database.zerodha_session_repo import ZerodhaSessionRepo

logger = logging.getLogger(__name__)


def bootstrap_admin_if_needed(db: SQLServerConnection) -> Optional[int]:
    """Create the first admin from env when app_users is empty.

    Returns the admin user id (existing or newly created), or None if
    credentials are not configured yet.
    """
    repo = AppUserRepo(db)
    existing = repo.get_admin()
    if existing:
        return int(existing["id"])
    if repo.count() > 0:
        # Users exist but none admin — promote oldest active.
        rows = repo.list_users()
        for r in rows:
            if int(r.get("is_active") or 0):
                repo.transfer_admin(int(r["id"]))
                db.commit()
                logger.warning(
                    "No admin flag set — promoted user id=%s login=%s",
                    r["id"], r.get("login"),
                )
                return int(r["id"])
        return None

    login = str(DASHBOARD_CONFIG.get("bootstrap_admin_login") or "").strip()
    password = str(DASHBOARD_CONFIG.get("bootstrap_admin_password") or "")
    if not login or not password:
        logger.info(
            "app_users empty — set OPT_BOOTSTRAP_ADMIN_LOGIN / "
            "OPT_BOOTSTRAP_ADMIN_PASSWORD then re-run to create admin"
        )
        return None
    uid = repo.create_user(
        login=login,
        password=password,
        display_name=str(DASHBOARD_CONFIG.get("bootstrap_admin_name") or login),
        is_admin=True,
        is_active=True,
    )
    db.commit()
    logger.info("Created bootstrap admin user_id=%s login=%s", uid, login.lower())
    return uid


def import_file_session_for_admin(db: SQLServerConnection, admin_user_id: int) -> bool:
    """Copy data/zerodha_session.json into zerodha_sessions for the admin if empty."""
    from providers.zerodha.session import load_session as load_file_session

    zrepo = ZerodhaSessionRepo(db)
    if zrepo.get(admin_user_id):
        return False
    sess = load_file_session()
    if sess is None or not sess.access_token:
        return False
    gen = sess.generated_at
    if getattr(gen, "tzinfo", None) is not None:
        gen = gen.replace(tzinfo=None)
    zrepo.upsert(
        admin_user_id,
        access_token=sess.access_token,
        kite_user_id=sess.user_id or "",
        api_key=sess.api_key,
        generated_at=gen,
    )
    db.commit()
    logger.info("Imported file Zerodha session into admin user_id=%s", admin_user_id)
    return True


def ensure_multi_user_bootstrap(db: SQLServerConnection) -> Optional[int]:
    """Ensure an admin exists; optionally import legacy file Zerodha session.

    Fresh installs have zero trades — new trades get ``owner_user_id`` at
    execute time from the signed-in user. No trade ownership backfill.
    """
    admin_id = bootstrap_admin_if_needed(db)
    if admin_id is None:
        return None
    import_file_session_for_admin(db, admin_id)
    return admin_id
