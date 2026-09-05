"""
lifecycle/zerodha_executor.py
===============================

Place and monitor Zerodha (Kite) orders for suggestion entry and trade close.
DB updates (`mark_executed`, `close_trade_with_fills`) happen only after every
leg order reaches COMPLETE status.

Gated by ``ZERODHA_EXECUTION_CONFIG.enabled`` and runtime flag
``trade_execution_enabled``. Re-runs ``validate_execution`` and live price-band
checks before any order leaves the app.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from config import ZERODHA_API_CONFIG, ZERODHA_EXECUTION_CONFIG
from contracts import TradeLegFill
from database.broker_order_repo import BrokerOrderRepo
from database.connection import SQLServerConnection
from database.models import SuggestionRepo, TradeRepo
from database.runtime_flags import FLAG_TRADE_EXECUTION_ENABLED, RuntimeFlagsRepo
from engine.execution_validator import validate_execution
from engine.zerodha_price_guard import (
    leg_limit_in_band,
    validate_limit_prices,
    validate_live_prices,
)
from lifecycle.leg_execution_order import leg_execution_order, legs_in_execution_order
from lifecycle.trade_executor import (
    close_trade_with_fills,
    mark_executed,
    supplement_trade,
    _circuit_breaker_on,
)
from lifecycle.zerodha_execution_job import (
    submit_execution_job,
    update_job_progress,
)
from providers.zerodha.execution_checks import (
    build_order_margin_params,
    check_exposure_conflicts,
    check_margin_for_orders,
    reconcile_positions_after_fill,
)
from providers.zerodha.execution_facade import KiteExecutionFacade
from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import Instrument, InstrumentMaster
from providers.zerodha.order_pricing import (
    ExecutionProfile,
    fetch_quote_map,
    limit_from_reference,
    profile_for,
    reference_price,
    round_to_tick,
)
from providers.zerodha.order_updates import (
    parse_kite_order_row,
    persist_order_update,
    wait_for_order_terminal,
)
from providers.zerodha.session import is_token_valid, load_session
from utils import now_ist


logger = logging.getLogger(__name__)

EXECUTION_PROVIDER_ZERODHA = "zerodha"
EXECUTION_CHANNEL_ZERODHA = "zerodha"
EXECUTION_CHANNEL_MANUAL = "manual"

_TERMINAL_FAIL = frozenset({"REJECTED", "CANCELLED"})
_IN_FLIGHT = frozenset({"PENDING", "OPEN", "TRIGGER PENDING"})
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class ZerodhaExecutionError(Exception):
    """User-visible execution failure."""


class LegOrderPartialError(ZerodhaExecutionError):
    """Leg timed out or failed with a partial fill — caller must reverse qty."""

    def __init__(self, message: str, *, partial: "LegFillOutcome"):
        super().__init__(message)
        self.partial = partial


@dataclass
class LegFillOutcome:
    leg_order: int
    fill_price: float
    fill_time: datetime
    kite_order_id: str
    broker_row_id: int
    filled_quantity: int = 0
    planned_quantity: int = 0


@dataclass
class ExecutionOutcome:
    ok: bool
    trade_id: Optional[str] = None
    message: str = ""
    leg_fills: List[LegFillOutcome] = field(default_factory=list)
    broker_orders: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    job_id: Optional[int] = None
    async_started: bool = False


def zerodha_execution_config_enabled() -> bool:
    return bool(ZERODHA_EXECUTION_CONFIG.get("enabled", False))


def zerodha_execution_runtime_enabled(db: SQLServerConnection) -> bool:
    try:
        return RuntimeFlagsRepo(db).get_bool(
            FLAG_TRADE_EXECUTION_ENABLED, default=False,
        )
    except Exception:
        logger.debug("zerodha_executor: runtime flag read failed", exc_info=True)
        return False


def zerodha_execution_enabled(db: SQLServerConnection) -> bool:
    return zerodha_execution_config_enabled() and zerodha_execution_runtime_enabled(db)


def zerodha_session_ready() -> bool:
    session = load_session()
    return session is not None and is_token_valid(session)


def zerodha_execution_ready(db: SQLServerConnection) -> bool:
    if not zerodha_execution_enabled(db):
        return False
    if not ZERODHA_API_CONFIG.get("api_key"):
        return False
    return zerodha_session_ready()


def trade_execution_channel(db: SQLServerConnection, trade_row: dict) -> str:
    """Return ``zerodha`` or ``manual`` for dashboard badges."""
    provider = str(trade_row.get("execution_provider") or "").lower()
    trade_id = trade_row.get("trade_id")
    if provider == EXECUTION_PROVIDER_ZERODHA:
        return EXECUTION_CHANNEL_ZERODHA
    if trade_id and BrokerOrderRepo(db).has_kite_orders_for_trade(str(trade_id)):
        return EXECUTION_CHANNEL_ZERODHA
    return EXECUTION_CHANNEL_MANUAL


def _assert_entry_execution_allowed(db: SQLServerConnection, suggestion_id: str) -> None:
    orphans = BrokerOrderRepo(db).orphan_entry_fills(suggestion_id)
    if orphans:
        legs = ", ".join(str(r.get("leg_order")) for r in orphans)
        raise ZerodhaExecutionError(
            "Prior Zerodha entry leg(s) filled without a recorded trade "
            f"(leg(s) {legs}) — flatten on Kite and clear broker rows before retrying"
        )


def _alert_rollback_failure(
    db: SQLServerConnection,
    *,
    suggestion_id: Optional[str],
    trade_id: Optional[str],
    leg_orders: List[int],
    context: str,
) -> None:
    if not leg_orders:
        return
    try:
        from contracts import Notification
        from database.models import NotificationRepo

        leg_txt = ", ".join(str(lo) for lo in leg_orders)
        NotificationRepo(db).insert(Notification(
            created_at=now_ist(),
            notif_type="ZERODHA_ROLLBACK_FAILED",
            severity="CRITICAL",
            title="Zerodha rollback failed — manual action required",
            body=(
                f"{context}. Leg(s) {leg_txt} may still be open at the broker. "
                "Flatten on Kite before retrying."
            ),
            related_suggestion_id=suggestion_id,
            related_trade_id=trade_id,
        ))
        db.commit()
    except Exception:
        logger.exception("zerodha rollback alert failed")


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _build_client() -> Tuple[KiteExecutionFacade, InstrumentMaster]:
    if not ZERODHA_API_CONFIG.get("api_key"):
        raise ZerodhaExecutionError("OPT_ZERODHA_API_KEY is not configured")
    session = load_session()
    if session is None or not is_token_valid(session):
        raise ZerodhaExecutionError(
            "Zerodha session missing or expired — log in on the dashboard first"
        )
    read = KiteFacade(
        api_key=ZERODHA_API_CONFIG["api_key"],
        access_token=session.access_token,
    )
    facade = KiteExecutionFacade(
        api_key=ZERODHA_API_CONFIG["api_key"],
        access_token=session.access_token,
    )
    master = InstrumentMaster(loader=lambda: read.instruments("NFO"))
    master.refresh_if_stale()
    return facade, master


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return datetime.fromisoformat(str(v)).date()
    except (TypeError, ValueError):
        return None


def _normalize_side(action: str, *, leg_order: int) -> str:
    txn = str(action or "").strip().upper()
    if txn not in ("BUY", "SELL"):
        raise ZerodhaExecutionError(
            f"leg {leg_order}: invalid action {action!r} (must be BUY or SELL)"
        )
    return txn


def _transaction_type_for_leg(leg: dict, mode: str) -> str:
    """Single source for Kite ``transaction_type`` from leg ``action`` + mode."""
    lo = int(leg["leg_order"])
    orig = _normalize_side(str(leg.get("action") or ""), leg_order=lo)
    m = str(mode or "").lower()
    if m in ("close", "exit"):
        return "BUY" if orig == "SELL" else "SELL"
    if m == "entry":
        return orig
    if m == "rollback":
        return "SELL" if orig == "BUY" else "BUY"
    raise ZerodhaExecutionError(f"leg {lo}: unknown execution mode {mode!r}")


def _assert_instrument_matches_leg(leg: dict, inst: Instrument) -> None:
    """Fail closed if resolved Kite instrument does not match DB leg identity."""
    lo = int(leg["leg_order"])
    opt = str(leg.get("option_type") or "").upper()
    if opt and inst.instrument_type.upper() != opt:
        raise ZerodhaExecutionError(
            f"leg {lo}: option type mismatch — expected {opt}, "
            f"got {inst.tradingsymbol} ({inst.instrument_type})"
        )
    strike = float(leg["strike"])
    if abs(inst.strike - strike) > 0.001:
        raise ZerodhaExecutionError(
            f"leg {lo}: strike mismatch — expected {strike}, "
            f"got {inst.tradingsymbol} ({inst.strike})"
        )
    sym = str(leg.get("symbol") or leg.get("underlying") or "NIFTY").upper()
    if inst.name.upper() != sym:
        raise ZerodhaExecutionError(
            f"leg {lo}: symbol mismatch — expected {sym}, got {inst.name}"
        )
    exp = _as_date(leg.get("expiry_date") or leg.get("expiry"))
    if exp and inst.expiry and inst.expiry != exp:
        raise ZerodhaExecutionError(
            f"leg {lo}: expiry mismatch — expected {exp}, got {inst.expiry}"
        )


def _resolve_instrument(leg: dict, master: InstrumentMaster) -> Instrument:
    sym = str(leg.get("symbol") or leg.get("underlying") or "NIFTY").upper()
    expiry = _as_date(leg.get("expiry_date") or leg.get("expiry"))
    if expiry is None:
        raise ZerodhaExecutionError(f"leg {leg.get('leg_order')}: missing expiry")
    strike = float(leg["strike"])
    opt = str(leg.get("option_type") or "").upper()
    inst = master.get_option(sym, expiry, strike, opt)
    if inst is None:
        master.refresh()
        inst = master.get_option(sym, expiry, strike, opt)
    if inst is None:
        raise ZerodhaExecutionError(
            f"leg {leg.get('leg_order')}: instrument not found for "
            f"{sym} {expiry} {strike} {opt}"
        )
    _assert_instrument_matches_leg(leg, inst)
    return inst


def _kite_symbol_key(inst: Instrument) -> str:
    return f"{inst.exchange}:{inst.tradingsymbol}"


def _round_to_tick(price: float, tick: float) -> float:
    return round_to_tick(price, tick)


def _limit_price(ltp: float, transaction_type: str, *, slippage_pct: float) -> float:
    slip = slippage_pct / 100.0
    txn = transaction_type.upper()
    if txn == "BUY":
        return ltp * (1.0 + slip)
    return ltp * (1.0 - slip)


def _resolve_limit_price(
    ltp: float,
    transaction_type: str,
    inst: Instrument,
    user_limit: Optional[float] = None,
    *,
    quote_row: Optional[dict] = None,
    profile: Optional[ExecutionProfile] = None,
    attempt: int = 0,
) -> float:
    """Use ``user_limit`` when provided; otherwise bid/ask ± configured slippage."""
    prof = profile or profile_for("entry")
    use_ba = bool(ZERODHA_EXECUTION_CONFIG.get("use_bid_ask_pricing", True))
    ref = reference_price(
        ltp=ltp,
        quote_row=quote_row,
        transaction_type=transaction_type,
        use_bid_ask=use_ba,
    )
    return limit_from_reference(
        ref,
        transaction_type,
        inst,
        slippage_pct=prof.slippage_pct,
        attempt=attempt,
        slip_walk_per_retry=prof.slip_walk_per_retry,
        user_limit=user_limit,
    )


def parse_leg_limits(raw: Any) -> Dict[int, float]:
    """Parse API payload ``leg_limits`` — list of {leg_order, limit_price} or dict."""
    out: Dict[int, float] = {}
    if not raw:
        return out
    if isinstance(raw, dict):
        for key, val in raw.items():
            if val is None:
                continue
            out[int(key)] = float(val)
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        lo = item.get("leg_order")
        lp = item.get("limit_price")
        if lo is None or lp is None:
            continue
        out[int(lo)] = float(lp)
    return out


def _fetch_ltps(facade: KiteExecutionFacade, keys: List[str]) -> Dict[str, float]:
    if not keys:
        return {}
    try:
        raw = facade.ltp(keys)
    except Exception as exc:
        raise ZerodhaExecutionError(f"Failed to fetch live prices: {exc}") from exc
    out: Dict[str, float] = {}
    for key in keys:
        row = raw.get(key) or {}
        lp = row.get("last_price")
        if lp is not None:
            out[key] = float(lp)
    return out


def _latest_order_row(history: List[dict]) -> dict:
    return history[-1] if history else {}


def _kite_order_is_dead(facade: KiteExecutionFacade, order_id: str) -> bool:
    """True when the broker order can no longer be modified (must place a new one)."""
    try:
        history = facade.order_history(order_id)
    except Exception:
        return False
    if not history:
        return False
    st = str(_latest_order_row(history).get("status") or "").upper()
    return st in _TERMINAL_FAIL or st == "COMPLETE"


def _sync_broker_from_kite_row(
    broker: BrokerOrderRepo,
    row_id: int,
    kite_row: dict,
    *,
    db: SQLServerConnection,
) -> dict:
    snap = parse_kite_order_row(kite_row)
    persist_order_update(snap)
    broker.update_status(
        row_id,
        status=snap["status"],
        fill_price=float(snap["average_price"]) if snap.get("average_price") is not None else None,
        filled_quantity=snap.get("filled_quantity"),
        pending_quantity=snap.get("pending_quantity"),
        status_message=(snap.get("status_message") or "")[:500] or None,
        updated_at=now_ist(),
    )
    db.commit()
    return snap


def _wait_for_order_complete(
    facade: KiteExecutionFacade,
    order_id: str,
    *,
    broker: Optional[BrokerOrderRepo] = None,
    broker_row_id: Optional[int] = None,
    db: Optional[SQLServerConnection] = None,
) -> Tuple[str, float, dict]:
    poll = float(ZERODHA_EXECUTION_CONFIG.get("order_poll_interval_sec", 2))
    max_wait = float(ZERODHA_EXECUTION_CONFIG.get("order_max_wait_sec", 45))
    use_ws = bool(ZERODHA_EXECUTION_CONFIG.get("use_ws_order_updates", True))
    try:
        snap = wait_for_order_terminal(
            order_id,
            facade=facade,
            max_wait=max_wait,
            poll_interval=poll,
            use_ws_cache=use_ws,
        )
    except TimeoutError as exc:
        raise ZerodhaExecutionError(str(exc)) from exc

    if broker is not None and broker_row_id is not None and db is not None:
        kite_row = snap.get("raw") if isinstance(snap.get("raw"), dict) else snap
        _sync_broker_from_kite_row(broker, broker_row_id, kite_row, db=db)

    status = str(snap.get("status") or "").upper()
    filled = int(snap.get("filled_quantity") or 0)
    if status == "COMPLETE":
        avg = snap.get("average_price") or snap.get("price")
        if avg is None:
            raise ZerodhaExecutionError(
                f"Order {order_id} complete but average_price missing"
            )
        return status, float(avg), snap
    if status in _TERMINAL_FAIL:
        msg = snap.get("status_message") or status
        if filled > 0:
            avg = snap.get("average_price") or snap.get("price") or 0.0
            return status, float(avg), snap
        raise ZerodhaExecutionError(f"Order {order_id} {status}: {msg}")
    if filled > 0:
        avg = snap.get("average_price") or snap.get("price") or 0.0
        return status, float(avg), snap
    raise ZerodhaExecutionError(
        f"Order {order_id} ended in unexpected state {status}"
    )


def _place_and_monitor_leg(
    db: SQLServerConnection,
    facade: KiteExecutionFacade,
    *,
    operation: str,
    leg: dict,
    inst: Instrument,
    transaction_type: str,
    ltp: float,
    suggestion_id: Optional[str],
    trade_id: Optional[str],
    user_limit_price: Optional[float] = None,
    expected_transaction_type: Optional[str] = None,
    execution_profile: Optional[ExecutionProfile] = None,
    execution_job_id: Optional[int] = None,
    quote_row: Optional[dict] = None,
    quantity_override: Optional[int] = None,
) -> LegFillOutcome:
    cfg = ZERODHA_EXECUTION_CONFIG
    profile = execution_profile or profile_for(
        "rollback" if operation == "ROLLBACK" else operation.lower(),
    )
    lo = int(leg["leg_order"])
    txn = str(transaction_type or "").strip().upper()
    if txn not in ("BUY", "SELL"):
        raise ZerodhaExecutionError(f"leg {lo}: invalid transaction_type {transaction_type!r}")
    if expected_transaction_type and txn != expected_transaction_type.upper():
        raise ZerodhaExecutionError(
            f"leg {lo}: refused order — expected {expected_transaction_type}, got {txn}"
        )
    _assert_instrument_matches_leg(leg, inst)
    lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
    qty = quantity_override if quantity_override is not None else (
        lots * int(inst.lot_size or leg.get("lot_size") or 0)
    )
    if qty <= 0:
        raise ZerodhaExecutionError(f"leg {leg['leg_order']}: invalid quantity")

    fixed_user_limit = user_limit_price is not None
    now = now_ist()
    broker = BrokerOrderRepo(db)
    order_type = profile.order_type
    limit_px = _resolve_limit_price(
        ltp, transaction_type, inst, user_limit_price,
        quote_row=quote_row, profile=profile, attempt=0,
    )
    row_id = broker.insert({
        "operation": operation,
        "suggestion_id": suggestion_id,
        "trade_id": trade_id,
        "leg_order": lo,
        "tradingsymbol": inst.tradingsymbol,
        "exchange": inst.exchange,
        "transaction_type": transaction_type,
        "quantity": qty,
        "limit_price": limit_px if order_type == "LIMIT" else None,
        "status": "PENDING",
        "tag": None,
        "order_type": order_type,
        "validity": profile.validity,
        "execution_job_id": execution_job_id,
        "created_at": now,
        "updated_at": now,
    })
    db.commit()

    tag = f"oa{row_id}"[:20]
    broker.update_status(row_id, status="OPEN", error_message=None, updated_at=now_ist())
    db.commit()

    max_retries = profile.max_retries
    last_exc: Optional[Exception] = None
    order_id: Optional[str] = None
    variety = str(cfg.get("variety", "regular"))
    product = str(cfg.get("product", "NRML"))

    for attempt in range(max_retries + 1):
        use_market = (
            profile.allow_market_fallback
            and attempt == max_retries
            and order_type == "LIMIT"
        )
        try:
            if quote_row is None and not fixed_user_limit:
                qmap = fetch_quote_map(facade, [_kite_symbol_key(inst)])
                quote_row = qmap.get(_kite_symbol_key(inst))
            if not use_market:
                limit_px = _resolve_limit_price(
                    ltp, transaction_type, inst, user_limit_price,
                    quote_row=quote_row, profile=profile, attempt=attempt,
                )
            eff_type = "MARKET" if use_market else order_type
            place_kwargs = {
                "variety": variety,
                "exchange": inst.exchange,
                "tradingsymbol": inst.tradingsymbol,
                "transaction_type": transaction_type,
                "quantity": qty,
                "product": product,
                "order_type": eff_type,
                "validity": profile.validity,
                "tag": tag,
            }
            if eff_type == "LIMIT":
                place_kwargs["price"] = limit_px

            if order_id is None:
                order_id = facade.place_order(**place_kwargs)
                broker.update_status(
                    row_id,
                    status="OPEN",
                    kite_order_id=order_id,
                    retry_count=attempt,
                    order_type=eff_type,
                    limit_price=limit_px if eff_type == "LIMIT" else None,
                    updated_at=now_ist(),
                )
                db.commit()
            else:
                if use_market:
                    try:
                        facade.cancel_order(order_id=order_id, variety=variety)
                    except Exception:
                        logger.debug("cancel before market fallback failed", exc_info=True)
                    order_id = facade.place_order(**place_kwargs)
                    broker.update_status(
                        row_id,
                        status="OPEN",
                        kite_order_id=order_id,
                        retry_count=attempt,
                        order_type=eff_type,
                        updated_at=now_ist(),
                    )
                    db.commit()
                else:
                    facade.modify_order(
                        order_id=order_id,
                        variety=variety,
                        price=limit_px,
                        validity=profile.validity,
                    )
                    broker.update_status(
                        row_id,
                        status="OPEN",
                        retry_count=attempt,
                        limit_price=limit_px,
                        updated_at=now_ist(),
                    )
                    db.commit()

            status, fill_px, snap = _wait_for_order_complete(
                facade, order_id,
                broker=broker, broker_row_id=row_id, db=db,
            )
            filled_qty = int(snap.get("filled_quantity") or 0)
            if status == "COMPLETE" and filled_qty <= 0:
                filled_qty = qty
            if status == "COMPLETE" and filled_qty >= qty:
                filled_at = now_ist()
                broker.update_status(
                    row_id,
                    status=status,
                    fill_price=fill_px,
                    filled_quantity=filled_qty,
                    pending_quantity=0,
                    updated_at=filled_at,
                )
                db.commit()
                return LegFillOutcome(
                    leg_order=lo,
                    fill_price=fill_px,
                    fill_time=filled_at,
                    kite_order_id=order_id,
                    broker_row_id=row_id,
                    filled_quantity=filled_qty,
                    planned_quantity=qty,
                )
            if filled_qty > 0:
                try:
                    facade.cancel_order(order_id=order_id, variety=variety)
                except Exception:
                    logger.debug("cancel partial remainder failed", exc_info=True)
                filled_at = now_ist()
                broker.update_status(
                    row_id,
                    status="PARTIAL",
                    fill_price=fill_px,
                    filled_quantity=filled_qty,
                    pending_quantity=max(0, qty - filled_qty),
                    error_message=f"Partial fill {filled_qty}/{qty}",
                    updated_at=filled_at,
                )
                db.commit()
                partial = LegFillOutcome(
                    leg_order=lo,
                    fill_price=fill_px,
                    fill_time=filled_at,
                    kite_order_id=order_id,
                    broker_row_id=row_id,
                    filled_quantity=filled_qty,
                    planned_quantity=qty,
                )
                raise LegOrderPartialError(
                    f"leg {lo}: partial fill {filled_qty}/{qty} before failure",
                    partial=partial,
                )
            raise ZerodhaExecutionError(
                f"Order {order_id} ended {status} with no fill"
            )
        except LegOrderPartialError:
            raise
        except ZerodhaExecutionError as exc:
            last_exc = exc
            logger.warning(
                "zerodha leg %s attempt %d/%d failed: %s",
                lo, attempt + 1, max_retries + 1, exc,
            )
            if order_id and attempt < max_retries:
                if _kite_order_is_dead(facade, order_id):
                    order_id = None
                if not fixed_user_limit:
                    try:
                        keys = [_kite_symbol_key(inst)]
                        fresh = _fetch_ltps(facade, keys).get(keys[0])
                        if fresh is not None:
                            ltp = fresh
                            qmap = fetch_quote_map(facade, keys)
                            quote_row = qmap.get(keys[0])
                    except Exception:
                        logger.debug("refresh LTP for retry failed", exc_info=True)
                broker.update_status(
                    row_id,
                    status="OPEN",
                    error_message=str(exc)[:500],
                    retry_count=attempt + 1,
                    updated_at=now_ist(),
                )
                db.commit()
                continue
            broker.update_status(
                row_id,
                status="FAILED",
                error_message=str(exc)[:500],
                retry_count=attempt + 1,
                updated_at=now_ist(),
            )
            db.commit()
            if order_id:
                try:
                    history = facade.order_history(order_id)
                    if history:
                        snap = parse_kite_order_row(history[-1])
                        if int(snap.get("filled_quantity") or 0) > 0:
                            raise LegOrderPartialError(str(exc), partial=LegFillOutcome(
                                leg_order=lo,
                                fill_price=float(snap.get("average_price") or 0),
                                fill_time=now_ist(),
                                kite_order_id=order_id,
                                broker_row_id=row_id,
                                filled_quantity=int(snap.get("filled_quantity") or 0),
                                planned_quantity=qty,
                            ))
                    facade.cancel_order(order_id=order_id, variety=variety)
                except LegOrderPartialError:
                    raise
                except Exception:
                    logger.debug("cancel after failure failed", exc_info=True)
            raise
        except Exception as exc:
            broker.update_status(
                row_id,
                status="FAILED",
                error_message=str(exc)[:500],
                updated_at=now_ist(),
            )
            db.commit()
            if order_id:
                try:
                    facade.cancel_order(order_id=order_id, variety=variety)
                except Exception:
                    logger.debug("cancel after unexpected error failed", exc_info=True)
            raise ZerodhaExecutionError(str(exc)) from exc

    raise ZerodhaExecutionError(str(last_exc or "order placement failed"))


def _reverse_partial_leg(
    facade: KiteExecutionFacade,
    db: SQLServerConnection,
    *,
    partial: LegFillOutcome,
    leg: dict,
    inst: Instrument,
    suggestion_id: Optional[str],
    trade_id: Optional[str],
    mode: str,
    inst_map: Optional[Dict[int, Instrument]] = None,
) -> None:
    """Reverse a partial fill on the failing leg."""
    if partial.filled_quantity <= 0:
        return
    if mode == "exit":
        close_txn = _transaction_type_for_leg(leg, "close")
        reverse_txn = "SELL" if close_txn == "BUY" else "BUY"
    else:
        reverse_txn = _transaction_type_for_leg(leg, "rollback")
    broker = BrokerOrderRepo(db)
    if broker.rollback_complete_for_leg(
        suggestion_id=suggestion_id,
        trade_id=trade_id,
        leg_order=partial.leg_order,
    ):
        return
    key = _kite_symbol_key(inst)
    ltp = _fetch_ltps(facade, [key]).get(key)
    if ltp is None:
        logger.error("partial reverse: no LTP for leg %s", partial.leg_order)
        return
    try:
        _place_and_monitor_leg(
            db, facade,
            operation="ROLLBACK",
            leg=leg,
            inst=inst,
            transaction_type=reverse_txn,
            ltp=ltp,
            suggestion_id=suggestion_id,
            trade_id=trade_id,
            expected_transaction_type=reverse_txn,
            execution_profile=profile_for("rollback"),
            quantity_override=partial.filled_quantity,
        )
    except Exception:
        logger.exception(
            "partial reverse failed for leg %s — manual cleanup required",
            partial.leg_order,
        )
        raise


def _rollback_filled_legs(
    facade: KiteExecutionFacade,
    db: SQLServerConnection,
    *,
    suggestion_id: Optional[str],
    trade_id: Optional[str],
    legs_by_order: Dict[int, dict],
    completed: List[LegFillOutcome],
    mode: str,
    inst_map: Optional[Dict[int, Instrument]] = None,
    partial_on_fail: Optional[LegFillOutcome] = None,
) -> List[int]:
    """Best-effort reverse orders for legs already filled before a failure."""
    if not completed and partial_on_fail is None:
        return []
    logger.warning(
        "Rolling back %d filled leg(s) (%s) for suggestion=%s trade=%s",
        len(completed), mode, suggestion_id, trade_id,
    )
    master: Optional[InstrumentMaster] = None
    failed: List[int] = []
    broker = BrokerOrderRepo(db)
    for fill in reversed(completed):
        leg = legs_by_order[fill.leg_order]
        lo = int(leg["leg_order"])
        if broker.rollback_complete_for_leg(
            suggestion_id=suggestion_id,
            trade_id=trade_id,
            leg_order=lo,
        ):
            continue
        inst = (inst_map or {}).get(lo)
        if inst is None:
            if master is None:
                _, master = _build_client()
            inst = _resolve_instrument(leg, master)
        if mode == "exit":
            close_txn = _transaction_type_for_leg(leg, "close")
            reverse_txn = "SELL" if close_txn == "BUY" else "BUY"
        else:
            reverse_txn = _transaction_type_for_leg(leg, "rollback")
        key = _kite_symbol_key(inst)
        ltp = _fetch_ltps(facade, [key]).get(key)
        if ltp is None:
            logger.error("rollback: no LTP for leg %s", fill.leg_order)
            failed.append(fill.leg_order)
            continue
        try:
            _place_and_monitor_leg(
                db, facade,
                operation="ROLLBACK",
                leg=leg,
                inst=inst,
                transaction_type=reverse_txn,
                ltp=ltp,
                suggestion_id=suggestion_id,
                trade_id=trade_id,
                expected_transaction_type=reverse_txn,
                execution_profile=profile_for("rollback"),
                quantity_override=(
                    fill.filled_quantity if fill.filled_quantity > 0 else fill.planned_quantity or None
                ),
            )
        except Exception:
            logger.exception(
                "rollback failed for leg %s — manual intervention required",
                fill.leg_order,
            )
            failed.append(fill.leg_order)
    if partial_on_fail is not None:
        leg = legs_by_order.get(partial_on_fail.leg_order)
        if leg:
            inst = (inst_map or {}).get(int(leg["leg_order"]))
            if inst is None:
                if master is None:
                    _, master = _build_client()
                inst = _resolve_instrument(leg, master)
            try:
                _reverse_partial_leg(
                    facade, db,
                    partial=partial_on_fail,
                    leg=leg,
                    inst=inst,
                    suggestion_id=suggestion_id,
                    trade_id=trade_id,
                    mode=mode,
                    inst_map=inst_map,
                )
            except Exception:
                failed.append(partial_on_fail.leg_order)
    if failed:
        ctx = (
            f"Entry rollback failed after a leg error (suggestion {suggestion_id})"
            if mode == "entry"
            else f"Close rollback failed after a leg error (trade {trade_id})"
        )
        _alert_rollback_failure(
            db,
            suggestion_id=suggestion_id,
            trade_id=trade_id,
            leg_orders=failed,
            context=ctx,
        )
    return failed


def _rollback_entry_legs(
    facade: KiteExecutionFacade,
    db: SQLServerConnection,
    *,
    suggestion_id: str,
    legs_by_order: Dict[int, dict],
    completed: List[LegFillOutcome],
    inst_map: Optional[Dict[int, Instrument]] = None,
    partial_on_fail: Optional[LegFillOutcome] = None,
) -> None:
    _rollback_filled_legs(
        facade, db,
        suggestion_id=suggestion_id,
        trade_id=None,
        legs_by_order=legs_by_order,
        completed=completed,
        mode="entry",
        inst_map=inst_map,
        partial_on_fail=partial_on_fail,
    )


def _rollback_close_legs(
    facade: KiteExecutionFacade,
    db: SQLServerConnection,
    *,
    trade_id: str,
    suggestion_id: Optional[str],
    legs_by_order: Dict[int, dict],
    completed: List[LegFillOutcome],
    inst_map: Optional[Dict[int, Instrument]] = None,
    partial_on_fail: Optional[LegFillOutcome] = None,
) -> None:
    _rollback_filled_legs(
        facade, db,
        suggestion_id=suggestion_id,
        trade_id=trade_id,
        legs_by_order=legs_by_order,
        completed=completed,
        mode="exit",
        inst_map=inst_map,
        partial_on_fail=partial_on_fail,
    )


def _refresh_leg_ltp(
    facade: KiteExecutionFacade,
    inst_map: Dict[int, Instrument],
    leg: dict,
) -> float:
    lo = int(leg["leg_order"])
    inst = inst_map[lo]
    key = _kite_symbol_key(inst)
    ltp = _fetch_ltps(facade, [key]).get(key)
    if ltp is None:
        raise ZerodhaExecutionError(f"leg {lo}: live price unavailable")
    return ltp


def _live_ltp_map(
    facade: KiteExecutionFacade,
    master: InstrumentMaster,
    legs: List[dict],
) -> Tuple[Dict[int, float], Dict[int, Instrument]]:
    inst_by_leg: Dict[int, Instrument] = {}
    keys: List[str] = []
    for leg in legs:
        inst = _resolve_instrument(leg, master)
        inst_by_leg[int(leg["leg_order"])] = inst
        keys.append(_kite_symbol_key(inst))
    ltps = _fetch_ltps(facade, keys)
    live: Dict[int, float] = {}
    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_by_leg[lo]
        lp = ltps.get(_kite_symbol_key(inst))
        if lp is not None:
            live[lo] = lp
    return live, inst_by_leg


def _spot_ltp(facade: KiteExecutionFacade) -> Optional[float]:
    try:
        raw = facade.ltp(["NSE:NIFTY 50"])
        row = raw.get("NSE:NIFTY 50") or {}
        lp = row.get("last_price")
        return float(lp) if lp is not None else None
    except Exception:
        logger.debug("NIFTY spot fetch failed", exc_info=True)
        return None


@dataclass
class LegExecutionPlan:
    leg_order: int
    execution_step: int
    action: str
    transaction_type: str
    tradingsymbol: str
    exchange: str
    quantity: int
    lots: int
    lot_size: int
    ltp: float
    limit_price: float
    auto_priced: bool
    suggested_price: Optional[float]
    band_lo: Optional[float]
    band_hi: Optional[float]
    in_band: bool

    def to_dict(self) -> dict:
        return {
            "leg_order": self.leg_order,
            "execution_step": self.execution_step,
            "action": self.action,
            "transaction_type": self.transaction_type,
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "quantity": self.quantity,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "ltp": self.ltp,
            "limit_price": self.limit_price,
            "auto_priced": self.auto_priced,
            "suggested_price": self.suggested_price,
            "band_lo": self.band_lo,
            "band_hi": self.band_hi,
            "in_band": self.in_band,
        }


@dataclass
class ExecutionPreview:
    operation: str
    suggestion_id: Optional[str]
    trade_id: Optional[str]
    trade_name: Optional[str]
    strategy: Optional[str]
    legs: List[LegExecutionPlan]
    all_limits_in_band: bool
    limit_vetoes: List[str]
    spot_at_execution: Optional[float]

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "suggestion_id": self.suggestion_id,
            "trade_id": self.trade_id,
            "trade_name": self.trade_name,
            "strategy": self.strategy,
            "legs": [l.to_dict() for l in self.legs],
            "all_limits_in_band": self.all_limits_in_band,
            "limit_vetoes": self.limit_vetoes,
            "spot_at_execution": self.spot_at_execution,
        }


def _leg_band_fields(leg: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    sug = leg.get("suggested_price")
    blo = leg.get("suggested_price_low")
    bhi = leg.get("suggested_price_high")
    return (
        float(sug) if sug is not None else None,
        float(blo) if blo is not None else None,
        float(bhi) if bhi is not None else None,
    )


def _build_leg_plans(
    ordered_legs: List[dict],
    inst_map: Dict[int, Instrument],
    live_map: Dict[int, float],
    leg_limits: Optional[Dict[int, float]],
    *,
    mode: str,
    strategy: str = "",
) -> List[LegExecutionPlan]:
    order_map = leg_execution_order(ordered_legs, strategy, mode=mode) if len(ordered_legs) > 1 else {}
    plans: List[LegExecutionPlan] = []
    for leg in ordered_legs:
        lo = int(leg["leg_order"])
        inst = inst_map[lo]
        ltp = live_map.get(lo)
        if ltp is None:
            raise ZerodhaExecutionError(f"leg {lo}: live price unavailable")
        if mode == "close":
            txn = _transaction_type_for_leg(leg, "close")
        else:
            txn = _transaction_type_for_leg(leg, "entry")
        user_lim = (leg_limits or {}).get(lo)
        auto = user_lim is None
        limit_px = _resolve_limit_price(ltp, txn, inst, user_lim)
        lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
        qty = lots * int(inst.lot_size or leg.get("lot_size") or 0)
        if qty <= 0:
            raise ZerodhaExecutionError(f"leg {lo}: invalid quantity")
        sug, blo, bhi = _leg_band_fields(leg)
        plans.append(LegExecutionPlan(
            leg_order=lo,
            execution_step=order_map.get(lo, 1),
            action=str(leg.get("action") or ""),
            transaction_type=txn,
            tradingsymbol=inst.tradingsymbol,
            exchange=inst.exchange,
            quantity=qty,
            lots=lots,
            lot_size=int(inst.lot_size or leg.get("lot_size") or 0),
            ltp=ltp,
            limit_price=limit_px,
            auto_priced=auto,
            suggested_price=sug,
            band_lo=blo,
            band_hi=bhi,
            in_band=leg_limit_in_band(leg, limit_px),
        ))
    return plans


def _run_pre_trade_checks(
    facade: KiteExecutionFacade,
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    ordered: List[dict],
    leg_limits: Optional[Dict[int, float]],
    *,
    mode: str = "entry",
    allow_existing_positions: bool = False,
    live_map: Optional[Dict[int, float]] = None,
) -> None:
    cfg = ZERODHA_EXECUTION_CONFIG
    txn_fn = (
        (lambda leg: _transaction_type_for_leg(leg, "entry"))
        if mode == "entry"
        else (lambda leg: _transaction_type_for_leg(leg, "close"))
    )

    def _limit_for(lo: int, inst: Instrument, txn: str) -> float:
        user = (leg_limits or {}).get(lo)
        ltp = (live_map or {}).get(lo)
        if ltp is None or ltp <= 0:
            ltp = 1.0
        return _resolve_limit_price(ltp, txn, inst, user, profile=profile_for(mode))

    if mode == "entry":
        margin_params = build_order_margin_params(
            ordered,
            inst_map,
            transaction_fn=txn_fn,
            limit_fn=_limit_for,
            product=str(cfg.get("product", "NRML")),
            variety=str(cfg.get("variety", "regular")),
        )
        margin = check_margin_for_orders(facade, margin_params)
        if not margin.ok:
            raise ZerodhaExecutionError(margin.message)

        exposure = check_exposure_conflicts(
            facade, ordered, inst_map, transaction_fn=txn_fn,
            allow_existing_positions=allow_existing_positions,
        )
        if not exposure.ok:
            raise ZerodhaExecutionError(exposure.message)


def _assert_execution_not_in_flight(
    db: SQLServerConnection,
    *,
    suggestion_id: Optional[str] = None,
    trade_id: Optional[str] = None,
) -> None:
    from database.zerodha_execution_job_repo import ZerodhaExecutionJobRepo

    broker = BrokerOrderRepo(db)
    jobs = ZerodhaExecutionJobRepo(db)
    if suggestion_id:
        pending = broker.pending_for_suggestion(suggestion_id)
        if pending:
            raise ZerodhaExecutionError(
                "Broker orders already in flight for this suggestion — wait or cancel on Kite"
            )
        running = jobs.running_for_suggestion(suggestion_id)
        if running is not None:
            raise ZerodhaExecutionError(
                "Zerodha execution already running for this suggestion"
            )
    if trade_id:
        pending = broker.pending_for_trade(trade_id)
        if pending:
            raise ZerodhaExecutionError(
                "Broker orders already in flight for this trade — wait or cancel on Kite"
            )
        running = jobs.running_for_trade(trade_id)
        if running is not None:
            raise ZerodhaExecutionError(
                "Zerodha execution already running for this trade"
            )


def _entry_context(
    db: SQLServerConnection,
    suggestion_id: str,
    leg_limits: Optional[Dict[int, float]],
) -> Tuple[dict, List[dict], KiteExecutionFacade, InstrumentMaster, Dict[int, float], Dict[int, Instrument], List[dict], str]:
    sug = SuggestionRepo(db)
    suggestion = sug.get(suggestion_id)
    if suggestion is None:
        raise ZerodhaExecutionError(f"Unknown suggestion: {suggestion_id}")
    legs = sug.legs(suggestion_id)
    if not legs:
        raise ZerodhaExecutionError("Suggestion has no legs")
    status = (suggestion.get("status") or "").upper()
    if status != "PENDING":
        raise ZerodhaExecutionError(
            f"Suggestion status is {status!r} — only PENDING can be executed"
        )
    _assert_execution_not_in_flight(db, suggestion_id=suggestion_id)
    _assert_entry_execution_allowed(db, suggestion_id)
    cb_active = _circuit_breaker_on(db)
    gate = validate_execution(suggestion, legs, circuit_breaker_active=cb_active)
    if not gate.ok:
        raise ZerodhaExecutionError(f"Execution blocked: {gate.reason()}")
    facade, master = _build_client()
    live_map, inst_map = _live_ltp_map(facade, master, legs)
    price_gate = validate_live_prices(legs, live_map)
    if not price_gate.ok:
        raise ZerodhaExecutionError(
            f"Live prices out of band: {price_gate.reason()}"
        )
    strategy = str(suggestion.get("strategy") or "")
    ordered = legs_in_execution_order(legs, strategy, mode="entry")
    _run_pre_trade_checks(
        facade, legs, inst_map, ordered, leg_limits, mode="entry",
        live_map=live_map,
    )
    return suggestion, legs, facade, master, live_map, inst_map, ordered, strategy


def preview_suggestion_execution(
    db: SQLServerConnection,
    suggestion_id: str,
    *,
    leg_limits: Optional[Dict[int, float]] = None,
    spot_at_execution: Optional[float] = None,
) -> ExecutionPreview:
    if not zerodha_execution_enabled(db):
        raise ZerodhaExecutionError("Zerodha execution is disabled")
    suggestion, legs, facade, _master, live_map, inst_map, ordered, strategy = (
        _entry_context(db, suggestion_id, leg_limits)
    )
    plans = _build_leg_plans(ordered, inst_map, live_map, leg_limits, mode="entry", strategy=strategy)
    limit_map = {p.leg_order: p.limit_price for p in plans}
    limit_gate = validate_limit_prices(legs, limit_map)
    spot = spot_at_execution
    if spot is None:
        spot = _spot_ltp(facade)
    if spot is None and suggestion.get("spot_at_generation") is not None:
        spot = float(suggestion["spot_at_generation"])
    return ExecutionPreview(
        operation="ENTRY",
        suggestion_id=suggestion_id,
        trade_id=None,
        trade_name=suggestion.get("trade_name"),
        strategy=strategy,
        legs=plans,
        all_limits_in_band=limit_gate.ok,
        limit_vetoes=limit_gate.vetoes,
        spot_at_execution=spot,
    )


def _enforce_limit_band(
    legs: List[dict],
    plans: List[LegExecutionPlan],
    *,
    ack_out_of_band: bool,
    system_plans: Optional[List[LegExecutionPlan]] = None,
) -> None:
    limit_map = {p.leg_order: p.limit_price for p in plans}
    limit_gate = validate_limit_prices(legs, limit_map)
    if limit_gate.ok:
        return
    if not ack_out_of_band:
        raise ZerodhaExecutionError(
            "Limit prices outside suggestion band: "
            + limit_gate.reason()
            + " — review the confirmation preview or set ack_out_of_band to proceed"
        )
    # Honor "Proceed anyway" only when the system-priced walk is also OOB.
    # Custom user limits that wander off-band while defaults stay in-band
    # must be corrected, not acked through.
    ref = system_plans if system_plans is not None else plans
    system_gate = validate_limit_prices(
        legs, {p.leg_order: p.limit_price for p in ref},
    )
    if system_gate.ok:
        raise ZerodhaExecutionError(
            "Custom limits are outside the suggestion band while system-priced "
            "limits are in band — adjust the prices or omit custom limits"
        )
def _handle_leg_failure(
    facade: KiteExecutionFacade,
    db: SQLServerConnection,
    exc: Exception,
    *,
    suggestion_id: Optional[str],
    trade_id: Optional[str],
    legs_by_order: Dict[int, dict],
    completed: List[LegFillOutcome],
    inst_map: Dict[int, Instrument],
    mode: str,
) -> None:
    partial: Optional[LegFillOutcome] = None
    if isinstance(exc, LegOrderPartialError):
        partial = exc.partial
    if mode == "entry":
        _rollback_entry_legs(
            facade, db,
            suggestion_id=suggestion_id or "",
            legs_by_order=legs_by_order,
            completed=completed,
            inst_map=inst_map,
            partial_on_fail=partial,
        )
    else:
        _rollback_close_legs(
            facade, db,
            trade_id=trade_id or "",
            suggestion_id=suggestion_id,
            legs_by_order=legs_by_order,
            completed=completed,
            inst_map=inst_map,
            partial_on_fail=partial,
        )


def execute_suggestion_in_zerodha(
    db: SQLServerConnection,
    suggestion_id: str,
    *,
    spot_at_execution: Optional[float] = None,
    leg_limits: Optional[Dict[int, float]] = None,
    ack_out_of_band: bool = False,
    execution_job_id: Optional[int] = None,
) -> ExecutionOutcome:
    if not zerodha_execution_enabled(db):
        raise ZerodhaExecutionError(
            "Zerodha execution is disabled — enable OPT_ZERODHA_EXECUTION_ENABLED "
            "and the trade_execution_enabled runtime flag"
        )

    lock = _lock_for(f"sugg:{suggestion_id}")
    if not lock.acquire(blocking=False):
        raise ZerodhaExecutionError("Execution already in progress for this suggestion")

    try:
        suggestion, legs, facade, master, live_map, inst_map, ordered, strategy = (
            _entry_context(db, suggestion_id, leg_limits)
        )
        plans = _build_leg_plans(ordered, inst_map, live_map, leg_limits, mode="entry", strategy=strategy)
        system_plans = (
            _build_leg_plans(ordered, inst_map, live_map, None, mode="entry", strategy=strategy)
            if leg_limits else None
        )
        _enforce_limit_band(
            legs, plans, ack_out_of_band=ack_out_of_band, system_plans=system_plans,
        )

        legs_by_order = {int(l["leg_order"]): l for l in legs}
        plan_by_order = {p.leg_order: p for p in plans}
        warnings: List[str] = []

        completed: List[LegFillOutcome] = []
        try:
            for leg in ordered:
                lo = int(leg["leg_order"])
                if execution_job_id is not None:
                    update_job_progress(
                        db, execution_job_id,
                        current_leg_order=lo,
                        filled_legs=len(completed),
                        message=f"Placing leg {lo}…",
                    )
                ltp = _refresh_leg_ltp(facade, inst_map, leg)
                live_map[lo] = ltp
                remaining = [l for l in legs if int(l["leg_order"]) not in {
                    f.leg_order for f in completed
                }]
                price_gate = validate_live_prices(remaining, live_map)
                if not price_gate.ok:
                    raise ZerodhaExecutionError(
                        f"Live prices out of band before leg {lo}: {price_gate.reason()}"
                    )
                plan = plan_by_order[lo]
                txn = _transaction_type_for_leg(leg, "entry")
                if txn != plan.transaction_type:
                    raise ZerodhaExecutionError(
                        f"leg {lo}: preview/execute mismatch ({plan.transaction_type} vs {txn})"
                    )
                user_lim = (leg_limits or {}).get(lo)
                qmap = fetch_quote_map(facade, [_kite_symbol_key(inst_map[lo])])
                outcome = _place_and_monitor_leg(
                    db, facade,
                    operation="ENTRY",
                    leg=leg,
                    inst=inst_map[lo],
                    transaction_type=txn,
                    ltp=ltp,
                    suggestion_id=suggestion_id,
                    trade_id=None,
                    user_limit_price=user_lim,
                    expected_transaction_type=txn,
                    execution_profile=profile_for("entry"),
                    execution_job_id=execution_job_id,
                    quote_row=qmap.get(_kite_symbol_key(inst_map[lo])),
                )
                completed.append(outcome)
        except Exception as exc:
            _handle_leg_failure(
                facade, db, exc,
                suggestion_id=suggestion_id,
                trade_id=None,
                legs_by_order=legs_by_order,
                completed=completed,
                inst_map=inst_map,
                mode="entry",
            )
            raise

        spot = spot_at_execution
        if spot is None:
            spot = _spot_ltp(facade)
        if spot is None and suggestion.get("spot_at_generation") is not None:
            spot = float(suggestion["spot_at_generation"])

        fills = [
            TradeLegFill(
                leg_order=f.leg_order,
                executed=True,
                fill_price=f.fill_price,
                fill_time=f.fill_time,
            )
            for f in completed
        ]
        try:
            trade_id = mark_executed(
                db,
                suggestion_id,
                fills,
                spot_at_execution=spot,
                actual_stop_loss_level=suggestion.get("stop_loss_level"),
                skip_execution_gate=True,
                execution_provider=EXECUTION_PROVIDER_ZERODHA,
            )
        except Exception as exc:
            _handle_leg_failure(
                facade, db, exc,
                suggestion_id=suggestion_id,
                trade_id=None,
                legs_by_order=legs_by_order,
                completed=completed,
                inst_map=inst_map,
                mode="entry",
            )
            raise
        if trade_id is None:
            _handle_leg_failure(
                facade, db,
                ZerodhaExecutionError("Trade was not created after fills"),
                suggestion_id=suggestion_id,
                trade_id=None,
                legs_by_order=legs_by_order,
                completed=completed,
                inst_map=inst_map,
                mode="entry",
            )
            raise ZerodhaExecutionError("Trade was not created after fills")

        broker_rows = BrokerOrderRepo(db).by_suggestion(suggestion_id)
        for row in broker_rows:
            if row.get("operation") == "ENTRY" and not row.get("trade_id"):
                db.execute(
                    "UPDATE options_broker_orders SET trade_id = ? WHERE id = ?",
                    [trade_id, row["id"]],
                ).close()
        db.commit()

        recon = reconcile_positions_after_fill(
            facade, ordered, inst_map,
            transaction_fn=lambda leg: _transaction_type_for_leg(leg, "entry"),
            mode="entry",
        )
        if not recon.ok and recon.message:
            warnings.append(recon.message)

        return ExecutionOutcome(
            ok=True,
            trade_id=trade_id,
            message="All legs filled in Zerodha; trade recorded",
            leg_fills=completed,
            broker_orders=broker_rows,
            warnings=warnings,
            job_id=execution_job_id,
        )
    finally:
        lock.release()


def execute_suggestion_in_zerodha_async(
    db: SQLServerConnection,
    suggestion_id: str,
    *,
    spot_at_execution: Optional[float] = None,
    leg_limits: Optional[Dict[int, float]] = None,
    ack_out_of_band: bool = False,
) -> ExecutionOutcome:
    """Start background entry execution; returns immediately with job_id."""
    if not zerodha_execution_enabled(db):
        raise ZerodhaExecutionError("Zerodha execution is disabled")
    _assert_execution_not_in_flight(db, suggestion_id=suggestion_id)

    suggestion = SuggestionRepo(db).get(suggestion_id)
    if suggestion is None:
        raise ZerodhaExecutionError(f"Unknown suggestion: {suggestion_id}")
    legs = SuggestionRepo(db).legs(suggestion_id)
    total = len(legs)

    def _runner(wdb: SQLServerConnection, job_id: int) -> ExecutionOutcome:
        return execute_suggestion_in_zerodha(
            wdb, suggestion_id,
            spot_at_execution=spot_at_execution,
            leg_limits=leg_limits,
            ack_out_of_band=ack_out_of_band,
            execution_job_id=job_id,
        )

    job_id = submit_execution_job(
        db,
        operation="ENTRY",
        suggestion_id=suggestion_id,
        trade_id=None,
        total_legs=total,
        runner=_runner,
    )
    return ExecutionOutcome(
        ok=True,
        message="Zerodha entry execution started",
        job_id=job_id,
        async_started=True,
    )


def preview_close_execution(
    db: SQLServerConnection,
    trade_id: str,
    *,
    leg_limits: Optional[Dict[int, float]] = None,
) -> ExecutionPreview:
    if not zerodha_execution_enabled(db):
        raise ZerodhaExecutionError("Zerodha execution is disabled")
    trd = TradeRepo(db)
    trade = trd.get(trade_id)
    if trade is None:
        raise ZerodhaExecutionError(f"Unknown trade: {trade_id}")
    all_legs = trd.legs_with_suggestion_info(trade_id)
    open_exits = [l for l in all_legs if l.get("executed") and l.get("exit_price") is None]
    if not open_exits:
        raise ZerodhaExecutionError("No open legs to close")
    facade, master = _build_client()
    live_map, inst_map = _live_ltp_map(facade, master, open_exits)
    strategy = ""
    if trade.get("suggestion_id"):
        sug_row = SuggestionRepo(db).get(trade["suggestion_id"])
        strategy = str((sug_row or {}).get("strategy") or "")
    ordered = legs_in_execution_order(open_exits, strategy, mode="close")
    plans = _build_leg_plans(ordered, inst_map, live_map, leg_limits, mode="close", strategy=strategy)
    limit_map = {p.leg_order: p.limit_price for p in plans}
    limit_gate = validate_limit_prices(open_exits, limit_map)
    return ExecutionPreview(
        operation="EXIT",
        suggestion_id=trade.get("suggestion_id"),
        trade_id=trade_id,
        trade_name=trade.get("trade_name"),
        strategy=strategy or None,
        legs=plans,
        all_limits_in_band=limit_gate.ok,
        limit_vetoes=limit_gate.vetoes,
        spot_at_execution=None,
    )


def close_trade_in_zerodha(
    db: SQLServerConnection,
    trade_id: str,
    *,
    leg_limits: Optional[Dict[int, float]] = None,
    ack_out_of_band: bool = False,
) -> ExecutionOutcome:
    if not zerodha_execution_enabled(db):
        raise ZerodhaExecutionError(
            "Zerodha execution is disabled — enable OPT_ZERODHA_EXECUTION_ENABLED "
            "and the trade_execution_enabled runtime flag"
        )

    lock = _lock_for(f"trade:{trade_id}")
    if not lock.acquire(blocking=False):
        raise ZerodhaExecutionError("Close already in progress for this trade")

    try:
        trd = TradeRepo(db)
        trade = trd.get(trade_id)
        if trade is None:
            raise ZerodhaExecutionError(f"Unknown trade: {trade_id}")
        if str(trade.get("status") or "").upper() in ("CLOSED", "VOID", "EXPIRED"):
            raise ZerodhaExecutionError(
                f"Trade status is {trade.get('status')!r} — cannot close"
            )

        all_legs = trd.legs_with_suggestion_info(trade_id)
        executed_legs = [l for l in all_legs if l.get("executed")]
        if not executed_legs:
            raise ZerodhaExecutionError("Trade has no executed legs to close")

        open_exits = [l for l in executed_legs if l.get("exit_price") is None]
        if not open_exits:
            raise ZerodhaExecutionError("All legs already have exit fills recorded")

        pending = BrokerOrderRepo(db).pending_for_trade(trade_id, operation="EXIT")
        if pending:
            raise ZerodhaExecutionError(
                "Broker close orders already in flight for this trade — wait or cancel on Kite"
            )
        _assert_execution_not_in_flight(db, trade_id=trade_id)

        facade, master = _build_client()
        live_map, inst_map = _live_ltp_map(facade, master, open_exits)

        strategy = ""
        if trade.get("suggestion_id"):
            sug_row = SuggestionRepo(db).get(trade["suggestion_id"])
            strategy = str((sug_row or {}).get("strategy") or "")

        ordered = legs_in_execution_order(open_exits, strategy, mode="close")
        _run_pre_trade_checks(
            facade, open_exits, inst_map, ordered, leg_limits, mode="close",
            live_map=live_map,
        )
        plans = _build_leg_plans(
            ordered, inst_map, live_map, leg_limits, mode="close", strategy=strategy,
        )
        system_plans = (
            _build_leg_plans(ordered, inst_map, live_map, None, mode="close", strategy=strategy)
            if leg_limits else None
        )
        _enforce_limit_band(
            open_exits, plans, ack_out_of_band=ack_out_of_band, system_plans=system_plans,
        )
        completed: List[LegFillOutcome] = []
        legs_by_order = {int(l["leg_order"]): l for l in open_exits}
        plan_by_order = {p.leg_order: p for p in plans}
        warnings: List[str] = []
        try:
            for leg in ordered:
                lo = int(leg["leg_order"])
                ltp = _refresh_leg_ltp(facade, inst_map, leg)
                live_map[lo] = ltp
                plan = plan_by_order[lo]
                close_txn = _transaction_type_for_leg(leg, "close")
                if close_txn != plan.transaction_type:
                    raise ZerodhaExecutionError(
                        f"leg {lo}: close preview/execute mismatch "
                        f"({plan.transaction_type} vs {close_txn})"
                    )
                user_lim = (leg_limits or {}).get(lo)
                qmap = fetch_quote_map(facade, [_kite_symbol_key(inst_map[lo])])
                outcome = _place_and_monitor_leg(
                    db, facade,
                    operation="EXIT",
                    leg=leg,
                    inst=inst_map[lo],
                    transaction_type=close_txn,
                    ltp=ltp,
                    suggestion_id=trade.get("suggestion_id"),
                    trade_id=trade_id,
                    user_limit_price=user_lim,
                    expected_transaction_type=close_txn,
                    execution_profile=profile_for("close"),
                    quote_row=qmap.get(_kite_symbol_key(inst_map[lo])),
                )
                completed.append(outcome)
        except Exception as exc:
            _handle_leg_failure(
                facade, db, exc,
                suggestion_id=trade.get("suggestion_id"),
                trade_id=trade_id,
                legs_by_order=legs_by_order,
                completed=completed,
                inst_map=inst_map,
                mode="exit",
            )
            raise

        exits = [
            {
                "leg_order": f.leg_order,
                "exit_price": f.fill_price,
                "exit_time": f.fill_time,
            }
            for f in completed
        ]
        close_trade_with_fills(db, trade_id, exits)

        recon = reconcile_positions_after_fill(
            facade, ordered, inst_map,
            transaction_fn=lambda leg: _transaction_type_for_leg(leg, "close"),
            mode="close",
        )
        if not recon.ok and recon.message:
            warnings.append(recon.message)

        broker_rows = BrokerOrderRepo(db).by_trade(trade_id, operation="EXIT")
        return ExecutionOutcome(
            ok=True,
            trade_id=trade_id,
            message="All close legs filled in Zerodha; trade closed",
            leg_fills=completed,
            broker_orders=broker_rows,
            warnings=warnings,
        )
    finally:
        lock.release()


def execute_supplement_in_zerodha(
    db: SQLServerConnection,
    trade_id: str,
    *,
    leg_limits: Optional[Dict[int, float]] = None,
    ack_out_of_band: bool = False,
    execution_job_id: Optional[int] = None,
) -> ExecutionOutcome:
    """Fill remaining unexecuted legs on a partial/broken trade via Zerodha."""
    if not zerodha_execution_enabled(db):
        raise ZerodhaExecutionError("Zerodha execution is disabled")

    lock = _lock_for(f"trade:{trade_id}")
    if not lock.acquire(blocking=False):
        raise ZerodhaExecutionError("Supplement already in progress for this trade")

    try:
        trd = TradeRepo(db)
        trade = trd.get(trade_id)
        if trade is None:
            raise ZerodhaExecutionError(f"Unknown trade: {trade_id}")
        all_legs = trd.legs_with_suggestion_info(trade_id)
        pending_legs = [l for l in all_legs if not l.get("executed")]
        if not pending_legs:
            raise ZerodhaExecutionError("All legs already executed — nothing to supplement")

        pending_orders = BrokerOrderRepo(db).pending_for_trade(trade_id, operation="SUPPLEMENT")
        if pending_orders:
            raise ZerodhaExecutionError(
                "Broker supplement orders already in flight — wait or cancel on Kite"
            )
        _assert_execution_not_in_flight(db, trade_id=trade_id)

        facade, master = _build_client()
        live_map, inst_map = _live_ltp_map(facade, master, pending_legs)
        strategy = ""
        if trade.get("suggestion_id"):
            sug_row = SuggestionRepo(db).get(trade["suggestion_id"])
            strategy = str((sug_row or {}).get("strategy") or "")

        ordered = legs_in_execution_order(pending_legs, strategy, mode="entry")
        _run_pre_trade_checks(
            facade, pending_legs, inst_map, ordered, leg_limits, mode="entry",
            allow_existing_positions=True,
            live_map=live_map,
        )
        plans = _build_leg_plans(
            ordered, inst_map, live_map, leg_limits, mode="entry", strategy=strategy,
        )
        system_plans = (
            _build_leg_plans(ordered, inst_map, live_map, None, mode="entry", strategy=strategy)
            if leg_limits else None
        )
        _enforce_limit_band(
            pending_legs, plans, ack_out_of_band=ack_out_of_band, system_plans=system_plans,
        )

        legs_by_order = {int(l["leg_order"]): l for l in pending_legs}
        plan_by_order = {p.leg_order: p for p in plans}
        completed: List[LegFillOutcome] = []
        warnings: List[str] = []

        try:
            for leg in ordered:
                lo = int(leg["leg_order"])
                if execution_job_id is not None:
                    update_job_progress(
                        db, execution_job_id,
                        current_leg_order=lo,
                        filled_legs=len(completed),
                        message=f"Supplement leg {lo}…",
                    )
                ltp = _refresh_leg_ltp(facade, inst_map, leg)
                live_map[lo] = ltp
                remaining = [l for l in pending_legs if int(l["leg_order"]) not in {
                    f.leg_order for f in completed
                }]
                price_gate = validate_live_prices(remaining, live_map)
                if not price_gate.ok:
                    raise ZerodhaExecutionError(
                        f"Live prices out of band before supplement leg {lo}: "
                        f"{price_gate.reason()}"
                    )
                txn = _transaction_type_for_leg(leg, "entry")
                user_lim = (leg_limits or {}).get(lo)
                qmap = fetch_quote_map(facade, [_kite_symbol_key(inst_map[lo])])
                outcome = _place_and_monitor_leg(
                    db, facade,
                    operation="SUPPLEMENT",
                    leg=leg,
                    inst=inst_map[lo],
                    transaction_type=txn,
                    ltp=ltp,
                    suggestion_id=trade.get("suggestion_id"),
                    trade_id=trade_id,
                    user_limit_price=user_lim,
                    expected_transaction_type=txn,
                    execution_profile=profile_for("entry"),
                    execution_job_id=execution_job_id,
                    quote_row=qmap.get(_kite_symbol_key(inst_map[lo])),
                )
                completed.append(outcome)
        except Exception as exc:
            _handle_leg_failure(
                facade, db, exc,
                suggestion_id=trade.get("suggestion_id"),
                trade_id=trade_id,
                legs_by_order=legs_by_order,
                completed=completed,
                inst_map=inst_map,
                mode="entry",
            )
            raise

        fills = [
            TradeLegFill(
                leg_order=f.leg_order,
                executed=True,
                fill_price=f.fill_price,
                fill_time=f.fill_time,
            )
            for f in completed
        ]
        supplement_trade(db, trade_id, fills)

        recon = reconcile_positions_after_fill(
            facade, ordered, inst_map,
            transaction_fn=lambda leg: _transaction_type_for_leg(leg, "entry"),
            mode="entry",
        )
        if not recon.ok and recon.message:
            warnings.append(recon.message)

        broker_rows = BrokerOrderRepo(db).by_trade(trade_id)
        return ExecutionOutcome(
            ok=True,
            trade_id=trade_id,
            message="Supplement legs filled in Zerodha",
            leg_fills=completed,
            broker_orders=broker_rows,
            warnings=warnings,
            job_id=execution_job_id,
        )
    finally:
        lock.release()


def close_trade_in_zerodha_async(
    db: SQLServerConnection,
    trade_id: str,
    *,
    leg_limits: Optional[Dict[int, float]] = None,
    ack_out_of_band: bool = False,
) -> ExecutionOutcome:
    if not zerodha_execution_enabled(db):
        raise ZerodhaExecutionError("Zerodha execution is disabled")
    _assert_execution_not_in_flight(db, trade_id=trade_id)

    trd = TradeRepo(db)
    all_legs = trd.legs_with_suggestion_info(trade_id)
    open_exits = [l for l in all_legs if l.get("executed") and l.get("exit_price") is None]

    def _runner(wdb: SQLServerConnection, job_id: int) -> ExecutionOutcome:
        return close_trade_in_zerodha(
            wdb, trade_id,
            leg_limits=leg_limits,
            ack_out_of_band=ack_out_of_band,
        )

    job_id = submit_execution_job(
        db,
        operation="EXIT",
        suggestion_id=None,
        trade_id=trade_id,
        total_legs=len(open_exits),
        runner=_runner,
    )
    return ExecutionOutcome(
        ok=True,
        message="Zerodha close execution started",
        job_id=job_id,
        async_started=True,
    )
