"""
lifecycle/trade_executor.py
===========================

User-driven flow: marks a suggestion as executed, given the actual fills.

Inputs:
    - suggestion_id
    - list of TradeLegFill (from contracts) — one per leg

Logic:
    1. Validate fills against suggestion legs
    2. Classify position_type:
       FULL_VALID  — every leg filled → trade is the suggested structure
       PAIRED      — at least one full hedge pair filled (short+matching long)
       NAKED       — at least one short filled with NO hedge
       VOID        — nothing filled (no trade created)
    3. Compute actual net credit from fills
    4. Insert TradeRow + leg rows
    5. Update suggestion status to EXECUTED / IGNORED
    6. If broken (PAIRED or NAKED), trigger broken_trade_advisor (returns options
       saved into broken_state_json for dashboard)

Returns the new trade_id, or None if VOID.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

from contracts import TradeLegFill
from database.connection import SQLServerConnection
from database.models import SuggestionRepo, TradeRepo
from database.runtime_flags import FLAG_CIRCUIT_BREAKER_ACTIVE, RuntimeFlagsRepo
from engine.broken_trade_advisor import advise, diagnose
from engine.charges import estimate_charges_per_txn
from engine.execution_validator import validate_execution
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)


def _fills_from_suggested(legs: Sequence[dict]) -> list[TradeLegFill]:
    """Build executed fills from each leg's suggested_price (midpoint)."""
    out: list[TradeLegFill] = []
    for leg in legs:
        price = leg.get("suggested_price")
        if price is None:
            continue
        out.append(TradeLegFill(
            leg_order=int(leg["leg_order"]),
            executed=True,
            fill_price=float(price),
            fill_time=now_ist(),
        ))
    return out


def _fill_width_scale(
    suggestion_legs: Sequence[dict],
    executed_legs: Sequence[dict],
) -> float:
    """fill qty / suggestion qty from max(lots × lot_size). Default 1.0."""
    def _max_qty(rows: Sequence[dict], lots_key: str) -> int:
        best = 0
        for row in rows:
            lots = row.get(lots_key)
            if lots is None:
                lots = row.get("lots") or 0
            try:
                qty = int(lots) * int(row.get("lot_size") or 0)
            except (TypeError, ValueError):
                continue
            if qty > best:
                best = qty
        return best

    sug_qty = _max_qty(suggestion_legs, "lots")
    fill_qty = _max_qty(executed_legs, "lots_actual")
    if sug_qty <= 0 or fill_qty <= 0:
        return 1.0
    return fill_qty / sug_qty


def _actual_max_profit(
    suggestion: dict,
    actual_net_credit: float,
    width_scale: float = 1.0,
) -> Optional[float]:
    """Recompute rupee max-profit from fills; fall back to suggestion economics."""
    sug_mp = suggestion.get("max_profit")
    sug_ml = suggestion.get("max_loss")
    try:
        if actual_net_credit > 0:
            return float(actual_net_credit)
        if actual_net_credit < 0 and sug_mp is not None and sug_ml is not None:
            width = (float(sug_mp) + float(sug_ml)) * float(width_scale)
            return max(0.0, width - abs(actual_net_credit))
    except (TypeError, ValueError):
        pass
    return sug_mp


def _actual_max_loss(
    suggestion: dict,
    actual_net_credit: float,
    width_scale: float = 1.0,
) -> Optional[float]:
    sug_mp = suggestion.get("max_profit")
    sug_ml = suggestion.get("max_loss")
    try:
        if sug_mp is None or sug_ml is None:
            return sug_ml
        width = (float(sug_mp) + float(sug_ml)) * float(width_scale)
        if actual_net_credit > 0:
            return max(0.0, width - actual_net_credit)
        if actual_net_credit < 0:
            return abs(actual_net_credit)
    except (TypeError, ValueError):
        pass
    return sug_ml


def _actual_breakevens(
    suggestion: dict,
    executed_legs: Sequence[dict],
) -> tuple[Optional[float], Optional[float]]:
    """Recompute BEs from fill prices; fall back to suggestion BEs."""
    fallback = (suggestion.get("upper_breakeven"), suggestion.get("lower_breakeven"))
    strategy = suggestion.get("strategy") or ""
    if not strategy or not executed_legs:
        return fallback
    try:
        from engine.exit_pricing import expiry_date as _parse_expiry
        from engine.leg_builder import breakevens
        from engine.live_expectation import legs_from_fills

        exp = _parse_expiry(suggestion.get("expiry_date"))
        if exp is None:
            exp = _parse_expiry(executed_legs[0].get("expiry_date"))
        if exp is None:
            exp = today_ist()
        underlying = str(
            suggestion.get("underlying")
            or executed_legs[0].get("symbol")
            or ""
        )
        fill_rows = []
        for row in executed_legs:
            copied = dict(row)
            if copied.get("lots_actual"):
                copied["lots"] = copied["lots_actual"]
            fill_rows.append(copied)
        fill_legs = legs_from_fills(fill_rows, underlying=underlying, expiry=exp)
        ub, lb = breakevens(fill_legs, strategy)
        if ub is not None or lb is not None:
            return ub, lb
    except Exception:
        logger.debug("trade_executor: fill breakeven recompute failed", exc_info=True)
    return fallback


def _require_positive_lot_size(legs: Sequence[dict], *, context: str) -> None:
    for leg in legs:
        try:
            lot_size = int(leg.get("lot_size") or 0)
        except (TypeError, ValueError):
            lot_size = 0
        if lot_size <= 0:
            raise ValueError(
                f"{context}: leg {leg.get('leg_order')} has missing or invalid lot_size"
            )


def _circuit_breaker_on(db: SQLServerConnection) -> bool:
    try:
        return RuntimeFlagsRepo(db).get_bool(
            FLAG_CIRCUIT_BREAKER_ACTIVE, default=False,
        )
    except Exception:
        logger.exception("trade_executor: circuit_breaker flag read failed")
        raise ValueError(
            "Execution blocked: circuit-breaker flag could not be read"
        ) from None


def mark_executed(
    db: SQLServerConnection,
    suggestion_id: str,
    fills: Sequence[TradeLegFill],
    spot_at_execution: Optional[float] = None,
    actual_stop_loss_level: Optional[float] = None,
    *,
    execute_at_suggested: bool = False,
    skip_execution_gate: bool = False,
    execution_provider: Optional[str] = None,
) -> Optional[str]:
    sug = SuggestionRepo(db)
    trd = TradeRepo(db)

    suggestion = sug.get(suggestion_id)
    if suggestion is None:
        raise ValueError(f"Unknown suggestion: {suggestion_id}")
    legs = sug.legs(suggestion_id)
    if not legs:
        raise ValueError(f"Suggestion {suggestion_id} has no legs")

    has_executed_fills = any(
        f.executed and f.fill_price is not None for f in fills
    )

    # User clicked Ignore (no fills) — dismiss without execution gates.
    if not has_executed_fills and not execute_at_suggested and not skip_execution_gate:
        status = (suggestion.get("status") or "").upper()
        if status != "PENDING":
            raise ValueError(f"Cannot ignore suggestion with status {status}")
        sug.update_status(suggestion_id, "IGNORED")
        db.commit()
        logger.info("Suggestion %s marked IGNORED by user", suggestion_id)
        return None

    status = (suggestion.get("status") or "").upper()
    if status != "PENDING":
        raise ValueError(f"Cannot execute suggestion with status {status}")

    from database.broker_order_repo import BrokerOrderRepo
    broker = BrokerOrderRepo(db)
    pending_orders = broker.pending_for_suggestion(suggestion_id)
    if pending_orders:
        raise ValueError(
            "Zerodha orders are already in flight for this suggestion — "
            "wait or cancel on Kite before recording a manual fill"
        )
    orphans = broker.orphan_entry_fills(suggestion_id)
    if orphans:
        raise ValueError(
            "Prior Zerodha entry fills exist without a recorded trade — "
            "flatten on Kite before recording a manual fill"
        )

    cb_active = _circuit_breaker_on(db)
    gate = validate_execution(
        suggestion, legs, circuit_breaker_active=cb_active,
    )
    if cb_active and not gate.ok:
        raise ValueError(f"Execution blocked: {gate.reason()}")

    if execute_at_suggested:
        fills = _fills_from_suggested(legs)
        if not fills:
            raise ValueError("No suggested prices on legs — cannot record at suggested values")
        if len(fills) != len(legs):
            raise ValueError("Every leg must have a suggested_price to record at suggested values")
        if spot_at_execution is None and suggestion.get("spot_at_generation") is not None:
            spot_at_execution = float(suggestion["spot_at_generation"])
        if actual_stop_loss_level is None:
            actual_stop_loss_level = suggestion.get("stop_loss_level")
        has_executed_fills = True
    elif not has_executed_fills:
        if skip_execution_gate:
            raise ValueError("Enter a fill price for at least one leg before Execute")
        sug.update_status(suggestion_id, "IGNORED")
        db.commit()
        logger.info("Suggestion %s marked IGNORED — no fills", suggestion_id)
        return None
    elif skip_execution_gate:
        if spot_at_execution is None and suggestion.get("spot_at_generation") is not None:
            spot_at_execution = float(suggestion["spot_at_generation"])
        if actual_stop_loss_level is None:
            actual_stop_loss_level = suggestion.get("stop_loss_level")

    if gate.ok:
        # Client skip / execute-at-suggested must not bypass a passing gate.
        gate_passed = True
    elif cb_active:
        raise ValueError(f"Execution blocked: {gate.reason()}")
    elif execute_at_suggested or skip_execution_gate:
        # Stale / vetoed UI: record fills (suggested or typed) with gate_passed=False.
        gate_passed = False
    else:
        logger.warning(
            "Execution blocked for %s: %s", suggestion_id, gate.reason()
        )
        raise ValueError(f"Execution blocked: {gate.reason()}")

    executed_on = now_ist()
    for f in fills:
        if f.executed and f.fill_price is not None:
            f.fill_time = executed_on

    fills_by_order = {f.leg_order: f for f in fills}

    executed_legs = []
    not_executed_legs = []
    actual_net_credit = 0.0
    for leg in legs:
        f = fills_by_order.get(leg["leg_order"])
        if f and f.executed and f.fill_price is not None:
            lots_used = (f.lots_override if f and f.lots_override else leg["lots"]) or 0
            executed_legs.append({
                **leg,
                "fill_price": f.fill_price,
                "lots_actual": lots_used,
            })
            sign = 1.0 if leg["action"] == "SELL" else -1.0
            actual_net_credit += sign * f.fill_price * lots_used * (leg["lot_size"] or 0)
        else:
            not_executed_legs.append(leg)

    _require_positive_lot_size(executed_legs, context="mark_executed")

    if not executed_legs:
        # Should not happen — empty fills handled above.
        sug.update_status(suggestion_id, "IGNORED")
        db.commit()
        logger.info("Suggestion %s marked IGNORED — no fills", suggestion_id)
        return None

    state = diagnose(executed_legs, not_executed_legs)
    if state == "FULL":
        position_type = "FULL_VALID"
    elif state in ("NAKED_LONG",):
        position_type = "NAKED_LONG"
    elif state == "NAKED_SHORT":
        position_type = "NAKED"
    elif state == "PAIRED_BROKEN":
        position_type = "PAIRED"
    else:
        position_type = "PARTIAL"

    broken_options = []
    if state != "FULL":
        # We can't price advise without a current chain — store the diagnostic state only
        broken_options = [
            {"rank": o.rank, "label": o.label, "recommended": o.recommended,
             "estimated_pnl": o.estimated_pnl, "when_to_use": o.when_to_use,
             "zerodha_steps": o.zerodha_steps, "time_sensitivity": o.time_sensitivity}
            for o in advise(state=state, executed_legs=executed_legs,
                            not_executed_legs=not_executed_legs,
                            spot=0.0, current_chain=[])
        ]

    width_scale = _fill_width_scale(legs, executed_legs)
    actual_ub, actual_lb = _actual_breakevens(suggestion, executed_legs)

    trade_id = trd.next_trade_id(today_ist())
    trd.insert({
        "trade_id":        trade_id,
        "suggestion_id":   suggestion_id,
        "trade_name":      suggestion.get("trade_name"),
        "executed_on":     executed_on,
        "position_type":   position_type,
        "net_credit_actual": actual_net_credit,
        "actual_max_profit":      _actual_max_profit(
            suggestion, actual_net_credit, width_scale=width_scale,
        ),
        "actual_max_loss":        _actual_max_loss(
            suggestion, actual_net_credit, width_scale=width_scale,
        ),
        "actual_upper_breakeven": actual_ub,
        "actual_lower_breakeven": actual_lb,
        "actual_stop_loss_level": actual_stop_loss_level if actual_stop_loss_level is not None else suggestion.get("stop_loss_level"),
        "spot_at_execution": spot_at_execution,
        "status":          "ACTIVE",
        "daily_status":    "OPEN",
        "exit_instruction": None,
        "broken_state_json": json.dumps({"state": state, "options": broken_options})
                              if state != "FULL" else None,
        "gross_pnl":       0.0,
        "total_charges":   0.0,
        "net_pnl":         0.0,
        "closed_on":       None,
    })

    trade_legs = []
    for leg in legs:
        f = fills_by_order.get(leg["leg_order"])
        executed = bool(f and f.executed and f.fill_price is not None)
        lots_actual = (f.lots_override if (f and f.lots_override) else leg["lots"]) if executed else None
        trade_legs.append({
            "suggestion_leg_id": leg.get("id"),
            "leg_order":         leg["leg_order"],
            "executed":          executed,
            "fill_price":        f.fill_price if (f and executed) else None,
            "fill_time":         f.fill_time  if (f and executed) else None,
            "not_filled_reason": (f.not_filled_reason if f else "Not selected by user")
                                 if not executed else None,
            "exit_price":        None,
            "exit_time":         None,
            "leg_pnl":           None,
            "leg_charges":       None,
            "lots_actual":       lots_actual,
        })
    trd.insert_legs(trade_id, trade_legs)
    sug.update_status(suggestion_id, "EXECUTED")
    # Phase 2c: stamp execution provenance (best-effort).
    try:
        gen_on = suggestion.get("generated_on")
        time_from = None
        if gen_on is not None:
            try:
                time_from = int((now_ist() - gen_on).total_seconds())
            except Exception:
                time_from = None
        trd.write_execution_provenance(
            trade_id,
            execution_data_source=suggestion.get("data_source"),
            execution_provider=execution_provider or suggestion.get("provider"),
            gate_passed=gate_passed,
            time_from_suggestion_sec=time_from,
        )
    except Exception:
        logger.exception(
            "trade_executor: write_execution_provenance failed for %s",
            trade_id,
        )
    db.commit()
    logger.info("Trade %s created: %s (%s)", trade_id, position_type, state)
    # Notify the live risk monitor (and any other listener) that a trade
    # was opened so it can refresh its watchlist immediately.
    try:
        from providers.event_bus import TOPIC_TRADE_OPENED, get_event_bus
        get_event_bus().publish(TOPIC_TRADE_OPENED, {"trade_id": trade_id})
    except Exception:
        logger.exception("trade_executor: TOPIC_TRADE_OPENED publish failed")
    return trade_id


def supplement_trade(
    db: SQLServerConnection,
    trade_id: str,
    fills: Sequence[TradeLegFill],
) -> None:
    """Fill remaining (unexecuted) legs on an existing partial/broken trade."""
    trd = TradeRepo(db)

    trade = trd.get(trade_id)
    if trade is None:
        raise ValueError(f"Unknown trade: {trade_id}")

    # Current legs (with suggestion info for full context)
    all_legs = trd.legs_with_suggestion_info(trade_id)

    fills_by_order = {f.leg_order: f for f in fills}

    # Apply new fills — never overwrite a leg that is already executed.
    for leg in all_legs:
        if leg.get("executed"):
            continue
        f = fills_by_order.get(leg["leg_order"])
        if f and f.executed and f.fill_price is not None:
            lots_actual = (f.lots_override if f.lots_override else leg["lots"]) or None
            trd.update_leg_fill(
                trade_id, leg["leg_order"], f.fill_price,
                f.fill_time or now_ist(), lots_actual
            )

    # Recompute net credit over ALL legs (now updated)
    updated_legs = trd.legs_with_suggestion_info(trade_id)
    new_credit = 0.0
    executed_updated = []
    not_executed_updated = []
    for leg in updated_legs:
        if leg["executed"]:
            executed_updated.append(leg)
            sign = 1.0 if leg["action"] == "SELL" else -1.0
            lots_used = (leg.get("lots_actual") or leg["lots"] or 0)
            new_credit += sign * float(leg["fill_price"] or 0) * int(lots_used) * int(leg["lot_size"] or 0)
        else:
            not_executed_updated.append(leg)

    _require_positive_lot_size(executed_updated, context="supplement_trade")

    # Reclassify position
    state = diagnose(executed_updated, not_executed_updated)
    if state == "FULL":
        position_type = "FULL_VALID"
    elif state in ("NAKED_LONG",):
        position_type = "NAKED_LONG"
    elif state == "NAKED_SHORT":
        position_type = "NAKED"
    elif state == "PAIRED_BROKEN":
        position_type = "PAIRED"
    else:
        position_type = "PARTIAL"

    broken_options = []
    if state != "FULL":
        broken_options = [
            {"rank": o.rank, "label": o.label, "recommended": o.recommended,
             "estimated_pnl": o.estimated_pnl, "when_to_use": o.when_to_use,
             "zerodha_steps": o.zerodha_steps, "time_sensitivity": o.time_sensitivity}
            for o in advise(state=state, executed_legs=executed_updated,
                            not_executed_legs=not_executed_updated,
                            spot=0.0, current_chain=[])
        ]

    broken_json = (json.dumps({"state": state, "options": broken_options})
                   if state != "FULL" else None)
    trd.update_position(trade_id, new_credit, position_type, broken_json)
    db.commit()


def close_trade_with_fills(
    db: SQLServerConnection,
    trade_id: str,
    exits: list,
) -> None:
    """Record closing fills for each executed leg and mark trade as CLOSED.

    exits: list of dicts — {leg_order: int, exit_price: float, exit_time: datetime|None}

    P&L per leg:
        SELL leg: received fill_price on open, paid exit_price to close
                  leg_pnl = (fill_price - exit_price) * lots * lot_size
        BUY  leg: paid fill_price on open, received exit_price on close
                  leg_pnl = (exit_price - fill_price) * lots * lot_size
        Unified:  leg_pnl = sign * (fill_price - exit_price) * lots * lot_size
                  where sign = +1 for SELL, -1 for BUY
    """
    trd = TradeRepo(db)

    trade = trd.get(trade_id)
    if trade is None:
        raise ValueError(f"Unknown trade: {trade_id}")
    if str(trade.get("status") or "").upper() in ("CLOSED", "VOID", "EXPIRED"):
        raise ValueError(
            f"Trade status is {trade.get('status')!r} — cannot close again"
        )

    all_legs = trd.legs_with_suggestion_info(trade_id)
    executed_legs = [l for l in all_legs if l["executed"]]
    if not executed_legs:
        raise ValueError(f"Trade {trade_id} has no executed legs to close")

    exits_by_order = {e["leg_order"]: e for e in exits}
    missing = [
        int(l["leg_order"]) for l in executed_legs
        if exits_by_order.get(l["leg_order"]) is None
        or exits_by_order[l["leg_order"]].get("exit_price") is None
    ]
    if missing:
        raise ValueError(
            "Close requires an exit price for every executed leg — missing "
            f"leg(s) {', '.join(str(lo) for lo in missing)}"
        )
    gross_pnl = 0.0
    txn_legs: list = []  # all actual buy/sell transactions for charge calculation

    for leg in executed_legs:
        e = exits_by_order.get(leg["leg_order"])
        if e and e.get("exit_price") is not None:
            sign = 1.0 if leg["action"] == "SELL" else -1.0
            lots = int(leg.get("lots_actual") or leg["lots"] or 0)
            lot_size = int(leg["lot_size"] or 0)
            leg_pnl = sign * (float(leg["fill_price"] or 0) - float(e["exit_price"])) * lots * lot_size
            gross_pnl += leg_pnl
            trd.update_leg_exit(
                trade_id, leg["leg_order"],
                float(e["exit_price"]),
                e.get("exit_time") or now_ist(),
                leg_pnl,
            )
            # Entry transaction
            txn_legs.append({"action": leg["action"],
                              "price": float(leg["fill_price"] or 0),
                              "lots": lots, "lot_size": lot_size})
            # Exit transaction (reversed action)
            close_action = "BUY" if leg["action"] == "SELL" else "SELL"
            txn_legs.append({"action": close_action,
                              "price": float(e["exit_price"]),
                              "lots": lots, "lot_size": lot_size})

    total_charges = estimate_charges_per_txn(txn_legs).total if txn_legs else 0.0
    net_pnl = gross_pnl - total_charges
    trd.close_trade(trade_id, gross_pnl, total_charges, net_pnl)
    db.commit()
    logger.info("Trade %s closed, gross_pnl=%.2f, charges=%.2f, net_pnl=%.2f",
                trade_id, gross_pnl, total_charges, net_pnl)
