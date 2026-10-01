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

from typing import Any, Dict, List, Optional, Tuple

import json

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


def build_loss_milestone_plan(*, investment_rs: float) -> Optional[Dict[str, Any]]:
    """Fixed rupee loss line: pct of entry premium. None when disabled."""
    cfg = loss_milestone_config()
    rs, pct = loss_milestone_rs(investment_rs=investment_rs)
    if not cfg.get("enabled") or rs <= 0:
        return None
    return {
        "pct_of_premium": pct,
        "investment_rs": round(float(investment_rs or 0.0), 2),
        "loss_rs": round(float(rs), 2),
    }


def parse_loss_milestone_plan(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    else:
        return None
    if not isinstance(data, dict):
        return None
    try:
        loss_rs = float(data.get("loss_rs") or 0.0)
    except (TypeError, ValueError):
        return None
    if loss_rs <= 0:
        return None
    return data


def loss_milestone_plan_matches_config(plan: Optional[Dict[str, Any]]) -> bool:
    """True when *plan* was built from the live overlay %."""
    cfg = loss_milestone_config()
    parsed = parse_loss_milestone_plan(plan)
    if not cfg.get("enabled") or float(cfg.get("pct_of_premium") or 0) <= 0:
        return parsed is None
    if parsed is None:
        return False
    try:
        if abs(float(parsed.get("pct_of_premium") or 0.0)
               - float(cfg.get("pct_of_premium") or 0.0)) > 1e-6:
            return False
    except (TypeError, ValueError):
        return False
    return True


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
    min_line_rs: Optional[float] = None,
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
        # Freeze at the Nth milestone line, not at whatever peak happens
        # to be when we first notice N locks (a gap-up to ₹10k must not
        # freeze a ₹9k sell line). Line at lock N = (N-1) × giveback.
        if implied >= max_locks:
            freeze_at = giveback * (max_locks - 1)
            floor = _positive_float(min_line_rs) or 0.0
            return max(freeze_at, floor), max_locks, True
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


def build_profit_milestone_plan(
    *,
    investment_rs: float,
    charges_rs: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """M1..MN from current ``max_locks``. None when trailing is unlimited.

    Each level is (peak to arm it, sell line once it is the pointer).
    Ticks only advance the pointer. Overlay changes (N, %, buffer)
    rebuild the plan on the next evaluate.
    """
    cfg = profit_milestone_config()
    n = cfg.get("max_locks")
    if not cfg.get("enabled") or not n:
        return None
    giveback, pct = profit_milestone_rs(
        investment_rs=investment_rs, charges_rs=charges_rs,
    )
    if giveback <= 0:
        return None
    floor = profit_milestone_charges_floor_rs(charges_rs=charges_rs)
    levels: List[Dict[str, Any]] = []
    for i in range(1, int(n) + 1):
        levels.append({
            "i": i,
            "peak_rs": round(giveback * i, 2),
            "sell_rs": round(max(floor, giveback * (i - 1)), 2),
        })
    return {
        "giveback_rs": round(giveback, 2),
        "charges_floor_rs": round(floor, 2),
        "pct_of_premium": pct,
        "charges_buffer_rs": round(float(cfg.get("charges_buffer_rs") or 0.0), 2),
        "max_locks": int(n),
        "investment_rs": round(float(investment_rs or 0.0), 2),
        "levels": levels,
    }


def parse_profit_milestone_plan(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    else:
        return None
    levels = data.get("levels") if isinstance(data, dict) else None
    if not isinstance(levels, list) or not levels:
        return None
    return data


def profit_milestone_plan_matches_config(plan: Optional[Dict[str, Any]]) -> bool:
    """True when *plan* was built from the live overlay (N / % / buffer)."""
    cfg = profit_milestone_config()
    parsed = parse_profit_milestone_plan(plan)
    n = cfg.get("max_locks")
    if not cfg.get("enabled") or not n:
        return parsed is None
    if parsed is None:
        return False
    levels = parsed.get("levels") or []
    if len(levels) != int(n):
        return False
    stored_n = parsed.get("max_locks")
    if stored_n is not None:
        try:
            if int(stored_n) != int(n):
                return False
        except (TypeError, ValueError):
            return False
    try:
        if abs(float(parsed.get("pct_of_premium") or 0.0)
               - float(cfg.get("pct_of_premium") or 0.0)) > 1e-6:
            return False
    except (TypeError, ValueError):
        return False
    if parsed.get("charges_buffer_rs") is None:
        return False
    try:
        if abs(float(parsed.get("charges_buffer_rs"))
               - float(cfg.get("charges_buffer_rs") or 0.0)) > 1e-6:
            return False
    except (TypeError, ValueError):
        return False
    return True


def apply_profit_milestone_plan(
    plan: Optional[Dict[str, Any]],
    *,
    peak_rs: Optional[float],
    pointer: int = 0,
) -> Tuple[Optional[float], int, bool]:
    """Advance the pointer from precomputed levels. Never moves backwards."""
    parsed = parse_profit_milestone_plan(plan)
    if not parsed:
        return None, max(0, int(pointer or 0)), False
    levels = parsed["levels"]
    n = len(levels)
    ptr = max(0, min(int(pointer or 0), n))
    peak = float(peak_rs or 0.0)
    for lev in levels:
        try:
            i = int(lev.get("i") or 0)
            arm = float(lev.get("peak_rs") or 0.0)
        except (TypeError, ValueError):
            continue
        if i > 0 and peak + 1e-9 >= arm:
            ptr = max(ptr, i)
    ptr = min(ptr, n)
    if ptr <= 0:
        return None, 0, False
    sell = None
    try:
        sell = float(levels[ptr - 1].get("sell_rs"))
    except (TypeError, ValueError, IndexError):
        sell = None
    return sell, ptr, ptr >= n
