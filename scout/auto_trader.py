"""Scout automation — auto-enter signals and auto-close open trades."""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import ScoutSignalRepo, ScoutTradeRepo
from scout.config_loader import get_scout_settings
from scout.execution_engine import (
    execute_entry,
    execution_mode_label,
    paper_close_if_triggered,
    process_pending_entries,
    manage_open_trade_step3,
    retry_unprotected_trades,
    zerodha_execute_enabled,
)
from scout.wallet import cap_quantity_for_wallet, entry_wallet_block_reason
from scout.profit_gate import entry_profit_block_reason, signal_type_allowed
from scout.settings_schema import (
    compute_trade_quantity,
    in_trading_window,
    strength_allowed,
)
from scout.signal_enrichment import enrich_signal
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


def _auto_enter_block_reason(
    trade_repo: ScoutTradeRepo,
    settings: dict,
    *,
    signal_id: int,
    symbol: str,
    strength: str,
    signal_type: str,
) -> Optional[str]:
    if not in_trading_window(settings):
        return "outside trading window"
    max_day = int(settings.get("max_trades_per_day") or 0)
    if max_day > 0 and trade_repo.count_trades_opened_today() >= max_day:
        return f"daily trade cap ({max_day}) reached"
    sym = str(symbol).upper()
    if settings.get("one_trade_per_symbol_per_day") and trade_repo.symbol_has_trade_today(sym):
        return f"symbol already traded today ({sym})"
    if _symbol_has_other_open_trade(trade_repo, sym, signal_id):
        return f"open trade already exists for {sym}"
    if not strength_allowed(settings, strength):
        return f"strength {strength} not in auto-enter list"
    if not signal_type_allowed(settings, signal_type):
        return f"signal type {signal_type} not in auto-enter list"
    return None


def _entry_limit_price(enriched: dict, sig: dict, ltp: float) -> float:
    """Limit price for Step 1 — top of entry band for BUY, bottom for SELL."""
    action = str(sig.get("action") or "BUY").upper()
    try:
        if action == "BUY":
            return float(enriched.get("entry_max") or ltp)
        return float(enriched.get("entry_min") or ltp)
    except (TypeError, ValueError):
        return float(ltp)


def try_auto_execute_signal(
    db: SQLServerConnection,
    *,
    signal_id: int,
    spot_lookup: Callable[[str], Optional[float]],
) -> Optional[dict]:
    """Auto-enter via 3-step execution engine (paper or Zerodha)."""
    settings = get_scout_settings(db, use_cache=False)
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

    block = _auto_enter_block_reason(
        trade_repo,
        settings,
        signal_id=signal_id,
        symbol=str(sig["symbol"]),
        strength=str(sig.get("strength") or "WEAK"),
        signal_type=str(sig.get("signal_type") or ""),
    )
    if block:
        logger.info("Scout auto-enter skip SIG #%s — %s", signal_id, block)
        return None

    sym = str(sig["symbol"]).upper()
    ltp = spot_lookup(sym)
    if ltp is None or float(ltp) <= 0:
        ltp = float(sig.get("ltp") or 0)
    if ltp <= 0:
        return None

    qty = compute_trade_quantity(settings, float(ltp))

    enriched = enrich_signal(
        sig,
        live_ltp=float(ltp),
        now=now_ist().replace(tzinfo=None),
        settings=settings,
    )
    if enriched.get("validity_status") != "ACTIVE":
        logger.info(
            "Scout auto-enter skip SIG #%s — status %s",
            signal_id, enriched.get("validity_status"),
        )
        return None

    entry_px = _entry_limit_price(enriched, sig, float(ltp))
    if zerodha_execute_enabled(settings):
        wallet_block = entry_wallet_block_reason(
            db, entry_price=entry_px, quantity=qty, settings=settings,
        )
        if wallet_block:
            logger.info("Scout auto-enter skip SIG #%s — %s", signal_id, wallet_block)
            return None
        from scout.wallet import wallet_summary
        summary = wallet_summary(db, settings)
        free = summary.get("free_inr")
        if free is not None:
            qty = cap_quantity_for_wallet(
                entry_price=entry_px, quantity=qty, free_inr=float(free),
            )
            if qty <= 0:
                logger.info("Scout auto-enter skip SIG #%s — no deployable capital", signal_id)
                return None

    profit_block = entry_profit_block_reason(
        signal=sig,
        entry=float(ltp),
        qty=qty,
        settings=settings,
    )
    if profit_block:
        logger.info("Scout auto-enter skip SIG #%s — %s", signal_id, profit_block)
        return None

    mode = execution_mode_label(settings)
    try:
        out = execute_entry(
            db,
            signal_id=signal_id,
            sig=sig,
            entry_price=entry_px,
            quantity=qty,
            settings=settings,
            mode=mode,
        )
    except Exception as exc:
        logger.exception("Scout auto-enter failed SIG #%s: %s", signal_id, exc)
        return None

    logger.info(
        "Scout auto-enter: SIG #%s → TRD #%s %s %s @ %.2f × %d (%s)",
        signal_id, out["trade_id"], sig["symbol"], sig["action"], entry_px, qty, mode,
    )
    return {
        "trade_id": out["trade_id"],
        "signal_id": signal_id,
        "symbol": sig["symbol"],
        "entry_price": entry_px,
        "quantity": qty,
        "execution_mode": mode,
    }


def try_auto_close_trades(
    db: SQLServerConnection,
    *,
    spot_lookup: Callable[[str], Optional[float]],
) -> List[dict]:
    """Step 3 — manage open trades (paper LTP or live Zerodha)."""
    settings = get_scout_settings(db, use_cache=False)
    if not settings.get("auto_close_trades"):
        return []
    if not SCOUT_CONFIG.get("enabled", True):
        return []

    trade_repo = ScoutTradeRepo(db)
    sig_repo = ScoutSignalRepo(db)
    closed: List[dict] = []
    live = zerodha_execute_enabled(settings)

    for trade in trade_repo.open_trades():
        status = str(trade.get("status") or "")
        if status not in ("OPEN", "UNPROTECTED"):
            continue
        sym = str(trade["symbol"]).upper()
        ltp = spot_lookup(sym)
        if ltp is None or float(ltp) <= 0:
            ltp = float(trade.get("entry_price") or 0)
        if ltp <= 0:
            continue

        sid = _coerce_int_id(trade.get("signal_id"))
        sig = sig_repo.get(sid) if sid else None
        if not sig:
            sig = {
                "action": trade.get("action"),
                "invalidation": None,
                "signal_type": trade.get("signal_type"),
                "meta": {},
            }

        if live:
            result = manage_open_trade_step3(
                db,
                trade=trade,
                signal=sig,
                live_ltp=float(ltp),
                settings=settings,
                live=True,
            )
        else:
            result = paper_close_if_triggered(
                db,
                trade=trade,
                signal=sig,
                live_ltp=float(ltp),
                settings=settings,
            )

        if result:
            closed.append({
                "trade_id": int(trade["id"]),
                "symbol": sym,
                "exit_price": float(result.get("exit_price") or ltp),
                "exit_reason": result.get("exit_reason"),
                "pnl": result.get("pnl"),
            })

    return closed


def try_auto_enter_pending_signals(
    db: SQLServerConnection,
    *,
    spot_lookup: Callable[[str], Optional[float]],
) -> List[dict]:
    """Retry auto-enter on recent signals that never got a trade row."""
    settings = get_scout_settings(db, use_cache=False)
    if not settings.get("auto_execute_signals"):
        return []
    if not SCOUT_CONFIG.get("enabled", True) or not is_market_open():
        return []

    valid_mins = int(settings.get("signal_valid_minutes", 30))
    sig_repo = ScoutSignalRepo(db)
    entered: List[dict] = []
    for sid in sig_repo.signal_ids_without_trade(since_minutes=valid_mins + 30):
        try:
            out = try_auto_execute_signal(db, signal_id=int(sid), spot_lookup=spot_lookup)
            if out:
                entered.append(out)
        except Exception:
            logger.exception("Scout auto-enter poll failed for signal %s", sid)
    return entered


def run_execution_poll(
    db: SQLServerConnection,
    *,
    spot_lookup: Callable[[str], Optional[float]],
) -> dict:
    """Single poll tick: pending entries, auto-enter retry, auto-close."""
    settings = get_scout_settings(db, use_cache=False)
    out = {
        "pending_filled": [],
        "protection_retried": [],
        "entered": [],
        "closed": [],
    }
    if zerodha_execute_enabled(settings):
        out["pending_filled"] = process_pending_entries(
            db, spot_lookup=spot_lookup, settings=settings,
        )
        out["protection_retried"] = retry_unprotected_trades(
            db, spot_lookup=spot_lookup, settings=settings,
        )
    out["entered"] = try_auto_enter_pending_signals(db, spot_lookup=spot_lookup)
    out["closed"] = try_auto_close_trades(db, spot_lookup=spot_lookup)
    return out


def on_signals_committed(
    db: SQLServerConnection,
    signal_ids: List[int],
    spot_lookup: Callable[[str], Optional[float]],
) -> None:
    """Run auto-enter for freshly committed signal rows."""
    if not signal_ids:
        return
    settings = get_scout_settings(db, use_cache=False)
    if not settings.get("auto_execute_signals"):
        return
    for sid in signal_ids:
        try:
            try_auto_execute_signal(db, signal_id=int(sid), spot_lookup=spot_lookup)
        except Exception:
            logger.exception("Scout auto-enter failed for signal %s", sid)
