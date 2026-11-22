"""Fail-closed owner scoping for multi-user books.

Isolation-critical queries must pass a concrete ``owner_user_id``. Using
``None`` to mean “all users” is forbidden — admin “see everything” is an
explicit ``admin_all=True`` flag instead.
"""

from __future__ import annotations

from typing import Optional


class OwnerScopeError(ValueError):
    """Raised when a scoped operation is missing the acting owner."""


def require_owner_user_id(
    owner_user_id: Optional[int],
    *,
    what: str = "operation",
) -> int:
    if owner_user_id is None:
        raise OwnerScopeError(f"{what} requires owner_user_id")
    try:
        uid = int(owner_user_id)
    except (TypeError, ValueError) as exc:
        raise OwnerScopeError(f"{what} requires a numeric owner_user_id") from exc
    if uid <= 0:
        raise OwnerScopeError(f"{what} requires a positive owner_user_id")
    return uid


def owner_sql(
    owner_user_id: int,
    *,
    include_null: bool = False,
    column: str = "owner_user_id",
) -> tuple[str, list]:
    """SQL fragment + params for one owner (optionally legacy NULL rows)."""
    uid = require_owner_user_id(owner_user_id, what="owner filter")
    if include_null:
        return f" AND ({column} = ? OR {column} IS NULL)", [uid]
    return f" AND {column} = ?", [uid]
