"""
engine/pnl_targets.py
=====================

Single source for profit-target math. Exit Plan (dashboard), EOD
``TAKE_PROFIT``, and live ``TARGET_HIT`` all call these helpers so the
rupee number on the card matches the alert.

Knobs live in ``STRATEGY_CONFIG``:

* Long premium (straddle / strangle / call / put / calendar):
  ``long_premium_target_*`` — DTE-aware multiple of debit paid.
* Debit spreads (bull call / bear put):
  ``debit_spread_target_fraction`` of debit paid.
* Credit spreads: ``strategy_take_profit_fraction`` of max profit
  (≈ fraction of credit captured). Fallback ``take_profit_fraction``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence, Tuple

from config import STRATEGY_CONFIG

_LONG_PREMIUM_FALLBACK = (
    "LONG_STRADDLE", "LONG_STRANGLE", "LONG_CALL", "LONG_PUT", "CALENDAR_SPREAD",
)
_DEBIT_SPREAD_FALLBACK = ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD")


def _str_set(key: str, fallback: Sequence[str]) -> frozenset:
    raw = STRATEGY_CONFIG.get(key) or fallback
    return frozenset(str(s) for s in raw)


def long_premium_strategies() -> frozenset:
    return _str_set("long_premium_target_strategies", _LONG_PREMIUM_FALLBACK)


def debit_spread_strategies() -> frozenset:
    return _str_set("debit_spread_target_strategies", _DEBIT_SPREAD_FALLBACK)


def is_long_premium_strategy(strategy: str) -> bool:
    return str(strategy or "") in long_premium_strategies()


def is_debit_spread_strategy(strategy: str) -> bool:
    return str(strategy or "") in debit_spread_strategies()


def long_premium_target_multiple(dte: int) -> float:
    """DTE-aware multiple of debit paid for long-premium structures.

        multiple = base + dte / dte_scale,   capped at max

    Defaults (base=0.50, dte_scale=14, max=1.50): 7 DTE → 1.00×.
    """
    base = float(STRATEGY_CONFIG.get("long_premium_target_base", 0.50))
    dte_scale = float(STRATEGY_CONFIG.get("long_premium_target_dte_scale", 14.0))
    cap = float(STRATEGY_CONFIG.get("long_premium_target_max", 1.50))
    dte_n = int(dte or 0)
    if dte_n <= 0 or dte_scale <= 0:
        return base
    return float(min(cap, base + dte_n / dte_scale))


def debit_spread_target_fraction() -> float:
    return float(STRATEGY_CONFIG.get("debit_spread_target_fraction", 0.50))


def credit_capture_fraction(strategy: str) -> float:
    """Fraction of max profit (≈ credit captured) for credit / fallback."""
    overrides = STRATEGY_CONFIG.get("strategy_take_profit_fraction") or {}
    default = float(STRATEGY_CONFIG.get("take_profit_fraction", 0.80))
    raw = overrides.get(strategy)
    if raw is None:
        return default
    return float(raw)


def _finite_positive(value: Optional[float]) -> bool:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def _debit_rs(entry_net_credit: float) -> float:
    if entry_net_credit < 0:
        return abs(float(entry_net_credit))
    return 0.0


def profit_target_trade_rs(
    *,
    strategy: str,
    dte: int,
    max_profit_rs: float,
    entry_net_credit: float,
) -> Tuple[Optional[float], str, float]:
    """Whole-trade rupee target matching the Exit Plan.

    Returns ``(target_rs, kind, fraction_or_multiple)``.
    ``target_rs`` is None when the inputs cannot form a target.
    """
    strat = str(strategy or "")
    if is_long_premium_strategy(strat):
        debit = _debit_rs(entry_net_credit)
        mult = long_premium_target_multiple(dte)
        if debit <= 0:
            return None, "long_premium", mult
        return debit * mult, "long_premium", mult

    if is_debit_spread_strategy(strat):
        debit = _debit_rs(entry_net_credit)
        frac = debit_spread_target_fraction()
        if debit <= 0:
            return None, "debit_spread", frac
        return debit * frac, "debit_spread", frac

    frac = credit_capture_fraction(strat)
    if _finite_positive(max_profit_rs):
        return float(max_profit_rs) * frac, "credit", frac
    credit = max(float(entry_net_credit or 0.0), 0.0)
    if credit > 0:
        return credit * frac, "credit", frac
    return None, "credit", frac


def take_profit_hit(
    *,
    strategy: str,
    dte: int,
    current_pnl: float,
    max_profit_rs: float,
    entry_net_credit: float,
) -> Tuple[bool, str]:
    """Whether MTM has reached the configured profit target."""
    if current_pnl <= 0:
        return False, ""
    target_rs, kind, frac = profit_target_trade_rs(
        strategy=strategy,
        dte=dte,
        max_profit_rs=max_profit_rs,
        entry_net_credit=entry_net_credit,
    )
    if target_rs is None or target_rs <= 0:
        return False, ""
    if current_pnl < target_rs:
        return False, ""
    pct = frac * 100.0
    if kind == "long_premium":
        reason = (
            f"Gained ≥{pct:.0f}% of debit paid "
            f"(₹{current_pnl:.0f} of ₹{target_rs:.0f})"
        )
    elif kind == "debit_spread":
        reason = (
            f"Spread gained ≥{pct:.0f}% of debit paid "
            f"(₹{current_pnl:.0f} of ₹{target_rs:.0f})"
        )
    else:
        reason = (
            f"Captured ≥{pct:.0f}% of max profit "
            f"(₹{current_pnl:.0f} of ₹{max_profit_rs:.0f})"
        )
    return True, reason


def pnl_rules_public() -> Dict[str, Any]:
    """JSON-safe slice of STRATEGY_CONFIG for the dashboard Exit Plan."""
    lrm = STRATEGY_CONFIG.get("live_risk_monitor") or {}
    sl_defaults = dict(STRATEGY_CONFIG.get("strategy_sl_defaults") or {})
    sl_limits = STRATEGY_CONFIG.get("strategy_sl_limits") or {}
    return {
        "strategy_sl_defaults": sl_defaults,
        "strategy_sl_limits": sl_limits,
        "long_premium_target_base": float(
            STRATEGY_CONFIG.get("long_premium_target_base", 0.50)
        ),
        "long_premium_target_dte_scale": float(
            STRATEGY_CONFIG.get("long_premium_target_dte_scale", 14.0)
        ),
        "long_premium_target_max": float(
            STRATEGY_CONFIG.get("long_premium_target_max", 1.50)
        ),
        "long_premium_target_strategies": sorted(long_premium_strategies()),
        "debit_spread_target_fraction": debit_spread_target_fraction(),
        "debit_spread_target_strategies": sorted(debit_spread_strategies()),
        "take_profit_fraction": float(
            STRATEGY_CONFIG.get("take_profit_fraction", 0.80)
        ),
        "strategy_take_profit_fraction": dict(
            STRATEGY_CONFIG.get("strategy_take_profit_fraction") or {}
        ),
        "live_risk_monitor": {
            "trailing_sl_steps": list(lrm.get("trailing_sl_steps") or []),
            "pre_breach_fraction": float(lrm.get("pre_breach_fraction", 0.70)),
        },
        "loss_milestone_alert": dict(
            STRATEGY_CONFIG.get("loss_milestone_alert") or {
                "enabled": False,
                "pct_of_max_loss": 25.0,
            }
        ),
    }
