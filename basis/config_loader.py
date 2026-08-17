"""Resolve Basis Monitor settings from DB (UI) or config defaults."""

from __future__ import annotations

import logging
from typing import Optional

from basis.settings_schema import default_basis_settings, merge_basis_settings, validate_basis_settings

logger = logging.getLogger(__name__)

_SETTINGS_CACHE: Optional[dict] = None


def _load_basis_settings_from_db(db) -> dict:
    from database.basis_models import BasisConfigRepo

    repo = BasisConfigRepo(db)
    saved = repo.get_settings()
    if saved is None:
        legacy_enabled = repo.get_enabled(default=default_basis_settings()["enabled"])
        legacy_universe = repo.get_universe(default=default_basis_settings()["universe"])
        if legacy_enabled is not None or legacy_universe:
            saved = {
                **default_basis_settings(),
                "enabled": legacy_enabled,
                "universe": legacy_universe,
            }
    return merge_basis_settings(saved)


def get_basis_settings(db=None, *, use_cache: bool = True) -> dict:
    """Full Basis settings — served from in-process cache unless cache bypassed."""
    global _SETTINGS_CACHE
    if use_cache and _SETTINGS_CACHE is not None:
        return dict(_SETTINGS_CACHE)

    if db is not None:
        merged = _load_basis_settings_from_db(db)
    else:
        merged = default_basis_settings()

    _SETTINGS_CACHE = dict(merged)
    return dict(merged)


def reload_basis_settings(db) -> dict:
    """Force reload from DB and refresh the in-process cache."""
    return get_basis_settings(db, use_cache=False)


def set_basis_settings(db, settings: dict, *, updated_by: str = "ui") -> dict:
    from database.basis_models import BasisConfigRepo

    cleaned = validate_basis_settings(settings)
    repo = BasisConfigRepo(db)
    repo.set_settings(cleaned, updated_by=updated_by)
    repo.set_json(BasisConfigRepo.ENABLED_KEY, cleaned["enabled"], updated_by=updated_by)
    repo.set_json(BasisConfigRepo.UNIVERSE_KEY, cleaned["universe"], updated_by=updated_by)
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = dict(cleaned)
    return cleaned


def invalidate_settings_cache() -> None:
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None
