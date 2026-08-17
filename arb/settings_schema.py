"""Arb Monitor UI settings — defaults, validation, and helpers."""

from __future__ import annotations

from typing import Optional

from config import ARB_CONFIG

VALID_UNIVERSES = frozenset({"nifty50_dual", "all_matched"})


def default_arb_settings() -> dict:
    """Persisted Arb settings (merged over ARB_CONFIG at runtime)."""
    return {
        "enabled": bool(ARB_CONFIG.get("enabled", True)),
        "universe": str(ARB_CONFIG.get("universe", "nifty50_dual")),
        "tick_staleness_sec": float(ARB_CONFIG.get("tick_staleness_sec", 3)),
        "leg_stale_close_sec": float(ARB_CONFIG.get("leg_stale_close_sec", 5)),
        "min_gap_store_pct": float(ARB_CONFIG.get("min_gap_store_pct", 0) or 0),
        "min_duration_store_sec": int(ARB_CONFIG.get("min_duration_store_sec", 0) or 0),
    }


def merge_arb_settings(saved: Optional[dict]) -> dict:
    base = default_arb_settings()
    if not saved:
        return base
    out = {**base}
    for k in base:
        if k in saved and saved[k] is not None:
            out[k] = saved[k]
    return validate_arb_settings(out)


def validate_arb_settings(raw: dict) -> dict:
    d = default_arb_settings()
    src = raw if isinstance(raw, dict) else {}

    d["enabled"] = bool(src.get("enabled", d["enabled"]))

    universe = str(src.get("universe", d["universe"])).lower().strip()
    d["universe"] = universe if universe in VALID_UNIVERSES else d["universe"]

    d["tick_staleness_sec"] = max(
        0.5, min(float(src.get("tick_staleness_sec", d["tick_staleness_sec"])), 30.0),
    )
    d["leg_stale_close_sec"] = max(
        1.0, min(float(src.get("leg_stale_close_sec", d["leg_stale_close_sec"])), 60.0),
    )
    d["min_gap_store_pct"] = max(
        0.0, min(float(src.get("min_gap_store_pct", d["min_gap_store_pct"])), 10.0),
    )
    d["min_duration_store_sec"] = max(
        0, min(int(src.get("min_duration_store_sec", d["min_duration_store_sec"])), 3600),
    )

    if d["leg_stale_close_sec"] < d["tick_staleness_sec"]:
        d["leg_stale_close_sec"] = d["tick_staleness_sec"] + 1.0

    return d


def arb_enabled(settings: Optional[dict] = None) -> bool:
    """Master switch — code-level ARB_CONFIG plus persisted UI setting."""
    if not ARB_CONFIG.get("enabled", True):
        return False
    s = settings if settings is not None else default_arb_settings()
    return bool(s.get("enabled", True))
