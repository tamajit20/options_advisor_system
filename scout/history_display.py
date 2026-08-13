"""
scout/history_display.py — P&L / win-rate display classifiers and trade aggregation.

Canonical implementation for History tab coloring. Keep in sync with
dashboard/static/scout.js helpers (pnlClass, winPctClass, pfClass, aggregateTrades).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def pnl_class(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    if n != n:  # NaN
        return ""
    return "pnl-profit" if n >= 0 else "pnl-loss"


def win_pct_class(pct: Any) -> str:
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return ""
    if p != p:
        return ""
    if p >= 55:
        return "pnl-winpct-good"
    if p < 45:
        return "pnl-winpct-bad"
    return "pnl-winpct-neutral"


def pf_class(pf: Any) -> str:
    if pf is None or pf == "":
        return ""
    try:
        n = float(pf)
    except (TypeError, ValueError):
        return ""
    if n != n:
        return ""
    return "pnl-profit" if n >= 1 else "pnl-loss"


def trade_net_pnl(trade: dict) -> float:
    net = trade.get("net_pnl")
    if net is not None and net != "":
        return float(net)
    return float(trade.get("pnl") or 0)


def aggregate_trades(trades: Optional[List[dict]]) -> Dict[str, Any]:
    wins = 0
    gross_pnl = 0.0
    net_pnl = 0.0
    charges = 0.0
    win_amounts: List[float] = []
    loss_amounts: List[float] = []

    for t in trades or []:
        gross = float(
            t.get("gross_pnl") if t.get("gross_pnl") is not None else (t.get("pnl") or 0)
        )
        net = trade_net_pnl(t)
        ch = float(
            t.get("total_charges") if t.get("total_charges") is not None else max(0.0, gross - net)
        )
        gross_pnl += gross
        net_pnl += net
        charges += ch
        if net > 0:
            wins += 1
            win_amounts.append(net)
        elif net < 0:
            loss_amounts.append(net)

    n = len(trades or [])
    win_sum = sum(win_amounts)
    loss_sum = abs(sum(loss_amounts))

    return {
        "count": n,
        "wins": wins,
        "win_pct": round(wins / n * 100) if n else 0,
        "pnl": round(gross_pnl * 100) / 100,
        "net_pnl": round(net_pnl * 100) / 100,
        "total_charges": round(charges * 100) / 100,
        "avg_win": round(win_sum / len(win_amounts) * 100) / 100 if win_amounts else 0,
        "avg_loss": round(sum(loss_amounts) / len(loss_amounts) * 100) / 100 if loss_amounts else 0,
        "profit_factor": round(win_sum / loss_sum * 100) / 100 if loss_sum > 0 else None,
    }
