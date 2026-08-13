"""
scout/execution_flow.py — Build per-trade execution flow for API / dashboard UI.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import ScoutSignalRepo, ScoutTradeOrderRepo, ScoutTradeRepo
from scout.execution_engine import execution_mode_label, zerodha_execute_enabled
from scout.live_quotes import latest_equity_ltps
from scout.signal_enrichment import build_exit_plan, evaluate_exit_alerts, scout_trade_mtm
from utils import now_ist


_STEP_LABELS = {
    1: "Step 1 — Enter",
    2: "Step 2 — Protect + target",
    3: "Step 3 — Watch + adjust",
}

_LEG_LABELS = {
    "ENTRY": "Entry order",
    "STOP_LOSS": "Stop loss",
    "TARGET": "Target limit",
    "EXIT": "Exit order",
}


def _order_status_class(status: str) -> str:
    st = str(status or "").upper()
    if st in ("COMPLETE", "FILLED", "SIMULATED"):
        return "done"
    if st in ("PLACED", "OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED"):
        return "active"
    if st in ("CANCELLED", "REJECTED", "FAILED"):
        return "failed"
    return "pending"


def _step_status(trade_status: str, step_num: int, orders: List[dict]) -> str:
    ts = str(trade_status or "").upper()
    step_orders = [o for o in orders if int(o.get("step_num") or 0) == step_num]
    if step_num == 1:
        if ts == "PENDING_ENTRY":
            return "active"
        if ts in ("OPEN", "CLOSING", "CLOSED"):
            return "done"
        if ts == "FAILED":
            return "failed"
        return "pending"
    if step_num == 2:
        if ts == "PENDING_ENTRY":
            return "pending"
        if not step_orders:
            return "pending"
        if all(_order_status_class(o.get("status")) == "done" for o in step_orders):
            return "done"
        return "active"
    if step_num == 3:
        if ts == "OPEN":
            return "active"
        if ts == "CLOSED":
            return "done"
        return "pending"
    return "pending"


def _format_order(o: dict) -> dict:
    return {
        "id": o.get("id"),
        "leg": o.get("leg"),
        "leg_label": _LEG_LABELS.get(str(o.get("leg") or ""), str(o.get("leg") or "")),
        "step_num": o.get("step_num"),
        "order_type": o.get("order_type"),
        "transaction_type": o.get("transaction_type"),
        "product": o.get("product"),
        "quantity": o.get("quantity"),
        "price": o.get("price"),
        "trigger_price": o.get("trigger_price"),
        "status": o.get("status"),
        "status_class": _order_status_class(o.get("status")),
        "kite_order_id": o.get("kite_order_id"),
        "exchange_order_id": o.get("exchange_order_id"),
        "status_message": o.get("status_message"),
        "placed_at": o.get("placed_at"),
    }


def build_trade_execution_flow(
    *,
    trade: dict,
    signal: Optional[dict],
    orders: List[dict],
    live_ltp: Optional[float],
    settings: dict,
) -> dict:
    ts = str(trade.get("status") or "OPEN").upper()
    mode = str(trade.get("execution_mode") or execution_mode_label())
    sig = signal or {
        "action": trade.get("action"),
        "invalidation": None,
        "signal_type": trade.get("signal_type"),
        "meta": {},
    }
    now = now_ist().replace(tzinfo=None)
    exit_plan = build_exit_plan(
        sig,
        entry_price=float(trade.get("entry_price") or 0),
        executed_at=trade.get("executed_at"),
        live_ltp=live_ltp,
        now=now,
        settings=settings,
    )
    exit_alerts = evaluate_exit_alerts(
        action=str(trade.get("action") or ""),
        live_ltp=live_ltp,
        exit_plan=exit_plan,
        entry_price=float(trade.get("entry_price") or 0),
        peak_price=trade.get("peak_price"),
        settings=settings,
    )
    mtm = scout_trade_mtm(trade, live_ltp)

    current_step = 1
    if ts == "PENDING_ENTRY":
        current_step = 1
    elif ts == "OPEN":
        current_step = 3
    elif ts == "CLOSED":
        current_step = 3

    steps = []
    for n in (1, 2, 3):
        step_orders = [_format_order(o) for o in orders if int(o.get("step_num") or 0) == n]
        steps.append({
            "step": n,
            "label": _STEP_LABELS[n],
            "status": _step_status(ts, n, orders),
            "orders": step_orders,
        })

    square_off = format_square_off_time(settings)

    return {
        "trade_id": trade.get("id"),
        "signal_id": trade.get("signal_id"),
        "symbol": trade.get("symbol"),
        "action": trade.get("action"),
        "trade_status": ts,
        "execution_mode": mode,
        "zerodha_live": mode == "zerodha" and zerodha_execute_enabled(settings),
        "current_step": current_step,
        "steps": steps,
        "exit_plan": exit_plan,
        "exit_alerts": exit_alerts,
        "mtm": mtm,
        "live_ltp": live_ltp,
        "effective_stop_price": trade.get("effective_stop_price"),
        "square_off_time": square_off,
    }


def build_flow_items(db: SQLServerConnection, *, settings: dict) -> List[dict]:
    """Unified signal + trade execution items for the dashboard."""
    sig_repo = ScoutSignalRepo(db)
    trade_repo = ScoutTradeRepo(db)
    order_repo = ScoutTradeOrderRepo(db)

    open_trades = trade_repo.open_trades()
    trade_by_signal = {
        int(t["signal_id"]): t for t in open_trades if t.get("signal_id") is not None
    }
    symbols = {str(t.get("symbol") or "").upper() for t in open_trades}
    since = int(SCOUT_CONFIG.get("signal_display_minutes", 120))
    signals = sig_repo.recent(limit=50, since_minutes=since)
    for s in signals:
        symbols.add(str(s.get("symbol") or "").upper())
    quotes = latest_equity_ltps(symbols)

    items: List[dict] = []
    seen_signals: set[int] = set()

    for trade in open_trades:
        sid = trade.get("signal_id")
        sig = sig_repo.get(int(sid)) if sid else None
        sym = str(trade.get("symbol") or "").upper()
        ltp = (quotes.get(sym) or {}).get("ltp")
        orders = order_repo.for_trade(int(trade["id"]))
        flow = build_trade_execution_flow(
            trade=trade, signal=sig, orders=orders, live_ltp=ltp, settings=settings,
        )
        items.append({
            "kind": "trade",
            "trade": trade,
            "signal": sig,
            "execution": flow,
        })
        if sid is not None:
            seen_signals.add(int(sid))

    for sig in signals:
        sid = int(sig["id"])
        if sid in seen_signals:
            continue
        if sid in trade_by_signal:
            continue
        sym = str(sig.get("symbol") or "").upper()
        ltp = (quotes.get(sym) or {}).get("ltp")
        items.append({
            "kind": "signal",
            "trade": None,
            "signal": sig,
            "execution": None,
            "live_ltp": ltp,
        })

    return items
