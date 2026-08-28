"""
lifecycle/intraday_sl_fallback.py
=================================

WS-down fallback for open-trade SL / target monitoring.

Runs **only** from the scheduler when ``providers.ws_health.is_ws_unhealthy``
reports that live WebSocket ticks are stale or absent. When WS is healthy,
``LiveRiskMonitor`` handles all intraday risk — this job exits immediately.

Uses ``get_market_data().get_chain()`` (Zerodha REST → NSE live → EOD) to
price legs and ``engine.exit_engine.evaluate_exit`` for the same decision
logic as the live monitor and EOD exit engine.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from config import PROVIDERS_CONFIG, STRATEGY_CONFIG
from contracts import Notification
from database.connection import SQLServerConnection
from database.models import NotificationRepo, TradeGreeksRepo, TradeRepo
from engine.exit_engine import evaluate_exit
from engine.greeks_exit import greeks_stress_check
from engine.sl_threshold import effective_sl_rs
from lifecycle.snapshot_orchestrator import _chain_index, _row_ltp
from providers.registry import get_market_data
from providers.ws_health import is_ws_unhealthy
from utils import days_between, now_ist, today_ist

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(
    os.environ.get("OPT_DATA_DIR", "data"),
    "sl_fallback_state.json",
)


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE) or ".", exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, separators=(",", ":"))
    os.replace(tmp, _STATE_FILE)


def _cfg() -> dict:
    return STRATEGY_CONFIG.get("intraday_sl_fallback") or {}


def _should_run(now: datetime) -> Tuple[bool, str]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return False, "disabled"
    lrm = STRATEGY_CONFIG.get("live_risk_monitor") or {}
    if not lrm.get("enabled", True):
        return False, "live_risk_monitor_disabled"
    if str(PROVIDERS_CONFIG.get("active") or "").lower() != "zerodha":
        return False, "non_zerodha_provider"

    unhealthy, reason = is_ws_unhealthy(now)
    if not unhealthy:
        state = _load_state()
        state["unhealthy_streak"] = 0
        _save_state(state)
        return False, reason

    min_streak = int(cfg.get("min_unhealthy_streak", 2))
    state = _load_state()
    streak = int(state.get("unhealthy_streak") or 0) + 1
    state["unhealthy_streak"] = streak
    state["last_unhealthy_reason"] = reason
    state["last_check_at"] = now.isoformat(timespec="seconds")
    _save_state(state)

    if streak < min_streak:
        return False, f"unhealthy_streak={streak}/{min_streak} ({reason})"

    return True, reason


def _net_credit(trade: dict, legs: List[dict]) -> float:
    nc = trade.get("net_credit_actual")
    if nc is not None:
        try:
            return float(nc)
        except (TypeError, ValueError):
            pass
    total = 0.0
    for leg in legs:
        if not leg.get("executed") or leg.get("fill_price") is None:
            continue
        fp = float(leg["fill_price"])
        lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
        lot_size = int(leg.get("lot_size") or 1)
        qty = lots * lot_size
        sign = 1.0 if str(leg.get("action") or "").upper() == "SELL" else -1.0
        total += sign * fp * qty
    return total


def _evaluate_trade(
    db: SQLServerConnection,
    trade: dict,
    *,
    trade_date: date,
    chains_cache: Dict[tuple, Dict[tuple, dict]],
    provider,
    now: datetime,
) -> Optional[str]:
    """Return notification body if an alert should fire, else None."""
    trade_id = trade["trade_id"]
    legs_all = TradeRepo(db).legs_with_suggestion_info(trade_id)
    legs = [l for l in legs_all if l.get("executed") and l.get("fill_price") is not None]
    if not legs:
        return None

    strategy = str(legs[0].get("strategy") or trade.get("strategy") or "")
    expiry = legs[0].get("expiry_date")
    if expiry is None:
        return None

    chain_key = (str(legs[0]["symbol"]), expiry)
    if chain_key not in chains_cache:
        try:
            rows = provider.get_chain(chain_key[0], trade_date, expiry)
        except Exception as exc:
            logger.warning(
                "intraday_sl_fallback: get_chain(%s) failed: %s",
                chain_key[0], exc,
            )
            rows = []
        chains_cache[chain_key] = _chain_index(rows)

    idx = chains_cache[chain_key]
    current_chain = []
    leg_ltps = {}
    for leg in legs:
        key = (float(leg["strike"]), str(leg["option_type"]).upper())
        row = idx.get(key)
        ltp = _row_ltp(row) if row else None
        if ltp is None:
            return None
        current_chain.append({
            "strike": float(leg["strike"]),
            "option_type": str(leg["option_type"]).upper(),
            "mid_price": ltp,
        })
        leg_ltps[key] = ltp

    legs_for_engine = [
        {
            "action": leg["action"],
            "strike": float(leg["strike"]),
            "option_type": leg["option_type"],
            "fill_price": float(leg["fill_price"]),
            "lots": int(leg.get("lots_actual") or leg.get("lots") or 1),
            "lot_size": int(leg.get("lot_size") or 1),
        }
        for leg in legs
    ]

    max_profit = float(trade.get("actual_max_profit") or 0.0)
    max_loss = float(trade.get("actual_max_loss") or 0.0)
    entry_credit = _net_credit(trade, legs)
    dte = max(days_between(trade_date, expiry), 0)
    sl_level = trade.get("actual_stop_loss_level")
    sl_level_f = float(sl_level) if sl_level is not None else None

    greeks_row = TradeGreeksRepo(db).latest_for_trade(trade_id)

    decision = evaluate_exit(
        trade_id=trade_id,
        legs=legs_for_engine,
        current_chain=current_chain,
        entry_net_credit=entry_credit,
        max_profit_rs=max_profit,
        max_loss_rs=max_loss,
        sl_level_per_share=sl_level_f,
        days_to_expiry=dte,
        strategy=strategy,
        as_of=now,
        greeks=greeks_row,
    )

    current_pnl = entry_credit
    for leg, mid in zip(legs_for_engine, [c["mid_price"] for c in current_chain]):
        qty = leg["lots"] * leg["lot_size"]
        sign = -1.0 if leg["action"] == "SELL" else 1.0
        current_pnl += sign * mid * qty

    greek_note = greeks_stress_check(
        strategy=strategy,
        days_to_expiry=dte,
        current_pnl=current_pnl,
        max_loss_rs=max_loss,
        greeks=greeks_row,
    )

    notif_type = None
    severity = "WARNING"
    if decision.decision == "SL_HIT":
        notif_type = "LOSS_LIMIT_HIT"
        severity = "CRITICAL"
    elif decision.decision == "THESIS_FAIL":
        notif_type = "THESIS_FAIL"
        severity = "CRITICAL"
    elif decision.decision == "TAKE_PROFIT":
        notif_type = "TARGET_HIT"
        severity = "INFO"
    elif greek_note and decision.decision == "HOLD":
        notif_type = "GREEK_STRESS"
        severity = "WARNING"

    if notif_type is None:
        return None

    sl_threshold, _ = effective_sl_rs(strategy=strategy, max_loss_rs=max_loss)
    body = (
        f"[WS fallback] {decision.reason}\n"
        f"Trade {trade.get('trade_name') or trade_id} · MTM ₹{current_pnl:,.0f}\n"
        f"Leg prices from chain poll (WS unhealthy)."
    )
    if greek_note:
        body += f"\n{greek_note}"

    notif_repo = NotificationRepo(db)
    cooldown_min = int(_cfg().get("alert_cooldown_minutes", 15))
    recent = notif_repo.recent_for_trade(trade_id, minutes=cooldown_min)
    for r in recent:
        if str(r.get("notif_type") or "") == notif_type:
            return None

    notif_repo.insert(Notification(
        created_at=now,
        notif_type=notif_type,
        severity=severity,
        title=f"{notif_type.replace('_', ' ')} on {trade.get('trade_name') or trade_id}",
        body=body[:900],
        related_trade_id=trade_id,
    ))
    return notif_type


def run_intraday_sl_fallback(
    db: SQLServerConnection,
    trade_date: Optional[date] = None,
    *,
    provider=None,
) -> int:
    """Poll chain prices and evaluate SL when WS is down. Returns alerts sent."""
    now = now_ist()
    ok, reason = _should_run(now)
    if not ok:
        logger.debug("intraday_sl_fallback: skipped — %s", reason)
        return 0

    trade_date = trade_date or today_ist()
    p = provider if provider is not None else get_market_data()
    trades = [t for t in TradeRepo(db).open_trades() if str(t.get("status") or "") == "ACTIVE"]
    if not trades:
        logger.debug("intraday_sl_fallback: no ACTIVE trades")
        return 0

    logger.info(
        "intraday_sl_fallback: WS unhealthy (%s) — evaluating %d trades",
        reason, len(trades),
    )

    chains_cache: Dict[tuple, Dict[tuple, dict]] = {}
    alerts = 0
    for trade in trades:
        try:
            if _evaluate_trade(
                db, trade, trade_date=trade_date,
                chains_cache=chains_cache, provider=p, now=now,
            ):
                alerts += 1
        except Exception:
            logger.exception(
                "intraday_sl_fallback: failed for trade %s",
                trade.get("trade_id"),
            )

    state = _load_state()
    state["last_run_at"] = now.isoformat(timespec="seconds")
    state["last_alerts"] = alerts
    _save_state(state)
    db.commit()
    return alerts
