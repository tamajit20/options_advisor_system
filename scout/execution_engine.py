"""
scout/execution_engine.py — Scout 3-step execution (paper or live Zerodha).

Step 1 — Enter: limit buy/sell at entry band
Step 2 — After fill: stop-loss + target limit on Zerodha (or simulated in DB)
Step 3 — Watch: modify stop (breakeven/trail), sync fills, square-off exit

When persisted ``zerodha_execute_orders`` is False, all steps update the
database only — no Kite order calls.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import ScoutTradeOrderRepo, ScoutTradeRepo
from scout.config_loader import get_scout_settings
from scout.market_data import zerodha_ready
from scout.signal_enrichment import (
    _effective_stop,
    build_exit_plan,
    enrich_signal,
    evaluate_exit_alerts,
)
from scout.trade_audit import build_entry_audit
from utils import now_ist

logger = logging.getLogger(__name__)

_ORDER_COMPLETE = frozenset({"COMPLETE", "FILLED"})
_ORDER_CANCELLED = frozenset({"CANCELLED", "REJECTED"})
_STEP2_MAX_ATTEMPTS = 3


def zerodha_execute_enabled(settings: Optional[dict] = None) -> bool:
    if settings is not None:
        return bool(settings.get("zerodha_execute_orders", False))
    from scout.config_loader import get_scout_settings
    return bool(get_scout_settings().get("zerodha_execute_orders", False))


def execution_mode_label(settings: Optional[dict] = None) -> str:
    return "zerodha" if zerodha_execute_enabled(settings) else "paper"


def _round_px(px: float) -> float:
    return round(float(px), 2)


def _exit_txn(action: str) -> str:
    return "SELL" if str(action).upper() == "BUY" else "BUY"


def _sl_limit_price(trigger: float, exit_txn: str) -> float:
    """Limit price for SL-M after trigger."""
    t = _round_px(trigger)
    if exit_txn == "SELL":
        return _round_px(t * 0.995)
    return _round_px(t * 1.005)


def _kite_client():
    from providers.zerodha.order_client import KiteOrderClient
    return KiteOrderClient()


def _record_order(
    order_repo: ScoutTradeOrderRepo,
    *,
    trade_id: int,
    step_num: int,
    leg: str,
    quantity: int,
    order_type: str,
    transaction_type: str,
    product: str,
    price: Optional[float] = None,
    trigger_price: Optional[float] = None,
    status: str,
    kite_order_id: Optional[str] = None,
    status_message: Optional[str] = None,
    meta: Optional[dict] = None,
) -> int:
    return order_repo.insert(
        trade_id=trade_id,
        step_num=step_num,
        leg=leg,
        quantity=quantity,
        order_type=order_type,
        transaction_type=transaction_type,
        product=product,
        price=price,
        trigger_price=trigger_price,
        status=status,
        kite_order_id=kite_order_id,
        status_message=status_message,
        meta=meta,
    )


def _place_entry_order(
    *,
    symbol: str,
    action: str,
    quantity: int,
    limit_price: float,
    live: bool,
) -> Tuple[Optional[str], str]:
    exchange = str(SCOUT_CONFIG.get("zerodha_exchange", "NSE"))
    product = str(SCOUT_CONFIG.get("zerodha_product", "MIS"))
    order_type = str(SCOUT_CONFIG.get("zerodha_entry_order_type", "LIMIT"))
    if not live:
        return None, "SIMULATED"
    client = _kite_client()
    oid = client.place_order(
        exchange=exchange,
        tradingsymbol=str(symbol).upper(),
        transaction_type=str(action).upper(),
        quantity=int(quantity),
        product=product,
        order_type=order_type,
        price=_round_px(limit_price),
    )
    return oid, "PLACED"


def _place_stop_order(
    *,
    symbol: str,
    exit_txn: str,
    quantity: int,
    trigger_price: float,
    live: bool,
) -> Tuple[Optional[str], str]:
    exchange = str(SCOUT_CONFIG.get("zerodha_exchange", "NSE"))
    product = str(SCOUT_CONFIG.get("zerodha_product", "MIS"))
    order_type = str(SCOUT_CONFIG.get("zerodha_stop_order_type", "SL-M"))
    limit_px = _sl_limit_price(trigger_price, exit_txn)
    if not live:
        return None, "SIMULATED"
    client = _kite_client()
    oid = client.place_order(
        exchange=exchange,
        tradingsymbol=str(symbol).upper(),
        transaction_type=exit_txn,
        quantity=int(quantity),
        product=product,
        order_type=order_type,
        trigger_price=_round_px(trigger_price),
        price=limit_px,
    )
    return oid, "PLACED"


def _place_target_order(
    *,
    symbol: str,
    exit_txn: str,
    quantity: int,
    target_price: float,
    live: bool,
) -> Tuple[Optional[str], str]:
    exchange = str(SCOUT_CONFIG.get("zerodha_exchange", "NSE"))
    product = str(SCOUT_CONFIG.get("zerodha_product", "MIS"))
    if not live:
        return None, "SIMULATED"
    client = _kite_client()
    oid = client.place_order(
        exchange=exchange,
        tradingsymbol=str(symbol).upper(),
        transaction_type=exit_txn,
        quantity=int(quantity),
        product=product,
        order_type="LIMIT",
        price=_round_px(target_price),
    )
    return oid, "PLACED"


def _place_exit_market(
    *,
    symbol: str,
    exit_txn: str,
    quantity: int,
    live: bool,
) -> Tuple[Optional[str], str]:
    exchange = str(SCOUT_CONFIG.get("zerodha_exchange", "NSE"))
    product = str(SCOUT_CONFIG.get("zerodha_product", "MIS"))
    order_type = str(SCOUT_CONFIG.get("zerodha_exit_order_type", "MARKET"))
    if not live:
        return None, "SIMULATED"
    client = _kite_client()
    oid = client.place_order(
        exchange=exchange,
        tradingsymbol=str(symbol).upper(),
        transaction_type=exit_txn,
        quantity=int(quantity),
        product=product,
        order_type=order_type,
    )
    return oid, "PLACED"


def _modify_stop_order(
    order_row: dict,
    *,
    trigger_price: float,
    exit_txn: str,
    live: bool,
) -> None:
    if not live:
        return
    oid = order_row.get("kite_order_id")
    if not oid:
        return
    client = _kite_client()
    client.modify_order(
        str(oid),
        order_type=str(SCOUT_CONFIG.get("zerodha_stop_order_type", "SL-M")),
        trigger_price=_round_px(trigger_price),
        price=_sl_limit_price(trigger_price, exit_txn),
    )


def _cancel_broker_order(order_row: Optional[dict], live: bool) -> None:
    if not live or not order_row:
        return
    oid = order_row.get("kite_order_id")
    status = str(order_row.get("status") or "").upper()
    if not oid or status in _ORDER_COMPLETE | _ORDER_CANCELLED:
        return
    try:
        _kite_client().cancel_order(str(oid))
    except Exception as exc:
        logger.warning("Cancel order %s failed: %s", oid, exc)


def _sync_order_status(order_repo: ScoutTradeOrderRepo, order_row: dict, live: bool) -> dict:
    if not live:
        return order_row
    oid = order_row.get("kite_order_id")
    if not oid:
        return order_row
    from providers.zerodha.order_client import KiteOrderClient
    client = KiteOrderClient()
    hist = client.order_history(str(oid))
    st = client.latest_status(hist)
    status = str(st.get("status") or "UNKNOWN").upper()
    order_repo.update_status(
        int(order_row["id"]),
        status=status,
        status_message=st.get("status_message"),
        exchange_order_id=st.get("exchange_order_id"),
        price=float(st["average_price"]) if st.get("average_price") else None,
    )
    return order_repo.get_leg(int(order_row["trade_id"]), str(order_row["leg"])) or order_row


def place_protection_and_target(
    db: SQLServerConnection,
    *,
    trade: dict,
    signal: dict,
    entry_price: float,
    settings: dict,
    live: bool,
) -> dict:
    """Step 2 — stop loss + target limit after entry fill. Returns {stop_ok, target_ok}."""
    trade_id = int(trade["id"])
    order_repo = ScoutTradeOrderRepo(db)
    trade_repo = ScoutTradeRepo(db)
    action = str(trade.get("action") or "BUY").upper()
    exit_txn = _exit_txn(action)
    qty = int(trade.get("quantity") or 1)
    sym = str(trade.get("symbol") or "").upper()
    product = str(SCOUT_CONFIG.get("zerodha_product", "MIS"))

    plan = build_exit_plan(
        signal,
        entry_price=float(entry_price),
        executed_at=trade.get("executed_at"),
        settings=settings,
    )
    stop_px = plan.get("stop_price")
    target_px = plan.get("target_price")
    stop_ok = order_repo.leg_placed(trade_id, "STOP_LOSS")
    target_ok = order_repo.leg_placed(trade_id, "TARGET")

    if stop_px is not None and not stop_ok:
        try:
            oid, st = _place_stop_order(
                symbol=sym,
                exit_txn=exit_txn,
                quantity=qty,
                trigger_price=float(stop_px),
                live=live,
            )
            _record_order(
                order_repo,
                trade_id=trade_id,
                step_num=2,
                leg="STOP_LOSS",
                quantity=qty,
                order_type=str(SCOUT_CONFIG.get("zerodha_stop_order_type", "SL-M")),
                transaction_type=exit_txn,
                product=product,
                trigger_price=float(stop_px),
                price=_sl_limit_price(float(stop_px), exit_txn),
                status=st,
                kite_order_id=oid,
            )
            trade_repo.update_effective_stop(trade_id, stop_price=float(stop_px))
            stop_ok = True
        except Exception as exc:
            logger.error("TRD #%s Step 2 stop failed: %s", trade_id, exc)
            _record_order(
                order_repo,
                trade_id=trade_id,
                step_num=2,
                leg="STOP_LOSS",
                quantity=qty,
                order_type=str(SCOUT_CONFIG.get("zerodha_stop_order_type", "SL-M")),
                transaction_type=exit_txn,
                product=product,
                trigger_price=float(stop_px) if stop_px is not None else None,
                price=_sl_limit_price(float(stop_px), exit_txn) if stop_px is not None else None,
                status="FAILED",
                status_message=str(exc)[:256],
            )
            stop_ok = False

    if target_px is not None and not target_ok:
        try:
            oid, st = _place_target_order(
                symbol=sym,
                exit_txn=exit_txn,
                quantity=qty,
                target_price=float(target_px),
                live=live,
            )
            _record_order(
                order_repo,
                trade_id=trade_id,
                step_num=2,
                leg="TARGET",
                quantity=qty,
                order_type="LIMIT",
                transaction_type=exit_txn,
                product=product,
                price=float(target_px),
                status=st,
                kite_order_id=oid,
            )
            target_ok = True
        except Exception as exc:
            logger.warning("TRD #%s Step 2 target failed: %s", trade_id, exc)

    return {"stop_ok": stop_ok, "target_ok": target_ok}


def run_catch_up_watch(
    db: SQLServerConnection,
    *,
    trade: dict,
    signal: dict,
    live_ltp: float,
    settings: dict,
    live: bool,
) -> None:
    """Immediate Step 3 check after Step 2 (breakeven / trail / already at target)."""
    manage_open_trade_step3(
        db,
        trade=trade,
        signal=signal,
        live_ltp=live_ltp,
        settings=settings,
        live=live,
        catch_up=True,
    )


def _apply_step2_result(
    trade_repo: ScoutTradeRepo,
    trade_id: int,
    result: dict,
    *,
    live: bool,
) -> None:
    if live and not result.get("stop_ok"):
        trade_repo.set_status(trade_id, "UNPROTECTED")
        logger.critical("TRD #%s UNPROTECTED — stop-loss not placed on Zerodha", trade_id)
        return
    row = trade_repo.get(trade_id) or {}
    if live and str(row.get("status")) == "UNPROTECTED" and result.get("stop_ok"):
        trade_repo.set_status(trade_id, "OPEN")


def execute_entry(
    db: SQLServerConnection,
    *,
    signal_id: int,
    sig: dict,
    entry_price: float,
    quantity: int,
    settings: dict,
    mode: str,
    source: str = "auto_execute",
) -> dict:
    """Step 1 — create trade row and entry order (live or simulated)."""
    from scout.wallet import (
        cap_quantity_for_wallet,
        entry_wallet_block_reason,
        verify_entry_margin,
        wallet_summary,
    )

    trade_repo = ScoutTradeRepo(db)
    order_repo = ScoutTradeOrderRepo(db)
    live = mode == "zerodha"
    executed_at = now_ist()
    action = str(sig.get("action") or "BUY").upper()
    product = str(SCOUT_CONFIG.get("zerodha_product", "MIS"))
    sym = str(sig["symbol"]).upper()
    entry_px = float(entry_price)
    qty = int(quantity)

    if live:
        ok, msg = zerodha_ready()
        if not ok:
            raise RuntimeError(msg)

        from providers.zerodha.permission_check import latest_check_from_db, last_permission_summary

        latest_check_from_db(db)
        if not permissions_ok_for_live():
            summ = last_permission_summary()
            hint = summ.get("failed_count") if summ else "unknown"
            raise RuntimeError(
                f"Zerodha permission check failed ({hint} issue(s)) — "
                "see Scout → Errors tab and re-run checks after login"
            )

        wallet_block = entry_wallet_block_reason(
            db, entry_price=entry_px, quantity=qty, settings=settings,
        )
        if wallet_block:
            raise RuntimeError(wallet_block)

        summary = wallet_summary(db, settings)
        free = summary.get("free_inr")
        if free is not None:
            qty = cap_quantity_for_wallet(
                entry_price=entry_px, quantity=qty, free_inr=float(free),
            )
            if qty <= 0:
                raise RuntimeError(
                    f"insufficient deployable capital for even 1 share @ ₹{entry_px:,.2f}"
                )

        margin_ok, margin_msg = verify_entry_margin(
            symbol=sym, action=action, quantity=qty, limit_price=entry_px,
        )
        if not margin_ok:
            raise RuntimeError(margin_msg or "margin check failed")

        oid: Optional[str] = None
        ost = "FAILED"
        try:
            oid, ost = _place_entry_order(
                symbol=sym,
                action=action,
                quantity=qty,
                limit_price=entry_px,
                live=True,
            )
        except Exception as exc:
            raise RuntimeError(f"Zerodha entry order failed: {exc}") from exc

        try:
            tid = trade_repo.mark_pending_entry(
                signal_id=signal_id,
                symbol=sym,
                action=action,
                signal_type=str(sig.get("signal_type") or ""),
                entry_price=entry_px,
                quantity=qty,
                executed_at=executed_at,
                notes=build_entry_audit(
                    sig,
                    entry_price=entry_px,
                    executed_at=executed_at,
                    mode="auto",
                    source=source,
                ),
                execution_mode=mode,
            )
            _record_order(
                order_repo,
                trade_id=tid,
                step_num=1,
                leg="ENTRY",
                quantity=qty,
                order_type=str(SCOUT_CONFIG.get("zerodha_entry_order_type", "LIMIT")),
                transaction_type=action,
                product=product,
                price=entry_px,
                status=ost,
                kite_order_id=oid,
            )
        except Exception as exc:
            if oid:
                _cancel_broker_order({"kite_order_id": oid, "status": "PLACED"}, live=True)
            raise RuntimeError(f"DB record failed after Kite entry: {exc}") from exc

        return {
            "trade_id": tid,
            "signal_id": signal_id,
            "status": "PENDING_ENTRY",
            "execution_mode": mode,
            "quantity": qty,
        }

    tid = trade_repo.mark_taken(
        signal_id=signal_id,
        symbol=sym,
        action=action,
        signal_type=str(sig.get("signal_type") or ""),
        entry_price=entry_px,
        quantity=qty,
        executed_at=executed_at,
        status="OPEN",
        execution_mode=mode,
        notes=build_entry_audit(
            sig,
            entry_price=entry_px,
            executed_at=executed_at,
            mode="auto",
            source=source,
        ),
    )

    _record_order(
        order_repo,
        trade_id=tid,
        step_num=1,
        leg="ENTRY",
        quantity=qty,
        order_type=str(SCOUT_CONFIG.get("zerodha_entry_order_type", "LIMIT")),
        transaction_type=action,
        product=product,
        price=entry_px,
        status="SIMULATED",
        kite_order_id=None,
    )

    place_protection_and_target(
        db,
        trade=trade_repo.get(tid) or {"id": tid, **sig, "quantity": qty, "action": action},
        signal=sig,
        entry_price=entry_px,
        settings=settings,
        live=False,
    )

    return {"trade_id": tid, "signal_id": signal_id, "status": "OPEN", "execution_mode": mode}


def process_pending_entries(
    db: SQLServerConnection,
    *,
    spot_lookup: Callable[[str], Optional[float]],
    settings: dict,
) -> List[dict]:
    """Poll Step 1 entry orders until filled, then run Step 2 + catch-up."""
    if not zerodha_execute_enabled(settings):
        return []
    trade_repo = ScoutTradeRepo(db)
    order_repo = ScoutTradeOrderRepo(db)
    from database.scout_models import ScoutSignalRepo
    sig_repo = ScoutSignalRepo(db)
    results: List[dict] = []

    for trade in trade_repo.pending_entry_trades():
        tid = int(trade["id"])
        entry_row = order_repo.get_leg(tid, "ENTRY")
        if not entry_row:
            continue
        entry_row = _sync_order_status(order_repo, entry_row, live=True)
        st = str(entry_row.get("status") or "").upper()
        sym = str(trade.get("symbol") or "").upper()

        if st in _ORDER_CANCELLED:
            trade_repo.mark_failed(tid, reason=f"entry_{st.lower()}")
            results.append({"trade_id": tid, "event": "entry_failed", "status": st})
            continue
        if st not in _ORDER_COMPLETE:
            continue

        fill_px = float(entry_row.get("price") or trade.get("entry_price") or 0)
        if fill_px <= 0:
            ltp = spot_lookup(sym)
            fill_px = float(ltp) if ltp else float(trade.get("entry_price") or 0)
        trade_repo.activate_from_fill(tid, entry_price=fill_px, executed_at=now_ist())
        trade = trade_repo.get(tid) or trade

        sid = trade.get("signal_id")
        sig = sig_repo.get(int(sid)) if sid else None
        if not sig:
            sig = {
                "action": trade.get("action"),
                "invalidation": None,
                "signal_type": trade.get("signal_type"),
                "meta": {},
            }

        place_result = place_protection_and_target(
            db, trade=trade, signal=sig, entry_price=fill_px, settings=settings, live=True,
        )
        _apply_step2_result(trade_repo, tid, place_result, live=True)
        trade = trade_repo.get(tid) or trade

        if str(trade.get("status") or "") == "OPEN":
            ltp = spot_lookup(sym) or fill_px
            run_catch_up_watch(
                db, trade=trade, signal=sig,
                live_ltp=float(ltp), settings=settings, live=True,
            )
        results.append({
            "trade_id": tid,
            "event": "entry_filled",
            "entry_price": fill_px,
            "protected": place_result.get("stop_ok"),
        })
    return results


def retry_unprotected_trades(
    db: SQLServerConnection,
    *,
    spot_lookup: Callable[[str], Optional[float]],
    settings: dict,
) -> List[dict]:
    """Retry Step 2 stop placement for UNPROTECTED live trades."""
    if not zerodha_execute_enabled(settings):
        return []
    trade_repo = ScoutTradeRepo(db)
    order_repo = ScoutTradeOrderRepo(db)
    from database.scout_models import ScoutSignalRepo
    sig_repo = ScoutSignalRepo(db)
    results: List[dict] = []

    for trade in trade_repo.unprotected_trades():
        tid = int(trade["id"])
        attempts = order_repo.count_step_attempts(tid, step_num=2, leg="STOP_LOSS")
        if attempts >= _STEP2_MAX_ATTEMPTS:
            continue
        sid = trade.get("signal_id")
        sig = sig_repo.get(int(sid)) if sid else None
        if not sig:
            sig = {
                "action": trade.get("action"),
                "invalidation": None,
                "signal_type": trade.get("signal_type"),
                "meta": {},
            }
        fill_px = float(trade.get("entry_price") or 0)
        result = place_protection_and_target(
            db, trade=trade, signal=sig, entry_price=fill_px, settings=settings, live=True,
        )
        _apply_step2_result(trade_repo, tid, result, live=True)
        if result.get("stop_ok"):
            sym = str(trade.get("symbol") or "").upper()
            ltp = spot_lookup(sym) or fill_px
            updated = trade_repo.get(tid) or trade
            run_catch_up_watch(
                db, trade=updated, signal=sig,
                live_ltp=float(ltp), settings=settings, live=True,
            )
            results.append({"trade_id": tid, "event": "protection_restored"})
        else:
            results.append({"trade_id": tid, "event": "protection_retry_failed"})
    return results


def _manage_unprotected_trade(
    db: SQLServerConnection,
    *,
    trade: dict,
    signal: dict,
    live_ltp: float,
    settings: dict,
    live: bool,
) -> Optional[dict]:
    """UNPROTECTED — no stop modify; allow square-off / alert-driven exit only."""
    trade_repo = ScoutTradeRepo(db)
    order_repo = ScoutTradeOrderRepo(db)
    tid = int(trade["id"])
    action = str(trade.get("action") or "BUY").upper()
    exit_txn = _exit_txn(action)
    sym = str(trade.get("symbol") or "").upper()
    qty = int(trade.get("quantity") or 1)
    entry = float(trade.get("entry_price") or 0)
    now = now_ist().replace(tzinfo=None)

    exit_plan = build_exit_plan(
        signal,
        entry_price=entry,
        executed_at=trade.get("executed_at"),
        live_ltp=float(live_ltp),
        now=now,
        settings=settings,
    )
    alerts = evaluate_exit_alerts(
        action=action,
        live_ltp=float(live_ltp),
        exit_plan=exit_plan,
        entry_price=entry,
        peak_price=trade.get("peak_price"),
        settings=settings,
    )
    if not alerts.get("close_now"):
        return None

    reason = "unprotected_exit"
    if alerts.get("alerts"):
        reason = str(alerts["alerts"][0].get("code") or reason).lower()

    if live:
        _cancel_broker_order(order_repo.get_leg(tid, "TARGET"), live=True)
        oid, st = _place_exit_market(
            symbol=sym, exit_txn=exit_txn, quantity=qty, live=True,
        )
        _record_order(
            order_repo,
            trade_id=tid,
            step_num=3,
            leg="EXIT",
            quantity=qty,
            order_type=str(SCOUT_CONFIG.get("zerodha_exit_order_type", "MARKET")),
            transaction_type=exit_txn,
            product=str(SCOUT_CONFIG.get("zerodha_product", "MIS")),
            status=st,
            kite_order_id=oid,
            meta={"reason": reason, "unprotected": True},
        )

    return _close_trade(db, tid, exit_price=float(live_ltp), reason=reason)


def manage_open_trade_step3(
    db: SQLServerConnection,
    *,
    trade: dict,
    signal: dict,
    live_ltp: float,
    settings: dict,
    live: bool,
    catch_up: bool = False,
) -> Optional[dict]:
    """Step 3 — modify stop, sync broker fills, square-off exit."""
    status = str(trade.get("status") or "")
    if status == "UNPROTECTED":
        return _manage_unprotected_trade(
            db, trade=trade, signal=signal, live_ltp=live_ltp, settings=settings, live=live,
        )
    if status != "OPEN":
        return None
    trade_repo = ScoutTradeRepo(db)
    order_repo = ScoutTradeOrderRepo(db)
    tid = int(trade["id"])
    action = str(trade.get("action") or "BUY").upper()
    exit_txn = _exit_txn(action)
    sym = str(trade.get("symbol") or "").upper()
    qty = int(trade.get("quantity") or 1)
    entry = float(trade.get("entry_price") or 0)
    now = now_ist().replace(tzinfo=None)

    exit_plan = build_exit_plan(
        signal,
        entry_price=entry,
        executed_at=trade.get("executed_at"),
        live_ltp=float(live_ltp),
        now=now,
        settings=settings,
    )

    peak = trade.get("peak_price")
    peak_f = float(peak) if peak is not None else None
    if action == "BUY":
        new_peak = float(live_ltp) if peak_f is None else max(peak_f, float(live_ltp))
    else:
        new_peak = float(live_ltp) if peak_f is None else min(peak_f, float(live_ltp))
    if peak_f is None or new_peak != peak_f:
        trade_repo.update_peak_price(tid, peak_price=new_peak)

    alerts = evaluate_exit_alerts(
        action=action,
        live_ltp=float(live_ltp),
        exit_plan=exit_plan,
        entry_price=entry,
        peak_price=new_peak,
        settings=settings,
    )

    stop_row = order_repo.get_leg(tid, "STOP_LOSS")
    target_row = order_repo.get_leg(tid, "TARGET")

    if live:
        if stop_row:
            stop_row = _sync_order_status(order_repo, stop_row, live=True)
        if target_row:
            target_row = _sync_order_status(order_repo, target_row, live=True)

        if stop_row and str(stop_row.get("status") or "").upper() in _ORDER_COMPLETE:
            _cancel_broker_order(target_row, live=True)
            px = float(stop_row.get("price") or live_ltp)
            return _close_trade(db, tid, exit_price=px, reason="stop_hit")

        if target_row and str(target_row.get("status") or "").upper() in _ORDER_COMPLETE:
            _cancel_broker_order(stop_row, live=True)
            px = float(target_row.get("price") or live_ltp)
            return _close_trade(db, tid, exit_price=px, reason="target_hit")

    risk = exit_plan.get("risk_per_share")
    risk_f = float(risk) if risk is not None else None
    orig_stop = exit_plan.get("stop_price")
    if orig_stop is not None and risk_f and risk_f > 0:
        eff_stop, _armed = _effective_stop(
            action=action,
            entry=entry,
            original_stop=float(orig_stop),
            live_ltp=float(live_ltp),
            risk=risk_f,
            peak_price=new_peak,
            settings=settings,
        )
        prev = trade.get("effective_stop_price")
        prev_f = float(prev) if prev is not None else None
        if stop_row and (prev_f is None or abs(eff_stop - prev_f) >= 0.01):
            try:
                _modify_stop_order(stop_row, trigger_price=eff_stop, exit_txn=exit_txn, live=live)
                order_repo.update_status(
                    int(stop_row["id"]),
                    status=str(stop_row.get("status") or "OPEN"),
                    trigger_price=eff_stop,
                    price=_sl_limit_price(eff_stop, exit_txn),
                )
                trade_repo.update_effective_stop(tid, stop_price=eff_stop)
            except Exception as exc:
                logger.warning("TRD #%s stop modify failed: %s", tid, exc)

    if not alerts.get("close_now"):
        return None

    reason = "auto_close"
    if alerts.get("alerts"):
        reason = str(alerts["alerts"][0].get("code") or reason).lower()

    if live and reason in ("square_off_due", "target_hit", "stop_hit", "auto_close"):
        _cancel_broker_order(stop_row, live=True)
        _cancel_broker_order(target_row, live=True)
        if reason != "target_hit" or not (
            target_row and str(target_row.get("status") or "").upper() in _ORDER_COMPLETE
        ):
            oid, st = _place_exit_market(
                symbol=sym, exit_txn=exit_txn, quantity=qty, live=True,
            )
            _record_order(
                order_repo,
                trade_id=tid,
                step_num=3,
                leg="EXIT",
                quantity=qty,
                order_type=str(SCOUT_CONFIG.get("zerodha_exit_order_type", "MARKET")),
                transaction_type=exit_txn,
                product=str(SCOUT_CONFIG.get("zerodha_product", "MIS")),
                status=st,
                kite_order_id=oid,
                meta={"reason": reason},
            )

    return _close_trade(db, tid, exit_price=float(live_ltp), reason=reason)


def _close_trade(
    db: SQLServerConnection,
    trade_id: int,
    *,
    exit_price: float,
    reason: str,
) -> Optional[dict]:
    trade_repo = ScoutTradeRepo(db)
    result = trade_repo.close(
        trade_id,
        exit_price=float(exit_price),
        closed_at=now_ist(),
        exit_reason=reason,
    )
    if result:
        logger.info("Scout close TRD #%s @ %.2f (%s)", trade_id, float(exit_price), reason)
    return result


def paper_close_if_triggered(
    db: SQLServerConnection,
    *,
    trade: dict,
    signal: dict,
    live_ltp: float,
    settings: dict,
) -> Optional[dict]:
    """Paper mode Step 3 — LTP-based close, record simulated exit order."""
    out = manage_open_trade_step3(
        db,
        trade=trade,
        signal=signal,
        live_ltp=live_ltp,
        settings=settings,
        live=False,
    )
    if out:
        order_repo = ScoutTradeOrderRepo(db)
        action = str(trade.get("action") or "BUY").upper()
        if not order_repo.get_leg(int(trade["id"]), "EXIT"):
            _record_order(
                order_repo,
                trade_id=int(trade["id"]),
                step_num=3,
                leg="EXIT",
                quantity=int(trade.get("quantity") or 1),
                order_type="SIMULATED",
                transaction_type=_exit_txn(action),
                product=str(SCOUT_CONFIG.get("zerodha_product", "MIS")),
                price=float(live_ltp),
                status="SIMULATED",
                meta={"reason": out.get("exit_reason")},
            )
    return out
