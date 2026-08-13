"""Zerodha wallet balance, deployable capital, and entry sizing gates."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from database.connection import SQLServerConnection
from database.scout_models import ScoutTradeRepo
from scout.market_data import zerodha_ready

logger = logging.getLogger(__name__)


def _live_execution(settings: dict) -> bool:
    return bool(settings.get("zerodha_execute_orders", False))

_last_wallet_error: Optional[str] = None
_last_wallet_snapshot: Optional[dict] = None


def last_wallet_error() -> Optional[str]:
    return _last_wallet_error


def last_wallet_snapshot() -> Optional[dict]:
    return dict(_last_wallet_snapshot) if _last_wallet_snapshot else None


def _set_wallet_cache(snapshot: dict) -> dict:
    global _last_wallet_snapshot, _last_wallet_error
    _last_wallet_snapshot = dict(snapshot)
    _last_wallet_error = None
    return snapshot


def _set_wallet_error(msg: str) -> None:
    global _last_wallet_error
    _last_wallet_error = msg


def max_deployable_inr(balance: float, settings: dict) -> float:
    """Capital Scout may deploy given wallet balance and reserve rules."""
    bal = max(0.0, float(balance))
    pct = float(settings.get("wallet_utilization_pct", 90.0))
    reserve = float(settings.get("wallet_reserve_inr", 0))
    by_pct = bal * (pct / 100.0)
    if reserve > 0:
        by_reserve = max(0.0, bal - reserve)
        return min(by_pct, by_reserve)
    return by_pct


def fetch_equity_wallet(*, live: bool = True) -> Optional[dict]:
    """Return wallet snapshot from Kite margins (equity segment)."""
    if not live:
        return None
    ok, msg = zerodha_ready()
    if not ok:
        _set_wallet_error(msg)
        return None
    try:
        from providers.zerodha.order_client import KiteOrderClient
        raw = KiteOrderClient().margins()
    except Exception as exc:
        _set_wallet_error(str(exc))
        logger.warning("Wallet fetch failed: %s", exc)
        return None

    equity = raw.get("equity") if isinstance(raw, dict) else {}
    available = (equity or {}).get("available") or {}
    utilised = (equity or {}).get("utilised") or {}
    live_bal = available.get("live_balance")
    if live_bal is None:
        live_bal = available.get("cash")
    if live_bal is None:
        live_bal = equity.get("net")
    try:
        balance = float(live_bal or 0)
    except (TypeError, ValueError):
        balance = 0.0

    snap = {
        "balance_inr": round(balance, 2),
        "net_inr": round(float(equity.get("net") or balance), 2),
        "utilised_debits_inr": round(float(utilised.get("debits") or 0), 2),
        "raw_segment": "equity",
    }
    return _set_wallet_cache(snap)


def wallet_summary(
    db: SQLServerConnection,
    settings: dict,
    *,
    fetch: bool = True,
) -> dict:
    """Balance, deployed, free deployable capital for UI and gates."""
    trade_repo = ScoutTradeRepo(db)
    deployed = trade_repo.deployed_capital_inr()
    live = _live_execution(settings)

    out: Dict[str, Any] = {
        "live": live,
        "balance_inr": None,
        "max_deployable_inr": None,
        "deployed_inr": round(deployed, 2),
        "free_inr": None,
        "wallet_utilization_pct": float(settings.get("wallet_utilization_pct", 90)),
        "wallet_reserve_inr": float(settings.get("wallet_reserve_inr", 0)),
        "error": None,
    }

    if not live:
        out["error"] = "paper_mode"
        return out

    snap = fetch_equity_wallet(live=True) if fetch else last_wallet_snapshot()
    if not snap:
        out["error"] = last_wallet_error() or "wallet_unavailable"
        return out

    balance = float(snap["balance_inr"])
    max_dep = max_deployable_inr(balance, settings)
    free = max(0.0, max_dep - deployed)
    out.update({
        "balance_inr": round(balance, 2),
        "max_deployable_inr": round(max_dep, 2),
        "free_inr": round(free, 2),
    })
    return out


def cap_quantity_for_wallet(
    *,
    entry_price: float,
    quantity: int,
    free_inr: float,
) -> int:
    px = float(entry_price)
    if px <= 0:
        return max(1, int(quantity))
    max_qty = int(float(free_inr) // px)
    return max(0, min(int(quantity), max_qty))


def entry_wallet_block_reason(
    db: SQLServerConnection,
    *,
    entry_price: float,
    quantity: int,
    settings: dict,
) -> Optional[str]:
    if not _live_execution(settings):
        return None
    summary = wallet_summary(db, settings)
    if summary.get("error") and summary["error"] != "paper_mode":
        return f"wallet unavailable: {summary['error']}"
    free = summary.get("free_inr")
    if free is None:
        return "wallet balance unknown"
    needed = float(entry_price) * int(quantity)
    if needed <= float(free) + 0.01:
        return None
    bal = summary.get("balance_inr")
    deployed = summary.get("deployed_inr")
    max_dep = summary.get("max_deployable_inr")
    return (
        f"insufficient deployable capital (need ₹{needed:,.0f}, "
        f"free ₹{float(free):,.0f}; balance ₹{bal:,.0f}, "
        f"deployed ₹{deployed:,.0f}, cap ₹{max_dep:,.0f})"
    )


def verify_entry_margin(
    *,
    symbol: str,
    action: str,
    quantity: int,
    limit_price: float,
    db: Optional[SQLServerConnection] = None,
    settings: Optional[dict] = None,
) -> Tuple[bool, str]:
    """Pre-check order margin via Kite order_margins."""
    try:
        from providers.zerodha.order_client import KiteOrderClient
        from config import SCOUT_CONFIG

        client = KiteOrderClient()
        orders = [{
            "exchange": str(SCOUT_CONFIG.get("zerodha_exchange", "NSE")),
            "tradingsymbol": str(symbol).upper(),
            "transaction_type": str(action).upper(),
            "quantity": int(quantity),
            "product": str(SCOUT_CONFIG.get("zerodha_product", "MIS")),
            "order_type": str(SCOUT_CONFIG.get("zerodha_entry_order_type", "LIMIT")),
            "price": round(float(limit_price), 2),
        }]
        resp = client.order_margins(orders)
        if not resp:
            return True, ""
        row = resp[0] if isinstance(resp, list) else resp
        required = float(row.get("total") or row.get("margin") or 0)
        if required <= 0:
            return True, ""
        if db is not None and settings is not None:
            summary = wallet_summary(db, settings)
            free = summary.get("free_inr")
            if free is None:
                free = summary.get("balance_inr")
            if free is not None and required > float(free) + 0.01:
                return False, (
                    f"insufficient margin (need ₹{required:,.0f}, "
                    f"deployable ₹{float(free):,.0f})"
                )
        return True, ""
    except Exception as exc:
        msg = str(exc)
        if "margin" in msg.lower() or "fund" in msg.lower() or "insufficient" in msg.lower():
            return False, msg
        logger.warning("Margin pre-check failed (non-blocking): %s", exc)
        return True, ""
