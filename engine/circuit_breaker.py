"""
engine/circuit_breaker.py
=========================

Pure function. Decides whether the **aggregate** MTM across all open
trades has breached the operator's daily-loss limit.

This is the system-wide kill, distinct from per-trade SL_HIT and from
the per-trade ADVERSE_MOVE_WARNING:

    * SL_HIT                \u2014 one trade hit its own stop.
    * ADVERSE_MOVE_WARNING  \u2014 one trade is in the red but not yet at SL.
    * DAILY_PNL_BREACH      \u2014 *all* trades together have exceeded the
                              operator's daily loss budget. Block new
                              executions and ring the alarm.

Inputs
------
total_pnl_rs : sum of current MTMs across all open trades (signed; negative
               = loss).
capital_rs   : declared trading capital. Configured in
               `STRATEGY_CONFIG["daily_pnl_circuit_breaker_capital_rs"]`.
limit_pct    : breach threshold as a percentage of capital (positive).
               `STRATEGY_CONFIG["daily_pnl_circuit_breaker_pct"]`.

Returns
-------
None when within budget, otherwise a `CircuitBreakerStatus` with the
human-readable headline + numeric details.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import STRATEGY_CONFIG


@dataclass(frozen=True)
class CircuitBreakerStatus:
    breached: bool
    total_pnl_rs: float
    capital_rs: float
    limit_pct: float
    limit_rs: float        # positive value: capital * limit_pct / 100
    pct_of_capital: float  # signed: negative when losing
    headline: str


def check_daily_pnl_breach(
    *,
    total_pnl_rs: float,
    capital_rs: Optional[float] = None,
    limit_pct: Optional[float] = None,
) -> Optional[CircuitBreakerStatus]:
    """Return a `CircuitBreakerStatus` only when the breach has occurred."""
    capital_rs = (
        float(capital_rs) if capital_rs is not None
        else float(STRATEGY_CONFIG.get(
            "daily_pnl_circuit_breaker_capital_rs", 500_000.0,
        ))
    )
    limit_pct = (
        float(limit_pct) if limit_pct is not None
        else float(STRATEGY_CONFIG.get("daily_pnl_circuit_breaker_pct", 3.0))
    )
    if capital_rs <= 0 or limit_pct <= 0:
        return None  # mis-configured \u2014 fail-open

    limit_rs = capital_rs * limit_pct / 100.0
    pct_of_capital = (total_pnl_rs / capital_rs) * 100.0
    if total_pnl_rs >= -limit_rs:
        # Within budget (covers winning, flat, and tolerable losses).
        # Equality is treated as still-within-budget on purpose: the
        # breach should require the limit to be strictly exceeded.
        return None

    headline = (
        f"\U0001F6D1 Daily P&L breach: "
        f"\u20b9{total_pnl_rs:+,.0f} ({pct_of_capital:+.2f}% of "
        f"\u20b9{capital_rs:,.0f}) \u2014 limit \u2013\u20b9{limit_rs:,.0f} "
        f"({limit_pct:.1f}%)"
    )
    return CircuitBreakerStatus(
        breached=True,
        total_pnl_rs=round(total_pnl_rs, 2),
        capital_rs=capital_rs,
        limit_pct=limit_pct,
        limit_rs=round(limit_rs, 2),
        pct_of_capital=round(pct_of_capital, 3),
        headline=headline,
    )
