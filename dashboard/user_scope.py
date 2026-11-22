"""Helpers for multi-user ownership checks in dashboard routes."""

from __future__ import annotations

from typing import Any, Optional, Tuple

from flask import jsonify

from dashboard.auth import current_user, current_user_id, require_admin


def acting_user_id() -> Optional[int]:
    return current_user_id()


def is_admin_user() -> bool:
    u = current_user()
    return bool(u and u.get("is_admin"))


def trade_owned_or_404(trade: Optional[dict]) -> Optional[Tuple[Any, int]]:
    """Return Flask error tuple if trade missing or not owned by current user."""
    if trade is None:
        return jsonify({"error": "Not found"}), 404
    uid = acting_user_id()
    if uid is None:
        return jsonify({"error": "Unauthorized"}), 401
    owner = trade.get("owner_user_id")
    if owner is None:
        return None  # legacy unowned — allow until backfill
    if int(owner) != int(uid):
        return jsonify({"error": "Not found"}), 404
    return None


def admin_gate():
    return require_admin()
