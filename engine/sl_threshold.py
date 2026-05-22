"""
engine/sl_threshold.py
======================

Per-strategy MTM stop-loss threshold in rupees.

Effective SL = loss_fraction × max_loss, optionally capped at absolute_cap_rs.
When cap_min_max_loss_rs is set, the rupee cap applies only if max_loss meets
that floor (smaller trades use the fraction alone — fewer false triggers).

Configured in STRATEGY_CONFIG["strategy_sl_limits"] with fallbacks from
strategy_sl_defaults.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from config import STRATEGY_CONFIG


def strategy_sl_config(strategy: str) -> Dict[str, Any]:
    """Return resolved SL knobs for *strategy*."""
    defaults = STRATEGY_CONFIG.get("strategy_sl_defaults") or {}
    base_frac = float(
        defaults.get("loss_fraction", STRATEGY_CONFIG.get("stop_loss_fraction", 0.50))
    )
    base_cap = defaults.get("absolute_cap_rs")
    base_cap_min = defaults.get("cap_min_max_loss_rs")

    per = (STRATEGY_CONFIG.get("strategy_sl_limits") or {}).get(strategy) or {}
    frac = float(per.get("loss_fraction", base_frac))

    cap_raw = per.get("absolute_cap_rs", base_cap) if per else base_cap
    cap: Optional[float] = None if cap_raw is None else float(cap_raw)

    cap_min_raw = per.get("cap_min_max_loss_rs", base_cap_min) if per else base_cap_min
    cap_min: Optional[float] = None if cap_min_raw is None else float(cap_min_raw)

    return {
        "loss_fraction": frac,
        "absolute_cap_rs": cap,
        "cap_min_max_loss_rs": cap_min,
    }


def _cap_applies(cfg: Dict[str, Any], max_loss_rs: float) -> bool:
    cap = cfg.get("absolute_cap_rs")
    if cap is None or cap <= 0:
        return False
    cap_min = cfg.get("cap_min_max_loss_rs")
    if cap_min is not None and max_loss_rs < float(cap_min):
        return False
    return True


def effective_sl_rs(*, strategy: str, max_loss_rs: float) -> Tuple[float, str]:
    """Return positive rupee SL threshold and a short binding-reason label."""
    if max_loss_rs <= 0:
        return 0.0, "no max loss"

    cfg = strategy_sl_config(strategy)
    frac = cfg["loss_fraction"]
    pct_rs = frac * max_loss_rs
    cap = cfg.get("absolute_cap_rs")

    if _cap_applies(cfg, max_loss_rs) and pct_rs > cap:
        return cap, f"₹{cap:,.0f} cap"

    return pct_rs, f"{frac * 100:.0f}% of max loss"
