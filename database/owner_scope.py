"""Fail-closed owner scoping for multi-user books.

Isolation-critical queries must resolve a concrete ``owner_user_id``. Using
``None`` to mean “all users” is forbidden — admin / system “see everything”
is an explicit ``admin_all=True`` flag instead.

Prefer the acting-owner ``ContextVar`` for write/safety paths so a new call
site that forgets to pass ``owner_user_id`` still fails closed (or picks up
the bound actor) rather than scanning every book.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional

ACTING_OWNER_USER_ID: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "acting_owner_user_id", default=None,
)


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


def get_acting_owner_user_id() -> Optional[int]:
    return ACTING_OWNER_USER_ID.get()


def resolve_owner_user_id(
    owner_user_id: Optional[int] = None,
    *,
    what: str = "operation",
) -> int:
    """Explicit arg wins; otherwise the bound acting owner; else raise."""
    if owner_user_id is not None:
        return require_owner_user_id(owner_user_id, what=what)
    return require_owner_user_id(ACTING_OWNER_USER_ID.get(), what=what)


@contextmanager
def acting_owner(owner_user_id: int) -> Iterator[int]:
    """Bind the acting owner for the duration of the block."""
    uid = require_owner_user_id(owner_user_id, what="acting_owner")
    token = ACTING_OWNER_USER_ID.set(uid)
    try:
        yield uid
    finally:
        ACTING_OWNER_USER_ID.reset(token)


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
