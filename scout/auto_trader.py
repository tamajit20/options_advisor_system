"""Scout automation — auto-enter signals and auto-close open trades at live LTP."""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import ScoutSignalRepo, ScoutTradeRepo
from scout.config_loader import get_automation
from scout.signal_enrichment import build_exit_plan, enrich_signal, evaluate_exit_alerts
from scout.trade_audit import build_entry_audit
from scout.utils import is_market_open
from utils import now_ist

logger = logging.getLogger(__name__)


def _coerce_int_id(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _symbol_has_other_open_trade(trade_repo: ScoutTradeRepo, symbol: str, signal_id: int) -> bool:
    sym_upper = str(symbol).upper()
    for t in trade_repo.open_trades():
        if str(t.get("symbol") or "").upper() != sym_upper:
            continue
        existing_sid = _coerce_int_id(t.get("signal_id"))
        if existing_sid is not None and existing_sid != signal_id:
            return True
    return False


def try_auto_execute_signal(
    db: SQLServerConnection,
    *,
    signal_id: int,
    spot_lookup: Callable[[str], Optional[float]],
) -> Optional[dict]:
    """Mark a new signal taken at live LTP when auto_execute_signals is enabled."""
    settings = get_automation(db, use_cache=False)
    if not settings.get("auto_execute_signals"):
        return None
    if not SCOUT_CONFIG.get("enabled", True):
        return None
    if not is_market_open():
        return None

    sig_repo = ScoutSignalRepo(db)
    trade_repo = ScoutTradeRepo(db)
    sig = sig_repo.get(signal_id)
    if not sig:
        return None

    if signal_id in trade_repo.open_signal_ids():
        return None
    if _symbol_has_other_open_trade(trade_repo, str(sig["symbol"]), signal_id):
        logger.info(
            "Scout auto-enter skip SIG #%s — open trade already exists for %s",
            signal_id, sig["symbol"],
        )
        return None

    sym = str(sig["symbol"]).upper()
    ltp = spot_lookup(sym)
    if ltp is None or float(ltp) <= 0:
        ltp = float(sig.get("ltp") or 0)
    if ltp <= 0:
        return None

    enriched = enrich_signal(sig, live_ltp=float(ltp), now=now_ist().replace(tzinfo=None))
    if enriched.get("validity_status") != "ACTIVE":
        logger.info(
            "Scout auto-enter skip SIG #%s — status %s",
            signal_id, enriched.get("validity_status"),
        )
        return None

    qty = max(1, int(settings.get("auto_trade_quantity") or 1))
    executed_at = now_ist()
    tid = trade_repo.mark_taken(
        signal_id=signal_id,
        symbol=str(sig["symbol"]),
        action=str(sig["action"]),
        signal_type=str(sig.get("signal_type") or ""),
        entry_price=float(ltp),
        quantity=qty,
        executed_at=executed_at,
        notes=build_entry_audit(
            sig,
            entry_price=float(ltp),
            executed_at=executed_at,
            mode="auto",
            source="auto_execute",
        ),
    )
    logger.info(
        "Scout auto-enter: SIG #%s → TRD #%s %s %s @ %.2f × %d",
        signal_id, tid, sig["symbol"], sig["action"], float(ltp), qty,
    )
    return {
        "trade_id": tid,
        "signal_id": signal_id,
        "symbol": sig["symbol"],
        "entry_price": float(ltp),
        "quantity": qty,
    }


def try_auto_close_trades(
    db: SQLServerConnection,
    *,
    spot_lookup: Callable[[str], Optional[float]],
) -> List[dict]:
    """Close open trades when target/stop/square-off triggers and auto_close is enabled."""
    settings = get_automation(db, use_cache=False)
    if not settings.get("auto_close_trades"):
        return []
    if not SCOUT_CONFIG.get("enabled", True):
        return []

    trade_repo = ScoutTradeRepo(db)
    sig_repo = ScoutSignalRepo(db)
    closed: List[dict] = []
    now = now_ist().replace(tzinfo=None)

    for trade in trade_repo.open_trades():
        sym = str(trade["symbol"]).upper()
        ltp = spot_lookup(sym)
        if ltp is None or float(ltp) <= 0:
            ltp = float(trade.get("entry_price") or 0)
        if ltp <= 0:
            continue

        sig = None
        sid = _coerce_int_id(trade.get("signal_id"))
        if sid is not None:
            sig = sig_repo.get(sid)
        if not sig:
            sig = {
                "action": trade.get("action"),
                "invalidation": None,
                "signal_type": trade.get("signal_type"),
                "meta": {},
            }

        exit_plan = build_exit_plan(
            sig,
            entry_price=float(trade.get("entry_price") or 0),
            executed_at=trade.get("executed_at"),
            live_ltp=float(ltp),
            now=now,
        )
        alerts = evaluate_exit_alerts(
            action=str(trade.get("action") or ""),
            live_ltp=float(ltp),
            exit_plan=exit_plan,
        )
        if not alerts.get("close_now"):
            continue

        reason = "auto_close"
        if alerts.get("alerts"):
            reason = str(alerts["alerts"][0].get("code") or reason).lower()

        tid = int(trade["id"])
        result = trade_repo.close(
            tid,
            exit_price=float(ltp),
            closed_at=now_ist(),
            exit_reason=reason,
        )
        if result:
            logger.info(
                "Scout auto-close: TRD #%s %s @ %.2f (%s)",
                tid, sym, float(ltp), reason,
            )
            closed.append({
                "trade_id": tid,
                "symbol": sym,
                "exit_price": float(ltp),
                "exit_reason": reason,
                "exit_alert": (alerts.get("alerts") or [{}])[0],
                "pnl": result.get("pnl"),
            })

    return closed


def on_signals_committed(
    db: SQLServerConnection,
    signal_ids: List[int],
    spot_lookup: Callable[[str], Optional[float]],
) -> None:
    """Run auto-enter for freshly committed signal rows."""
    if not signal_ids:
        return
    settings = get_automation(db, use_cache=False)
    if not settings.get("auto_execute_signals"):
        return
    for sid in signal_ids:
        try:
            try_auto_execute_signal(db, signal_id=int(sid), spot_lookup=spot_lookup)
        except Exception:
            logger.exception("Scout auto-enter failed for signal %s", sid)
