"""Resolve Arb Monitor settings from DB (UI) or config defaults."""

from __future__ import annotations

import logging
from typing import Optional

from arb.settings_schema import default_arb_settings, merge_arb_settings, validate_arb_settings

logger = logging.getLogger(__name__)

_SETTINGS_CACHE: Optional[dict] = None


def _load_arb_settings_from_db(db) -> dict:
    from database.arb_models import ArbConfigRepo

    repo = ArbConfigRepo(db)
    saved = repo.get_settings()
    if saved is None:
        legacy_enabled = repo.get_enabled(default=default_arb_settings()["enabled"])
        legacy_universe = repo.get_universe(default=default_arb_settings()["universe"])
        if legacy_enabled is not None or legacy_universe:
            saved = {
                **default_arb_settings(),
                "enabled": legacy_enabled,
                "universe": legacy_universe,
            }
    return merge_arb_settings(saved)


def get_arb_settings(db=None, *, use_cache: bool = True) -> dict:
    """Full Arb settings — served from in-process cache unless cache bypassed."""
    global _SETTINGS_CACHE
    if use_cache and _SETTINGS_CACHE is not None:
        return dict(_SETTINGS_CACHE)

    if db is not None:
        merged = _load_arb_settings_from_db(db)
    else:
        merged = default_arb_settings()

    _SETTINGS_CACHE = dict(merged)
    return dict(merged)


def reload_arb_settings(db) -> dict:
    """Force reload from DB and refresh the in-process cache."""
    return get_arb_settings(db, use_cache=False)


def set_arb_settings(db, settings: dict, *, updated_by: str = "ui") -> dict:
    from database.arb_models import ArbConfigRepo

    cleaned = validate_arb_settings(settings)
    repo = ArbConfigRepo(db)
    repo.set_settings(cleaned, updated_by=updated_by)
    # Keep legacy keys in sync for older code paths.
    repo.set_json(ArbConfigRepo.ENABLED_KEY, cleaned["enabled"], updated_by=updated_by)
    repo.set_json(ArbConfigRepo.UNIVERSE_KEY, cleaned["universe"], updated_by=updated_by)
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = dict(cleaned)
    return cleaned


def invalidate_settings_cache() -> None:
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None
