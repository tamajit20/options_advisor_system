"""
engine/greeks_exit.py
=====================

Greek-stress checks for open-trade exit decisions.

Uses daily trade-level Greeks from ``options_trade_greeks`` (C6 job) to flag
trades where vega or delta exposure is elevated relative to max loss — especially
near expiry on short-premium structures.
"""

from __future__ import annotations

from typing import Mapping, Optional

from config import STRATEGY_CONFIG

_CREDIT_STRATEGIES = frozenset({
    "IRON_CONDOR", "IRON_BUTTERFLY", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD",
    "JADE_LIZARD", "SHORT_STRANGLE",
})


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def greeks_stress_check(
    *,
    strategy: str,
    days_to_expiry: int,
    current_pnl: float,
    max_loss_rs: float,
    greeks: Optional[Mapping] = None,
) -> Optional[str]:
    """Return a human-readable stress reason, or None if no stress detected."""
    cfg = STRATEGY_CONFIG.get("greeks_exit") or {}
    if not cfg.get("enabled", True):
        return None
    if not greeks:
        return None

    strat = (strategy or "").upper()
    dte = max(int(days_to_expiry or 0), 0)
    ml = abs(float(max_loss_rs or 0.0))
    if ml <= 0:
        return None

    net_vega = _as_float(greeks.get("net_vega"))
    net_delta = _as_float(greeks.get("net_delta"))
    net_theta = _as_float(greeks.get("net_theta"))

    vega_dte_max = int(cfg.get("vega_stress_dte_max", 7))
    vega_frac = float(cfg.get("vega_stress_fraction_of_max_loss", 0.08))
    vega_min_rs = float(cfg.get("vega_stress_min_abs_vega_rs", 1500.0))
    min_loss_frac = float(cfg.get("vega_stress_min_loss_fraction", 0.10))

    if (
        strat in _CREDIT_STRATEGIES
        and dte <= vega_dte_max
        and current_pnl < 0
        and abs(current_pnl) >= min_loss_frac * ml
        and net_vega is not None
        and abs(net_vega) >= max(vega_min_rs, vega_frac * ml)
    ):
        return (
            f"Greek stress: |net vega| ₹{abs(net_vega):,.0f} with DTE={dte} "
            f"and MTM loss ₹{current_pnl:,.0f} — IV expansion risk elevated"
        )

    delta_dte_max = int(cfg.get("delta_stress_dte_max", 5))
    delta_frac = float(cfg.get("delta_stress_fraction_of_max_loss", 0.15))
    if (
        dte <= delta_dte_max
        and current_pnl < 0
        and net_delta is not None
        and abs(net_delta) >= delta_frac * ml
    ):
        return (
            f"Greek stress: |net delta| ₹{abs(net_delta):,.0f} with DTE={dte} "
            f"and MTM ₹{current_pnl:,.0f} — directional exposure elevated"
        )

    if net_theta is not None and current_pnl > 0:
        theta_help = float(cfg.get("theta_fade_min_daily_rs", 500.0))
        if net_theta > 0 and current_pnl > theta_help * 3:
            return (
                f"Greek note: positive net theta ₹{net_theta:,.0f}/day but "
                f"MTM already ₹{current_pnl:,.0f} — consider booking before decay fades"
            )

    return None
