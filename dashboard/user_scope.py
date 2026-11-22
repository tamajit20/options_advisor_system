"""Helpers for multi-user ownership checks in dashboard routes."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional, Tuple

from flask import jsonify

from dashboard.auth import current_user, current_user_id, require_admin
from database.owner_scope import acting_owner, resolve_owner_user_id


def acting_user_id() -> Optional[int]:
    return current_user_id()


def is_admin_user() -> bool:
    u = current_user()
    return bool(u and u.get("is_admin"))


def require_login_or_401() -> Optional[Tuple[Any, int]]:
    if acting_user_id() is None:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def trade_owned_or_404(
    trade: Optional[dict],
    *,
    allow_unowned: bool = False,
) -> Optional[Tuple[Any, int]]:
    """Return Flask error tuple if trade missing or not owned by current user.

    Mutate / private-book routes leave ``allow_unowned=False`` (fail closed:
    a trade with no ``owner_user_id`` is treated as not found).
    """
    if trade is None:
        return jsonify({"error": "Not found"}), 404
    uid = acting_user_id()
    if uid is None:
        return jsonify({"error": "Unauthorized"}), 401
    owner = trade.get("owner_user_id")
    if owner is None:
        if allow_unowned:
            return None
        return jsonify({"error": "Not found"}), 404
    if int(owner) != int(uid):
        return jsonify({"error": "Not found"}), 404
    return None


def owned_trade_or_error(db, trade_id: str):
    """Load trade for the signed-in user. Returns ``(trade, None)`` or ``(None, err)``."""
    from database.models import TradeRepo

    trade = TradeRepo(db).get(trade_id)
    denied = trade_owned_or_404(trade)
    if denied is not None:
        return None, denied
    return trade, None


@contextmanager
def bind_acting_owner_from_session() -> Iterator[Optional[int]]:
    """Bind ``owner_scope`` from the Flask session for the request body."""
    uid = acting_user_id()
    if uid is None:
        yield None
        return
    with acting_owner(int(uid)) as owner:
        yield owner


def require_bound_owner_or_401():
    """Login check + bind acting owner. Returns ``(uid, None)`` or ``(None, err)``."""
    denied = require_login_or_401()
    if denied is not None:
        return None, denied
    return resolve_owner_user_id(acting_user_id(), what="dashboard request"), None


def admin_gate():
    return require_admin()
