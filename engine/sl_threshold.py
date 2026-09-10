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


def trade_investment_rs(*, entry_net_credit_rs: float) -> float:
    """Absolute premium at entry — same basis as dashboard P&L % brackets."""
    return abs(float(entry_net_credit_rs or 0.0))


def _confirm_seconds(raw: dict, *, default: int = 20) -> int:
    """Seconds MTM must stay past a milestone before auto-close. 0 = first tick."""
    try:
        return max(0, int(raw.get("confirm_seconds", default)))
    except (TypeError, ValueError):
        return default


def loss_milestone_config() -> Dict[str, Any]:
    """Resolved loss-milestone knobs from STRATEGY_CONFIG."""
    raw = STRATEGY_CONFIG.get("loss_milestone_alert") or {}
    enabled = bool(raw.get("enabled", False))
    pct_raw = raw.get("pct_of_premium")
    if pct_raw is None:
        pct_raw = raw.get("pct_of_max_loss")  # legacy deployments
    try:
        pct = float(pct_raw if pct_raw is not None else 25.0)
    except (TypeError, ValueError):
        pct = 25.0
    pct = max(0.0, min(100.0, pct))
    cd = raw.get("cooldown_minutes")
    cooldown_minutes: Optional[int] = None
    if cd is not None:
        try:
            cooldown_minutes = max(0, int(cd))
        except (TypeError, ValueError):
            cooldown_minutes = None
    auto_close = bool(raw.get("auto_close", True))
    retry_raw = raw.get("auto_close_retry_seconds", 60)
    try:
        auto_close_retry_seconds = max(0, int(retry_raw))
    except (TypeError, ValueError):
        auto_close_retry_seconds = 60
    return {
        "enabled": enabled,
        "pct_of_premium": pct,
        "cooldown_minutes": cooldown_minutes,
        "auto_close": auto_close,
        "auto_close_retry_seconds": auto_close_retry_seconds,
        "confirm_seconds": _confirm_seconds(raw),
    }


def loss_milestone_rs(*, investment_rs: float) -> Tuple[float, float]:
    """Return (milestone_rs, pct_of_premium) when enabled; else (0.0, pct)."""
    cfg = loss_milestone_config()
    pct = cfg["pct_of_premium"]
    if not cfg["enabled"] or investment_rs <= 0 or pct <= 0:
        return 0.0, pct
    return investment_rs * (pct / 100.0), pct


def profit_milestone_config() -> Dict[str, Any]:
    """Resolved profit-milestone knobs from STRATEGY_CONFIG.

    Independent of ``loss_milestone_alert``. Giveback is ``pct_of_premium`` of
    entry premium, measured from the trade's peak MTM once that peak is at
    least the giveback amount. SL stays on the loss side.
    """
    raw = STRATEGY_CONFIG.get("profit_milestone_alert") or {}
    enabled = bool(raw.get("enabled", False))
    try:
        pct = float(raw.get("pct_of_premium") if raw.get("pct_of_premium") is not None else 5.0)
    except (TypeError, ValueError):
        pct = 5.0
    pct = max(0.0, min(100.0, pct))
    cd = raw.get("cooldown_minutes")
    cooldown_minutes: Optional[int] = None
    if cd is not None:
        try:
            cooldown_minutes = max(0, int(cd))
        except (TypeError, ValueError):
            cooldown_minutes = None
    auto_close = bool(raw.get("auto_close", True))
    retry_raw = raw.get("auto_close_retry_seconds", 60)
    try:
        auto_close_retry_seconds = max(0, int(retry_raw))
    except (TypeError, ValueError):
        auto_close_retry_seconds = 60
    return {
        "enabled": enabled,
        "pct_of_premium": pct,
        "cooldown_minutes": cooldown_minutes,
        "auto_close": auto_close,
        "auto_close_retry_seconds": auto_close_retry_seconds,
        "confirm_seconds": _confirm_seconds(raw),
    }


def profit_milestone_rs(*, investment_rs: float) -> Tuple[float, float]:
    """Return (giveback_rs, pct_of_premium) when enabled; else (0.0, pct)."""
    cfg = profit_milestone_config()
    pct = cfg["pct_of_premium"]
    if not cfg["enabled"] or investment_rs <= 0 or pct <= 0:
        return 0.0, pct
    return investment_rs * (pct / 100.0), pct


def profit_milestone_line_rs(
    *, peak_rs: Optional[float], giveback_rs: float,
) -> Optional[float]:
    """Sell-at MTM once peak is a real profit (peak ≥ giveback). Else None.

    Line = peak − giveback and never goes negative. Ratchets only via peak.
    """
    if giveback_rs <= 0 or peak_rs is None:
        return None
    peak = float(peak_rs)
    if peak < giveback_rs:
        return None
    return peak - float(giveback_rs)
