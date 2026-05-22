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


from config import STRATEGY_CONFIG
from engine.sl_threshold import effective_sl_rs


def assess_adverse_move(
    *,
    current_pnl: float,
    max_loss_rs: float,
    strategy: str = "",
    warning_pct: Optional[float] = None,
    sl_pct: Optional[float] = None,
) -> Optional[AdverseMoveAdvice]:
    """Pure check. Returns advice if the trade is in the warning band."""
    if max_loss_rs <= 0:
        return None
    if current_pnl >= 0:
        return None  # winning or flat

    sl_threshold, sl_label = effective_sl_rs(strategy=strategy, max_loss_rs=max_loss_rs)
    if sl_threshold <= 0:
        return None

    lrm = STRATEGY_CONFIG.get("live_risk_monitor") or {}
    warn_frac = (
        float(warning_pct) / 100.0 if warning_pct is not None
        else float(lrm.get("pre_breach_fraction", 0.70))
    )
    sl_frac = (
        float(sl_pct) / 100.0 if sl_pct is not None
        else 1.0
    )

    loss = abs(current_pnl)
    pct_of_sl = loss / sl_threshold * 100.0

    if loss < warn_frac * sl_threshold:
        return None
    if loss >= sl_frac * sl_threshold:
        return None

    headline = (
        f"\u26a0 Trade is at {pct_of_sl:.0f}% of SL threshold "
        f"(\u20b9{current_pnl:.0f} of \u2013\u20b9{sl_threshold:.0f}, {sl_label})"
    )
    recovery_hint = (
        "Adverse-move advisory:\n"
        f"  \u2022 Current MTM is \u20b9{current_pnl:.0f}, which is "
        f"{pct_of_sl:.0f}% of the effective SL threshold (₹{sl_threshold:,.0f}).\n"
        f"  \u2022 Hard SL fires at ₹{sl_threshold:,.0f} ({sl_label}).\n"
        "  \u2022 Consider: roll the threatened side further OTM, take a "
        "partial close on the losing leg, or close the whole structure if "
        "the directional view has changed.\n"
        "  \u2022 Do nothing if you still trust the original thesis and the "
        "move looks like noise."
    )
    return AdverseMoveAdvice(
        severity="MODERATE",
        pnl_pct_of_max_loss=round(pct_of_sl, 1),
        headline=headline,
        recovery_hint=recovery_hint,
    )
