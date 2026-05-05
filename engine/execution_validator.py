"""
engine/execution_validator.py
=============================

**Pure** pre-execution gate. Given a suggestion + its legs (and
optionally a current chain / now timestamp), decide whether the user
should be allowed to execute.

This is the centralized place every execution path must run before
flipping a `PENDING` suggestion to a real trade. It is intentionally
side-effect-free — no DB writes, no notifications, no logging at
WARNING — so callers (`lifecycle/trade_executor.mark_executed`,
dashboard "preview" endpoint, future automation) can all share the
same logic and it is trivially unit-testable.

Checks
------
1. **Status**         — suggestion must be `PENDING`. EXECUTED/IGNORED
                        block.
2. **Validator stamp** — `validator_status == 'STALE_0935'` blocks.
                        Anything else passes (NULL, NOT_VALIDATED,
                        STILL_GOOD_0935 are all fine).
3. **Entry-date**     — `entry_date < today` blocks (stale suggestion
                        from a prior day that wasn't tidied up).
4. **Data freshness** — `now - data_as_of` must be <= configured
                        max age (default 240 min). Skipped if
                        `data_as_of` is missing.
5. **Strike distance** — every short leg's strike must be at least
                        `min_short_strike_buffer_pct` of `spot_at_generation`
                        away from spot, in the correct direction
                        (short PE below spot, short CE above spot).
                        Long-only suggestions skip this check.

Each failed check appends a string to `vetoes`. The validator is fail-open
on missing data — if `spot_at_generation` is None we can't run #5, so
we add a soft `warnings` entry rather than a hard veto.

The result is a small dataclass so callers can present granular
feedback to the user instead of a single boolean.

Locked-architecture rules
-------------------------
* No DB / no I/O / no clock reads inside the function — `now` is
  always passed (defaults to `utils.now_ist()` only at the boundary).
* No imports from `database.*` or `lifecycle.*` — engine layer.
* Configuration via `STRATEGY_CONFIG` only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Iterable, List, Optional

from config import STRATEGY_CONFIG
from utils import now_ist, today_ist


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionValidation:
    """Outcome of `validate_execution`.

    `ok` is True iff `vetoes` is empty. `warnings` are advisory and
    never block execution. `details` is a free-form dict for the UI to
    show diagnostic info (e.g. computed buffer percentages).
    """
    ok: bool
    vetoes:   List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details:  dict      = field(default_factory=dict)

    def reason(self) -> str:
        """Single-line summary of why execution was blocked."""
        if self.ok:
            return "OK"
        return "; ".join(self.vetoes)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)).date()
    except (TypeError, ValueError):
        return None


def _as_datetime(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def validate_execution(
    suggestion: dict,
    legs: Iterable[dict],
    *,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
    circuit_breaker_active: bool = False,
) -> ExecutionValidation:
    """Run all pre-execution checks. See module docstring for details."""
    if not STRATEGY_CONFIG.get("execution_validator_enabled", True):
        return ExecutionValidation(ok=True, details={"skipped": True})

    now    = now    or now_ist()
    today  = today  or today_ist()
    legs   = list(legs)
    vetoes:   List[str] = []
    warnings: List[str] = []
    details: dict = {}

    # 0. Daily P&L circuit breaker (system-wide). Hard veto regardless
    # of whether this individual trade looks fine — the operator has
    # exhausted their daily budget.
    if circuit_breaker_active:
        vetoes.append(
            "daily P&L circuit breaker is active — aggregate losses "
            "have breached the configured limit; clear the runtime "
            "flag to resume executions"
        )
        details["circuit_breaker_active"] = True

    # 1. Status -------------------------------------------------------------
    status = (suggestion.get("status") or "").upper()
    if status != "PENDING":
        vetoes.append(f"suggestion status is {status!r}, not PENDING")

    # 2. Validator stamp ----------------------------------------------------
    vstatus = (suggestion.get("validator_status") or "").upper()
    if vstatus == "STALE_0935":
        vetoes.append("intraday validator marked this suggestion STALE_0935")
    details["validator_status"] = vstatus or "NOT_VALIDATED"

    # 3. Entry date ---------------------------------------------------------
    entry_date = _as_date(suggestion.get("entry_date"))
    if entry_date is not None and entry_date < today:
        vetoes.append(
            f"entry_date {entry_date} is in the past (today is {today})"
        )

    # 4. Freshness ----------------------------------------------------------
    max_age_min = float(
        STRATEGY_CONFIG.get("execution_validator_max_data_age_minutes", 240.0)
    )
    data_as_of = _as_datetime(suggestion.get("data_as_of"))
    if data_as_of is not None:
        age_min = (now - data_as_of).total_seconds() / 60.0
        details["data_age_minutes"] = round(age_min, 1)
        if age_min > max_age_min:
            vetoes.append(
                f"underlying data is {age_min:.0f}m old "
                f"(max {max_age_min:.0f}m)"
            )
    else:
        warnings.append("data_as_of is missing — freshness not checked")

    # 4b. Suggestion freshness (Phase 3 — #2). Hard cap on how stale a
    # PENDING suggestion can be at the moment of execution. Independent of
    # data_as_of (which is the underlying market-data clock); this gates
    # on `generated_on` (the engine clock that priced the legs) so a user
    # can't click through a 09:30 suggestion at 14:55 with completely
    # different premiums.
    fresh_min = float(
        STRATEGY_CONFIG.get("suggestion_freshness_minutes", 30)
    )
    gen_on = _as_datetime(suggestion.get("generated_on"))
    if gen_on is not None and fresh_min > 0:
        age_min = (now - gen_on).total_seconds() / 60.0
        details["suggestion_age_minutes"] = round(age_min, 1)
        if age_min > fresh_min:
            vetoes.append(
                f"suggestion generated {age_min:.0f}m ago "
                f"(max {fresh_min:.0f}m); re-validate or regenerate"
            )

    # 5. Strike-distance ----------------------------------------------------
    buf_pct = float(
        STRATEGY_CONFIG.get("min_short_strike_buffer_pct", 1.5)
    )
    spot = suggestion.get("spot_at_generation")
    short_legs = [l for l in legs if str(l.get("action", "")).upper() == "SELL"]
    if not short_legs:
        details["strike_distance"] = "no short legs"
    elif spot is None:
        warnings.append(
            "spot_at_generation missing — strike distance not checked"
        )
    else:
        try:
            spot_f = float(spot)
        except (TypeError, ValueError):
            warnings.append("spot_at_generation is not numeric")
        else:
            min_buffer = spot_f * buf_pct / 100.0
            offending: list[str] = []
            for leg in short_legs:
                try:
                    strike = float(leg["strike"])
                except (KeyError, TypeError, ValueError):
                    continue
                opt = str(leg.get("option_type", "")).upper()
                # Required direction: short PE strikes BELOW spot, short CE ABOVE.
                # Distance must respect the buffer in the correct direction —
                # a short PE at strike >= spot is wrong-side AND too close.
                if opt == "PE":
                    distance = spot_f - strike
                elif opt == "CE":
                    distance = strike - spot_f
                else:
                    continue
                if distance < min_buffer:
                    pct = (distance / spot_f * 100.0) if spot_f else 0.0
                    offending.append(
                        f"short {opt} {strike:g} only {pct:+.2f}% from spot "
                        f"(min {buf_pct:.2f}%)"
                    )
            if offending:
                vetoes.append(
                    "strike too close to spot: " + "; ".join(offending)
                )
            details["spot"] = spot_f
            details["min_buffer_pct"] = buf_pct

    return ExecutionValidation(
        ok=not vetoes,
        vetoes=vetoes,
        warnings=warnings,
        details=details,
    )
