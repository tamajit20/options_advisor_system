"""
simulation/simulator.py
=======================

Tracks how a suggested trade WOULD have performed if executed at the
suggested price. Updates daily as new EOD data comes in.

Day 1 entry classification:
    FULL_VALID — actual open is within suggested_price_low..high
    ADJUSTED   — open is outside band but ≤ adjusted_max_gap_pct% gap;
                 use actual open as the entry price
    VOID       — open is >adjusted_max_gap_pct% gap; mark sim void

After day 1, each day:
    - Track open/high/low/settle
    - Compute mark-to-market P&L using settle
    - On expiry day, mark final_settle and close

Boundary: this is a separate package; uses database + engine but NOT downloader.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional, Sequence

from config import SIMULATION_CONFIG
from contracts import SimulationDayUpdate
from database.connection import SQLServerConnection
from database.models import (
    FoEodRepo,
    SimulationRepo,
    SuggestionRepo,
)
from utils import days_between, today_ist

logger = logging.getLogger(__name__)


def _classify_day1(
    suggested: float,
    sug_low: float,
    sug_high: float,
    actual_open: float,
) -> tuple[str, str, Optional[float]]:
    """Return (quality, note, sim_entry_price)."""
    if sug_low > 0 and sug_high > 0 and sug_low <= actual_open <= sug_high:
        return "FULL_VALID", "Open within suggested band", suggested
    if suggested > 0:
        gap_pct = abs(actual_open - suggested) / suggested * 100.0
        max_gap = SIMULATION_CONFIG["adjusted_max_gap_pct"]
        if gap_pct <= max_gap:
            return ("ADJUSTED",
                    f"Gap {gap_pct:.1f}% (≤{max_gap:.0f}%) — using actual open",
                    actual_open)
        return ("VOID",
                f"Gap {gap_pct:.1f}% exceeds {max_gap:.0f}%",
                None)
    return "VOID", "Suggested price was zero", None


def _compute_day_pnl(
    legs_state: Sequence[dict],
    chain_today: Sequence[dict],
) -> float:
    """Sum of MTM across legs at settle."""
    chain_lookup = {(float(c["strike"]), c["option_type"]):
                    float(c.get("settle_price") or c.get("close_price") or 0.0)
                    for c in chain_today}
    total = 0.0
    for leg in legs_state:
        if leg.get("sim_entry_price") is None:
            continue
        mid = chain_lookup.get((float(leg["strike"]), leg["option_type"]), 0.0)
        qty = int(leg["lots"]) * int(leg["lot_size"])
        sign = 1.0 if leg["action"] == "SELL" else -1.0
        # Credit from entry: + sign × entry × qty
        # Cost to close at settle: + sign × (entry − mid) × qty (signs work out)
        total += sign * (float(leg["sim_entry_price"]) - mid) * qty
    return total


def update_simulation(
    db: SQLServerConnection,
    suggestion_id: str,
    sim_date: date | None = None,
) -> bool:
    """Run simulation update for one suggestion as-of `sim_date`. Returns True
    if any work was done (i.e. trade not already void/closed)."""
    sim_date = sim_date or today_ist()
    sug = SuggestionRepo(db)
    sim = SimulationRepo(db)
    fo = FoEodRepo(db)

    suggestion = sug.get(suggestion_id)
    if suggestion is None:
        return False
    legs = sug.legs(suggestion_id)
    if not legs:
        return False

    # Don't simulate NONE / NO_SUGGESTION
    if suggestion.get("strategy") in (None, "NONE"):
        return False

    sim.ensure_simulation_row(suggestion_id, started_on=sim_date)
    summary = sim.get_summary(suggestion_id)
    if summary and summary.get("overall_quality") in ("VOID", "CLOSED"):
        return False

    underlying = legs[0]["symbol"]
    expiry = legs[0]["expiry_date"]
    is_expiry_day = sim_date == expiry

    chain = fo.get_chain(underlying, sim_date, expiry)
    if not chain:
        logger.warning("Sim %s: no chain on %s — skip", suggestion_id, sim_date)
        return False

    chain_lookup = {(float(c["strike"]), c["option_type"]): c for c in chain}

    # Existing legs in simulation (carry forward sim_entry_price)
    existing = {l["leg_order"]: l for l in sim.get_legs(suggestion_id)}
    is_first_day = not existing

    legs_state: List[dict] = []
    for leg in legs:
        key = (float(leg["strike"]), leg["option_type"])
        c = chain_lookup.get(key)
        if c is None:
            continue
        open_p = float(c.get("open_price") or 0.0)
        high_p = float(c.get("high_price") or 0.0)
        low_p  = float(c.get("low_price") or 0.0)
        settle = float(c.get("settle_price") or c.get("close_price") or 0.0)

        sim_entry = None
        quality = "FULL_VALID"
        note = ""
        if is_first_day:
            quality, note, sim_entry = _classify_day1(
                suggested=float(leg["suggested_price"]),
                sug_low=float(leg["suggested_price_low"] or 0.0),
                sug_high=float(leg["suggested_price_high"] or 0.0),
                actual_open=open_p,
            )
        else:
            prior = existing.get(leg["leg_order"])
            if prior:
                sim_entry = prior.get("sim_entry_price")
                quality = prior.get("quality") or "FULL_VALID"
                note = prior.get("adjustment_note") or ""

        legs_state.append({
            **leg,
            "open_price":       open_p,
            "high_price":       high_p,
            "low_price":        low_p,
            "settle_price":     settle,
            "sim_entry_price":  sim_entry,
            "quality":          quality,
            "adjustment_note":  note,
        })

    # If any leg VOID on first day → mark whole sim VOID
    if is_first_day and any(l["quality"] == "VOID" for l in legs_state):
        for leg in legs_state:
            u = SimulationDayUpdate(
                suggestion_id=suggestion_id,
                leg_order=leg["leg_order"],
                sim_date=sim_date,
                suggested_price=float(leg["suggested_price"]),
                sim_entry_price=None,
                open_price=leg["open_price"],
                high_price=leg["high_price"],
                low_price=leg["low_price"],
                settle_price=leg["settle_price"],
                quality="VOID",
                adjustment_note="Day-1 entry gap too large",
                day_pnl=0.0,
                cumulative_pnl=0.0,
                is_expiry_day=is_expiry_day,
                final_settle=None,
            )
            sim.upsert_leg_day(u)
        sim.update_summary(
            suggestion_id, completed_on=sim_date, overall_quality="VOID",
            sim_net_credit=0.0, sim_final_pnl=0.0, sim_charges=0.0, sim_net_pnl=0.0,
            notes="Voided on entry — gap exceeded threshold",
        )
        db.commit()
        return True

    # Normal day
    day_pnl = _compute_day_pnl(legs_state, chain)
    prior_cum = float(summary.get("sim_final_pnl") or 0.0) if summary else 0.0
    cum_pnl = day_pnl  # MTM is full PnL since entry — not delta
    for leg in legs_state:
        u = SimulationDayUpdate(
            suggestion_id=suggestion_id,
            leg_order=leg["leg_order"],
            sim_date=sim_date,
            suggested_price=float(leg["suggested_price"]),
            sim_entry_price=leg["sim_entry_price"],
            open_price=leg["open_price"],
            high_price=leg["high_price"],
            low_price=leg["low_price"],
            settle_price=leg["settle_price"],
            quality=leg["quality"],
            adjustment_note=leg["adjustment_note"],
            day_pnl=day_pnl,
            cumulative_pnl=cum_pnl,
            is_expiry_day=is_expiry_day,
            final_settle=leg["settle_price"] if is_expiry_day else None,
        )
        sim.upsert_leg_day(u)

    if is_expiry_day:
        worst_quality = "FULL_VALID"
        for leg in legs_state:
            if leg["quality"] == "ADJUSTED" and worst_quality == "FULL_VALID":
                worst_quality = "ADJUSTED"
        sim.update_summary(
            suggestion_id, completed_on=sim_date, overall_quality=worst_quality,
            sim_net_credit=0.0, sim_final_pnl=cum_pnl,
            sim_charges=0.0, sim_net_pnl=cum_pnl,
            notes="Closed at expiry settle",
        )
    else:
        sim.update_summary(
            suggestion_id, completed_on=None, overall_quality="ACTIVE",
            sim_net_credit=0.0, sim_final_pnl=cum_pnl,
            sim_charges=0.0, sim_net_pnl=cum_pnl,
            notes="",
        )

    db.commit()
    return True


def run_simulation_update(db: SQLServerConnection, sim_date: date | None = None) -> int:
    """Run simulation update for every active suggestion (today and recent)."""
    sim_date = sim_date or today_ist()
    sug = SuggestionRepo(db)
    # Pick everything generated in the last ~30 days that's not closed
    rows = db.fetch_all(
        "SELECT suggestion_id FROM options_suggestions "
        "WHERE generated_on >= ? AND status NOT IN ('NO_SUGGESTION') "
        "ORDER BY generated_on DESC",
        [sim_date - timedelta(days=30)],
    )
    updated = 0
    for r in rows:
        try:
            if update_simulation(db, r["suggestion_id"], sim_date):
                updated += 1
        except Exception:
            logger.exception("Simulation update failed for %s", r["suggestion_id"])
    logger.info("Simulation: %d suggestions updated", updated)
    return updated
