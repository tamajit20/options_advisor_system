"""
engine/name_generator.py
========================

Trade name generator: `{UNDER}-{STRATEGY}-{MONTH}{WEEK}-{YY}`
e.g. `NIFTY-CONDOR-MAY2-26`, `BNIFTY-BPS-JUN1-26`.

Aliases:
    BANKNIFTY → BNIFTY
    FINNIFTY  → FNIFTY
    MIDCPNIFTY → MIDCAP

Strategy code map:
    IRON_CONDOR        → CONDOR
    BULL_PUT_SPREAD    → BPS
    BEAR_CALL_SPREAD   → BCS
    BULL_CALL_SPREAD   → BCAL
    BEAR_PUT_SPREAD    → BPUT
    LONG_STRADDLE      → STRADDLE
    LONG_STRANGLE      → STRANGLE
    SHORT_STRANGLE     → SHSTR
    BUTTERFLY          → BFLY
    CALENDAR           → CAL

Collision suffix: -B, -C, -D, ...  (caller passes existing names list).
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Iterable

_UNDERLYING_ALIASES = {
    "BANKNIFTY":  "BNIFTY",
    "FINNIFTY":   "FNIFTY",
    "MIDCPNIFTY": "MIDCAP",
}

_STRATEGY_CODES = {
    "IRON_CONDOR":      "CONDOR",
    "BULL_PUT_SPREAD":  "BPS",
    "BEAR_CALL_SPREAD": "BCS",
    "BULL_CALL_SPREAD": "BCAL",
    "BEAR_PUT_SPREAD":  "BPUT",
    "LONG_STRADDLE":    "STRADDLE",
    "LONG_STRANGLE":    "STRANGLE",
    "LONG_CALL":        "LCALL",
    "LONG_PUT":         "LPUT",
    "IRON_BUTTERFLY":   "BFLY",
    "JADE_LIZARD":      "JLIZ",
    "SHORT_STRANGLE":   "SHSTR",
    "BUTTERFLY":        "BFLY",
    "CALENDAR":         "CAL",
}


def _week_of_month(d: date) -> int:
    """Returns 1..5 — the ordinal week number containing `d` within its month.
    Week 1 begins on the 1st."""
    return ((d.day - 1) // 7) + 1


def make_trade_name(
    *,
    underlying: str,
    strategy: str,
    expiry: date,
    existing_names: Iterable[str] = (),
) -> str:
    u_code = _UNDERLYING_ALIASES.get(underlying.upper(), underlying.upper())
    s_code = _STRATEGY_CODES.get(strategy.upper(), strategy.upper().replace("_", "")[:8])
    month = calendar.month_abbr[expiry.month].upper()  # JAN..DEC
    week = _week_of_month(expiry)
    yy = expiry.year % 100
    base = f"{u_code}-{s_code}-{month}{week}-{yy:02d}"

    existing = set(existing_names)
    if base not in existing:
        return base

    # Collision: append -B, -C, ...
    for ch in "BCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{base}-{ch}"
        if candidate not in existing:
            return candidate
    raise ValueError(f"Could not generate unique trade name from base {base!r}")
