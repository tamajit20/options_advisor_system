"""
lifecycle/eod_gap_replay.py
===========================

Reconstruct weekday EOD MTM and exit-engine decisions for open trades over
periods when the live risk monitor had no snapshots (system off / WS down).

Uses FO bhav settle/close prices — intraday path is not available without
live snapshots.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from database.models import FoEodRepo, TradeRepo
from engine.exit_engine import evaluate_exit
from engine.sl_threshold import effective_sl_rs
from lifecycle.data_backfill import weekdays_in_range
from utils import days_between, now_ist, today_ist

logger = logging.getLogger(__name__)

_ACTIONABLE = frozenset({"SL_HIT", "THESIS_FAIL", "TAKE_PROFIT"})


def _next_weekday(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def _as_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    return date.fromisoformat(str(val)[:10])


def _chain_mid_rows(chain_rows: Sequence[Mapping]) -> List[dict]:
    return [
        {
            "strike": float(c["strike"]),
            "option_type": c["option_type"],
            "mid_price": float(c.get("settle_price") or c.get("close_price") or 0.0),
        }
        for c in chain_rows
    ]


def _compute_mtm(
    legs: Sequence[Mapping],
    current_chain: Sequence[Mapping],
    entry_net_credit: float,
) -> float:
    chain_lookup: dict[tuple[float, str], float] = {
        (float(r["strike"]), r["option_type"]): float(r.get("mid_price") or 0.0)
        for r in current_chain
    }
    current_value = 0.0
    for leg in legs:
        key = (float(leg["strike"]), leg["option_type"])
        mid = chain_lookup.get(key, 0.0)
        qty = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
        sign = -1.0 if leg["action"] == "SELL" else 1.0
        current_value += sign * mid * qty
    return entry_net_credit + current_value


def _build_legs_for_engine(
    trade_legs: Sequence[Mapping],
    sug_legs: Sequence[Mapping],
) -> List[dict]:
    by_order = {l["leg_order"]: l for l in trade_legs}
    out: List[dict] = []
    for sl in sug_legs:
        tl = by_order.get(sl["leg_order"])
        if not tl or not tl.get("executed"):
            continue
        out.append({
            "action": sl["action"],
            "strike": float(sl["strike"]),
            "option_type": sl["option_type"],
            "lots": sl["lots"],
            "lot_size": sl["lot_size"],
            "fill_price": tl.get("fill_price"),
        })
    return out


def _latest_snapshot_at(db: SQLServerConnection, trade_id: str) -> Optional[datetime]:
    row = db.fetch_one(
        "SELECT MAX(snapshot_at) AS latest_at FROM ("
        "  SELECT snapshot_at FROM options_trade_mtm_snapshot WHERE trade_id = ? "
        "  UNION ALL "
        "  SELECT snapshot_at FROM options_trade_mtm_snapshot_history WHERE trade_id = ?"
        ") AS s",
        [trade_id, trade_id],
    )
    if not row or row.get("latest_at") is None:
        return None
    val = row["latest_at"]
    return val if isinstance(val, datetime) else datetime.fromisoformat(str(val))


def replay_gap_for_trade(
    db: SQLServerConnection,
    trade_id: str,
    *,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """Return EOD replay payload for one open trade, or ``{"error": ...}``."""
    trd = TradeRepo(db)
    fo = FoEodRepo(db)
    trade = trd.get(trade_id)
    if trade is None:
        return {"error": "not_found"}
    if trade.get("status") in ("CLOSED", "EXPIRED", "VOID"):
        return {"error": "trade_not_open"}

    trade_legs = trd.legs(trade_id)
    sug_legs = db.fetch_all(
        "SELECT * FROM options_suggestion_legs WHERE suggestion_id = ? ORDER BY leg_order",
        [trade["suggestion_id"]],
    )
    if not sug_legs:
        return {"error": "no_suggestion_legs"}

    legs_for_engine = _build_legs_for_engine(trade_legs, sug_legs)
    if not legs_for_engine:
        return {"error": "no_executed_legs"}

    sug_row = db.fetch_one(
        "SELECT strategy FROM options_suggestions WHERE suggestion_id = ?",
        [trade["suggestion_id"]],
    )
    strategy = (sug_row or {}).get("strategy", "") or ""

    underlying = sug_legs[0]["symbol"]
    expiry = _as_date(sug_legs[0]["expiry_date"])
    if expiry is None:
        return {"error": "no_expiry"}

    executed_on = _as_date(trade.get("executed_on"))
    if executed_on is None:
        return {"error": "no_executed_on"}

    end = min(as_of or today_ist(), expiry)
    last_snap = _latest_snapshot_at(db, trade_id)
    if last_snap is not None:
        replay_from = _next_weekday(last_snap.date())
    else:
        replay_from = executed_on

    sl_threshold, sl_label = effective_sl_rs(
        strategy=strategy,
        max_loss_rs=float(trade.get("actual_max_loss") or 0.0),
    )
    lrm_cfg = STRATEGY_CONFIG.get("live_risk_monitor") or {}
    pre_breach_fraction = float(lrm_cfg.get("pre_breach_fraction", 0.70))
    pre_breach_rs = pre_breach_fraction * sl_threshold if sl_threshold > 0 else 0.0

    entry_net_credit = float(trade.get("net_credit_actual") or 0.0)
    max_profit_rs = float(trade.get("actual_max_profit") or 0.0)
    max_loss_rs = float(trade.get("actual_max_loss") or 0.0)

    days_out: List[dict] = []
    first_actionable: Optional[dict] = None

    if replay_from <= end:
        for d in weekdays_in_range(replay_from, end):
            chain_rows = fo.get_chain(underlying, d, expiry)
            if not chain_rows:
                continue
            current_chain = _chain_mid_rows(chain_rows)
            dte = days_between(d, expiry)
            mtm = _compute_mtm(legs_for_engine, current_chain, entry_net_credit)

            decision = evaluate_exit(
                trade_id=trade_id,
                legs=legs_for_engine,
                current_chain=current_chain,
                entry_net_credit=entry_net_credit,
                max_profit_rs=max_profit_rs,
                max_loss_rs=max_loss_rs,
                sl_level_per_share=trade.get("actual_stop_loss_level"),
                days_to_expiry=dte,
                strategy=strategy,
                as_of=datetime.combine(d, datetime.min.time()),
            )

            flags: List[str] = []
            if decision.decision == "TAKE_PROFIT":
                flags.append("target")
            if decision.decision == "THESIS_FAIL":
                flags.append("thesis")
            if decision.decision == "SL_HIT" or (
                sl_threshold > 0 and mtm <= -sl_threshold
            ):
                flags.append("sl_hit")
            elif pre_breach_rs > 0 and mtm <= -pre_breach_rs:
                flags.append("pre_breach")

            day_rec = {
                "date": d.isoformat(),
                "dte": dte,
                "mtm": round(mtm, 2),
                "decision": decision.decision,
                "reason": decision.reason,
                "flags": flags,
                "sl_threshold_rs": round(sl_threshold, 2),
                "pre_breach_rs": round(pre_breach_rs, 2),
            }
            days_out.append(day_rec)
            if first_actionable is None and decision.decision in _ACTIONABLE:
                first_actionable = {
                    "date": d.isoformat(),
                    "decision": decision.decision,
                    "mtm": round(mtm, 2),
                }

    return {
        "trade_id": trade_id,
        "strategy": strategy,
        "underlying": underlying,
        "expiry": expiry.isoformat(),
        "executed_on": executed_on.isoformat(),
        "monitor_last_seen": last_snap.strftime("%Y-%m-%d %H:%M:%S") if last_snap else None,
        "replay_from": replay_from.isoformat() if replay_from <= end else None,
        "replay_through": end.isoformat(),
        "sl_threshold_rs": round(sl_threshold, 2),
        "sl_label": sl_label,
        "pre_breach_fraction": pre_breach_fraction,
        "days": days_out,
        "first_actionable": first_actionable,
        "has_gap": bool(days_out),
        "disclaimer": (
            "EOD settle prices only. Intraday SL/target crossings while the "
            "monitor was off may not appear here."
        ),
    }
