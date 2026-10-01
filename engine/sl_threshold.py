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

    Independent of ``loss_milestone_alert``. Giveback is
    ``max(pct of premium, estimated charges + charges_buffer_rs)``.
    Sell line = peak − giveback, floored at charges + buffer so the line
    Line ratchets up with new peaks until ``max_locks`` (blank / omitted =
    unlimited). A lock is one giveback-sized step of peak, not each tick.
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
        "confirm_seconds": _confirm_seconds(raw, default=15),
        "charges_buffer_rs": _charges_buffer_rs(raw),
        "max_locks": _max_profit_locks(raw),
    }


def _charges_buffer_rs(raw: dict, *, default: float = 50.0) -> float:
    """Extra rupees on estimated charges for giveback / sell-line floor."""
    if "charges_buffer_rs" not in raw:
        return default
    try:
        return max(0.0, float(raw.get("charges_buffer_rs")))
    except (TypeError, ValueError):
        return default


def _max_profit_locks(raw: dict) -> Optional[int]:
    """How many upward sell-line ratchets before freeze.

    ``None`` means unlimited — config omitted, ``null``, or blank ``\"\"`` /
    whitespace. A positive int freezes the line after that many locks.
    """
    if "max_locks" not in raw or raw.get("max_locks") is None:
        return None
    val = raw.get("max_locks")
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return None
        try:
            n = int(text)
        except ValueError:
            return None
    else:
        try:
            n = int(val)
        except (TypeError, ValueError):
            return None
    if n <= 0:
        return None
    return n


def advance_profit_milestone_locks(
    *,
    raw_line_rs: Optional[float],
    prev_line_rs: Optional[float],
    lock_count: int,
    max_locks: Optional[int],
    peak_rs: Optional[float] = None,
    giveback_rs: Optional[float] = None,
) -> Tuple[Optional[float], int, bool]:
    """Apply optional freeze after ``max_locks`` material ratchets.

    Returns ``(line_rs, lock_count, frozen)``.

    A lock is one giveback-sized step of peak MTM (lock 1 when the line
    first arms at peak ≥ giveback; lock 2 at 2× giveback, …). Tiny live
    ticks must not consume locks — otherwise ``max_locks=3`` freezes on
    the charges floor after three ₹1 upticks.

    When ``peak_rs`` / ``giveback_rs`` are omitted, falls back to counting
    any upward move of the sell line (unit tests / legacy).
    When ``max_locks`` is ``None``, behaviour matches unlimited trailing.
    """
    count = max(0, int(lock_count or 0))
    if max_locks is None:
        return raw_line_rs, count, False
    if raw_line_rs is None:
        return prev_line_rs, count, bool(count >= max_locks and prev_line_rs is not None)

    raw = float(raw_line_rs)
    if count >= max_locks and prev_line_rs is not None:
        return float(prev_line_rs), count, True

    peak = _positive_float(peak_rs)
    giveback = _positive_float(giveback_rs)
    if peak is not None and giveback is not None:
        implied = max(1, int(peak // giveback))
        implied = max(count, implied)
        if implied >= max_locks:
            return raw, max_locks, True
        return raw, implied, False

    # Legacy: any strictly higher sell line consumes a lock.
    if prev_line_rs is None:
        return raw, 1, max_locks <= 1
    prev = float(prev_line_rs)
    if raw > prev + 1e-9:
        new_count = count + 1
        return raw, new_count, new_count >= max_locks
    return prev, count, False


def _positive_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        n = float(val)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def profit_milestone_rs(*, investment_rs: float, charges_rs: float = 0.0) -> Tuple[float, float]:
    """Return (giveback_rs, pct_of_premium) when enabled; else (0.0, pct).

    Giveback is ``pct_of_premium`` of entry premium, but never smaller than
    estimated round-trip charges + ``charges_buffer_rs``. Used as the
    distance below peak for the trailing sell line.
    """
    cfg = profit_milestone_config()
    pct = cfg["pct_of_premium"]
    if not cfg["enabled"] or investment_rs <= 0 or pct <= 0:
        return 0.0, pct
    configured = investment_rs * (pct / 100.0)
    try:
        charges = max(0.0, float(charges_rs or 0.0))
    except (TypeError, ValueError):
        charges = 0.0
    floor = charges + float(cfg.get("charges_buffer_rs") or 0.0)
    return max(configured, floor), pct


def profit_milestone_charges_floor_rs(*, charges_rs: float = 0.0) -> float:
    """Estimated charges + ``charges_buffer_rs`` — minimum sell-line level."""
    cfg = profit_milestone_config()
    try:
        charges = max(0.0, float(charges_rs or 0.0))
    except (TypeError, ValueError):
        charges = 0.0
    return charges + float(cfg.get("charges_buffer_rs") or 0.0)


def profit_pct_auto_close_config() -> Dict[str, Any]:
    """Hard profit-% take: close when MTM ≥ pct of entry premium (no confirm)."""
    raw = STRATEGY_CONFIG.get("profit_pct_auto_close") or {}
    enabled = bool(raw.get("enabled", False))
    try:
        pct = float(
            raw.get("pct_of_premium")
            if raw.get("pct_of_premium") is not None
            else 5.0
        )
    except (TypeError, ValueError):
        pct = 5.0
    pct = max(0.0, min(100.0, pct))
    return {
        "enabled": enabled,
        "pct_of_premium": pct,
        # Always flattens when enabled — no separate auto_close toggle.
        "auto_close": True,
        "auto_close_retry_seconds": 60,
    }


def profit_pct_auto_close_rs(*, investment_rs: float) -> Tuple[float, float]:
    """Return (target_rs, pct) when enabled; else (0.0, pct)."""
    cfg = profit_pct_auto_close_config()
    pct = cfg["pct_of_premium"]
    if not cfg["enabled"] or investment_rs <= 0 or pct <= 0:
        return 0.0, pct
    return investment_rs * (pct / 100.0), pct


def profit_milestone_line_rs(
    *,
    peak_rs: Optional[float],
    giveback_rs: float,
    min_line_rs: float = 0.0,
) -> Optional[float]:
    """Trailing sell line once peak ≥ giveback. Else None.

    Line = max(peak − giveback, min_line_rs) where ``min_line_rs`` is normally
    charges + buffer. So the line trails with peak but never drops below
    brokerage. Ratchets only via peak.
    """
    if giveback_rs <= 0 or peak_rs is None:
        return None
    peak = float(peak_rs)
    giveback = float(giveback_rs)
    if peak < giveback:
        return None
    try:
        floor = max(0.0, float(min_line_rs or 0.0))
    except (TypeError, ValueError):
        floor = 0.0
    return max(peak - giveback, floor)
