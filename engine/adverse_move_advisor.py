"""
engine/adverse_move_advisor.py
==============================

Pure function. Decides whether an open trade has moved adversely enough
to warrant an early-warning notification, **before** the hard SL is hit.

Why a separate concept from `engine/exit_engine.evaluate_exit`?
---------------------------------------------------------------
`evaluate_exit` returns one of HOLD / TAKE_PROFIT / SL_HIT / EXPIRE / ...
Once it returns HOLD, the trade is "fine, just keep watching". But there
is a useful intermediate band: the trade is in the red enough that the
user should be thinking about defensive action, but not so far gone that
the SL has triggered. This function fills that gap.

Inputs
------
current_pnl    : current MTM in rupees (signed; negative = loss)
max_loss_rs    : the trade's defined max loss in rupees (positive)
warning_pct    : threshold as percentage of max_loss; default from
                 STRATEGY_CONFIG["adverse_move_warning_pct"]
sl_pct         : SL fraction (so we never warn when SL has already fired);
                 default from STRATEGY_CONFIG["stop_loss_fraction"]

Returns
-------
None when the trade is not in the warning band, or an `AdverseMoveAdvice`
dataclass with severity + recovery suggestion text.

Severity bands
--------------
The result is a single severity tier ("MODERATE"); we keep the API tiny
on purpose. If finer granularity is needed in the future we can add
"SEVERE" at e.g. 50% of max loss \u2014 a tracked future-scope item.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import STRATEGY_CONFIG


@dataclass(frozen=True)
class AdverseMoveAdvice:
    severity: str           # "MODERATE"
    pnl_pct_of_max_loss: float  # current loss as % of max loss, e.g. 35.2
    headline: str           # short user-facing line
    recovery_hint: str      # multi-line action suggestion


def assess_adverse_move(
    *,
    current_pnl: float,
    max_loss_rs: float,
    warning_pct: Optional[float] = None,
    sl_pct: Optional[float] = None,
) -> Optional[AdverseMoveAdvice]:
    """Pure check. Returns advice if the trade is in the warning band."""
    if max_loss_rs <= 0:
        return None
    if current_pnl >= 0:
        return None  # winning or flat

    warning_pct = (
        float(warning_pct) if warning_pct is not None
        else float(STRATEGY_CONFIG.get("adverse_move_warning_pct", 30.0))
    )
    sl_pct = (
        float(sl_pct) if sl_pct is not None
        else float(STRATEGY_CONFIG.get("stop_loss_fraction", 0.60)) * 100.0
    )

    pct_of_max = abs(current_pnl) / max_loss_rs * 100.0

    # Below warning threshold \u2014 nothing to say.
    if pct_of_max < warning_pct:
        return None
    # SL has already triggered \u2014 caller will fire SL_HIT instead.
    if pct_of_max >= sl_pct:
        return None

    headline = (
        f"\u26a0 Trade is at {pct_of_max:.0f}% of max loss "
        f"(\u20b9{current_pnl:.0f} of \u2013\u20b9{max_loss_rs:.0f})"
    )
    recovery_hint = (
        "Adverse-move advisory:\n"
        f"  \u2022 Current MTM is \u20b9{current_pnl:.0f}, which is "
        f"{pct_of_max:.0f}% of the defined max loss.\n"
        f"  \u2022 Hard SL fires at {sl_pct:.0f}% of max loss \u2014 still room, "
        "but the trade is meaningfully in the red.\n"
        "  \u2022 Consider: roll the threatened side further OTM, take a "
        "partial close on the losing leg, or close the whole structure if "
        "the directional view has changed.\n"
        "  \u2022 Do nothing if you still trust the original thesis and the "
        "move looks like noise."
    )
    return AdverseMoveAdvice(
        severity="MODERATE",
        pnl_pct_of_max_loss=round(pct_of_max, 1),
        headline=headline,
        recovery_hint=recovery_hint,
    )
