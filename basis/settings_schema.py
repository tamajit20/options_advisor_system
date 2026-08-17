"""Basis Monitor UI settings — defaults, validation, and helpers."""

from __future__ import annotations

from typing import Optional

from config import BASIS_CONFIG

VALID_UNIVERSES = frozenset({"nifty50_fo", "all_nse_fo"})


def default_basis_settings() -> dict:
    """Persisted Basis settings (merged over BASIS_CONFIG at runtime)."""
    return {
        "enabled": bool(BASIS_CONFIG.get("enabled", True)),
        "universe": str(BASIS_CONFIG.get("universe", "nifty50_fo")),
        "tick_staleness_sec": float(BASIS_CONFIG.get("tick_staleness_sec", 3)),
        "min_basis_store_pct": float(BASIS_CONFIG.get("min_basis_store_pct", 0) or 0),
        "min_duration_store_sec": int(BASIS_CONFIG.get("min_duration_store_sec", 0) or 0),
    }


def merge_basis_settings(saved: Optional[dict]) -> dict:
    base = default_basis_settings()
    if not saved:
        return base
    out = {**base}
    for k in base:
        if k in saved and saved[k] is not None:
            out[k] = saved[k]
    return validate_basis_settings(out)


def validate_basis_settings(raw: dict) -> dict:
    d = default_basis_settings()
    src = raw if isinstance(raw, dict) else {}

    d["enabled"] = bool(src.get("enabled", d["enabled"]))

    universe = str(src.get("universe", d["universe"])).lower().strip()
    d["universe"] = universe if universe in VALID_UNIVERSES else d["universe"]

    d["tick_staleness_sec"] = max(
        0.5, min(float(src.get("tick_staleness_sec", d["tick_staleness_sec"])), 30.0),
    )
    d["min_basis_store_pct"] = max(
        0.0, min(float(src.get("min_basis_store_pct", d["min_basis_store_pct"])), 10.0),
    )
    d["min_duration_store_sec"] = max(
        0, min(int(src.get("min_duration_store_sec", d["min_duration_store_sec"])), 3600),
    )

    return d


def basis_enabled(settings: Optional[dict] = None) -> bool:
    """Master switch — code-level BASIS_CONFIG plus persisted UI setting."""
    if not BASIS_CONFIG.get("enabled", True):
        return False
    s = settings if settings is not None else default_basis_settings()
    return bool(s.get("enabled", True))
