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
from lifecycle.trade_executor import close_trade_with_fills, mark_executed
from providers.zerodha.execution_facade import KiteExecutionFacade
from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import Instrument, InstrumentMaster
from providers.zerodha.session import is_token_valid, load_session
from utils import now_ist


logger = logging.getLogger(__name__)

_TERMINAL_FAIL = frozenset({"REJECTED", "CANCELLED"})
_IN_FLIGHT = frozenset({"PENDING", "OPEN", "TRIGGER PENDING"})
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class ZerodhaExecutionError(Exception):
    """User-visible execution failure."""


@dataclass
class LegFillOutcome:
    leg_order: int
    fill_price: float
    fill_time: datetime
    kite_order_id: str
    broker_row_id: int


@dataclass
class ExecutionOutcome:
    ok: bool
    trade_id: Optional[str] = None
    message: str = ""
    leg_fills: List[LegFillOutcome] = field(default_factory=list)
    broker_orders: List[dict] = field(default_factory=list)


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
    return inst


def _kite_symbol_key(inst: Instrument) -> str:
    return f"{inst.exchange}:{inst.tradingsymbol}"


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    steps = round(price / tick)
    return round(steps * tick, 2)


def _limit_price(ltp: float, transaction_type: str) -> float:
    slip = float(ZERODHA_EXECUTION_CONFIG.get("limit_slippage_pct", 0.5)) / 100.0
    txn = transaction_type.upper()
    if txn == "BUY":
        return ltp * (1.0 + slip)
    return ltp * (1.0 - slip)


def _resolve_limit_price(
    ltp: float,
    transaction_type: str,
    inst: Instrument,
    user_limit: Optional[float] = None,
) -> float:
    """Use ``user_limit`` when provided; otherwise LTP ± configured slippage."""
    if user_limit is not None:
        px = float(user_limit)
        if px <= 0:
            raise ZerodhaExecutionError(
                f"leg limit price must be positive (got {user_limit})"
            )
        return _round_to_tick(px, inst.tick_size)
    return _round_to_tick(_limit_price(ltp, transaction_type), inst.tick_size)


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


def _wait_for_order_complete(
    facade: KiteExecutionFacade,
    order_id: str,
) -> Tuple[str, float]:
    poll = float(ZERODHA_EXECUTION_CONFIG.get("order_poll_interval_sec", 2))
    max_wait = float(ZERODHA_EXECUTION_CONFIG.get("order_max_wait_sec", 45))
    deadline = time.monotonic() + max_wait
    last_status = "UNKNOWN"
    while time.monotonic() < deadline:
        history = facade.order_history(order_id)
        row = _latest_order_row(history)
        last_status = str(row.get("status") or "UNKNOWN").upper()
        if last_status == "COMPLETE":
            avg = row.get("average_price") or row.get("price")
            if avg is None:
                raise ZerodhaExecutionError(
                    f"Order {order_id} complete but average_price missing"
                )
            return last_status, float(avg)
        if last_status in _TERMINAL_FAIL:
            msg = row.get("status_message") or last_status
            raise ZerodhaExecutionError(f"Order {order_id} {last_status}: {msg}")
        time.sleep(poll)
    raise ZerodhaExecutionError(
        f"Order {order_id} timed out after {max_wait:.0f}s (last status {last_status})"
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
) -> LegFillOutcome:
    cfg = ZERODHA_EXECUTION_CONFIG
    lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
    qty = lots * int(inst.lot_size or leg.get("lot_size") or 0)
    if qty <= 0:
        raise ZerodhaExecutionError(f"leg {leg['leg_order']}: invalid quantity")

    limit_px = _resolve_limit_price(ltp, transaction_type, inst, user_limit_price)
    fixed_user_limit = user_limit_price is not None
    now = now_ist()
    broker = BrokerOrderRepo(db)
    row_id = broker.insert({
        "operation": operation,
        "suggestion_id": suggestion_id,
        "trade_id": trade_id,
        "leg_order": int(leg["leg_order"]),
        "tradingsymbol": inst.tradingsymbol,
        "exchange": inst.exchange,
        "transaction_type": transaction_type,
        "quantity": qty,
        "limit_price": limit_px,
        "status": "PENDING",
        "tag": None,
        "created_at": now,
        "updated_at": now,
    })
    db.commit()

    tag = f"oa{row_id}"[:20]
    broker.update_status(row_id, status="OPEN", error_message=None, updated_at=now_ist())
    db.commit()

    max_retries = int(cfg.get("order_max_retries", 3))
    last_exc: Optional[Exception] = None
    order_id: Optional[str] = None

    for attempt in range(max_retries + 1):
        try:
            if order_id is None:
                order_id = facade.place_order(
                    variety=str(cfg.get("variety", "regular")),
                    exchange=inst.exchange,
                    tradingsymbol=inst.tradingsymbol,
                    transaction_type=transaction_type,
                    quantity=qty,
                    product=str(cfg.get("product", "NRML")),
                    order_type="LIMIT",
                    price=limit_px,
                    tag=tag,
                )
                broker.update_status(
                    row_id,
                    status="OPEN",
                    kite_order_id=order_id,
                    retry_count=attempt,
                    updated_at=now_ist(),
                )
                db.commit()
            else:
                facade.modify_order(
                    order_id=order_id,
                    variety=str(cfg.get("variety", "regular")),
                    price=limit_px,
                )
                broker.update_status(
                    row_id,
                    status="OPEN",
                    retry_count=attempt,
                    updated_at=now_ist(),
                )
                db.commit()

            status, fill_px = _wait_for_order_complete(facade, order_id)
            filled_at = now_ist()
            broker.update_status(
                row_id,
                status=status,
                fill_price=fill_px,
                updated_at=filled_at,
            )
            db.commit()
            return LegFillOutcome(
                leg_order=int(leg["leg_order"]),
                fill_price=fill_px,
                fill_time=filled_at,
                kite_order_id=order_id,
                broker_row_id=row_id,
            )
        except ZerodhaExecutionError as exc:
            last_exc = exc
            logger.warning(
                "zerodha leg %s attempt %d/%d failed: %s",
                leg.get("leg_order"), attempt + 1, max_retries + 1, exc,
            )
            if order_id and attempt < max_retries:
                if not fixed_user_limit:
                    try:
                        keys = [_kite_symbol_key(inst)]
                        fresh = _fetch_ltps(facade, keys).get(keys[0])
                        if fresh is not None:
                            limit_px = _resolve_limit_price(
                                fresh, transaction_type, inst, None,
                            )
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
                    facade.cancel_order(
                        order_id=order_id,
                        variety=str(cfg.get("variety", "regular")),
                    )
                except Exception:
                    logger.debug("cancel after failure failed", exc_info=True)
            raise
        except Exception as exc:
            last_exc = exc
            broker.update_status(
                row_id,
                status="FAILED",
                error_message=str(exc)[:500],
                updated_at=now_ist(),
            )
            db.commit()
            raise ZerodhaExecutionError(str(exc)) from exc

    raise ZerodhaExecutionError(str(last_exc or "order placement failed"))


def _rollback_entry_legs(
    facade: KiteExecutionFacade,
    db: SQLServerConnection,
    *,
    suggestion_id: str,
    legs_by_order: Dict[int, dict],
    completed: List[LegFillOutcome],
) -> None:
    """Best-effort reverse orders for legs already filled before a failure."""
    if not completed:
        return
    logger.warning(
        "Rolling back %d filled leg(s) for suggestion %s after failure",
        len(completed), suggestion_id,
    )
    master = InstrumentMaster(loader=lambda: facade.instruments("NFO"))
    master.refresh_if_stale()
    for fill in reversed(completed):
        leg = legs_by_order[fill.leg_order]
        inst = _resolve_instrument(leg, master)
        orig_txn = "BUY" if str(leg.get("action", "")).upper() == "BUY" else "SELL"
        reverse_txn = "SELL" if orig_txn == "BUY" else "BUY"
        key = _kite_symbol_key(inst)
        ltp = _fetch_ltps(facade, [key]).get(key)
        if ltp is None:
            logger.error("rollback: no LTP for leg %s", fill.leg_order)
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
                trade_id=None,
            )
        except Exception:
            logger.exception(
                "rollback failed for leg %s — manual intervention required",
                fill.leg_order,
            )


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
            orig = str(leg.get("action") or "").upper()
            txn = "BUY" if orig == "SELL" else "SELL"
        else:
            txn = str(leg.get("action") or "").upper()
        user_lim = (leg_limits or {}).get(lo)
        auto = user_lim is None
        limit_px = _resolve_limit_price(ltp, txn, inst, user_lim)
        lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
        qty = lots * int(inst.lot_size or leg.get("lot_size") or 0)
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
    pending = BrokerOrderRepo(db).pending_for_suggestion(suggestion_id)
    if pending:
        raise ZerodhaExecutionError(
            "Broker orders already in flight for this suggestion — wait or cancel on Kite"
        )
    gate = validate_execution(suggestion, legs)
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
) -> None:
    limit_map = {p.leg_order: p.limit_price for p in plans}
    limit_gate = validate_limit_prices(legs, limit_map)
    if not limit_gate.ok and not ack_out_of_band:
        raise ZerodhaExecutionError(
            "Limit prices outside suggestion band: "
            + limit_gate.reason()
            + " — review the confirmation preview or set ack_out_of_band to proceed"
        )
def execute_suggestion_in_zerodha(
    db: SQLServerConnection,
    suggestion_id: str,
    *,
    spot_at_execution: Optional[float] = None,
    leg_limits: Optional[Dict[int, float]] = None,
    ack_out_of_band: bool = False,
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
        _enforce_limit_band(legs, plans, ack_out_of_band=ack_out_of_band)

        legs_by_order = {int(l["leg_order"]): l for l in legs}
        plan_by_order = {p.leg_order: p for p in plans}

        completed: List[LegFillOutcome] = []
        try:
            for leg in ordered:
                lo = int(leg["leg_order"])
                plan = plan_by_order[lo]
                ltp = live_map.get(lo)
                if ltp is None:
                    raise ZerodhaExecutionError(f"leg {lo}: live price unavailable")
                txn = plan.transaction_type
                user_lim = (leg_limits or {}).get(lo)
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
                )
                completed.append(outcome)
        except Exception:
            _rollback_entry_legs(
                facade, db,
                suggestion_id=suggestion_id,
                legs_by_order=legs_by_order,
                completed=completed,
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
        trade_id = mark_executed(
            db,
            suggestion_id,
            fills,
            spot_at_execution=spot,
            actual_stop_loss_level=suggestion.get("stop_loss_level"),
            skip_execution_gate=True,
        )
        if trade_id is None:
            raise ZerodhaExecutionError("Trade was not created after fills")

        broker_rows = BrokerOrderRepo(db).by_suggestion(suggestion_id)
        for row in broker_rows:
            if row.get("operation") == "ENTRY" and row.get("kite_order_id"):
                BrokerOrderRepo(db).update_status(
                    int(row["id"]),
                    status=row.get("status") or "COMPLETE",
                    updated_at=now_ist(),
                )
        for row in broker_rows:
            if row.get("operation") == "ENTRY" and not row.get("trade_id"):
                db.execute(
                    "UPDATE options_broker_orders SET trade_id = ? WHERE id = ?",
                    [trade_id, row["id"]],
                ).close()
        db.commit()

        return ExecutionOutcome(
            ok=True,
            trade_id=trade_id,
            message="All legs filled in Zerodha; trade recorded",
            leg_fills=completed,
            broker_orders=broker_rows,
        )
    finally:
        lock.release()


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
    return ExecutionPreview(
        operation="EXIT",
        suggestion_id=trade.get("suggestion_id"),
        trade_id=trade_id,
        trade_name=trade.get("trade_name"),
        strategy=strategy or None,
        legs=plans,
        all_limits_in_band=True,
        limit_vetoes=[],
        spot_at_execution=None,
    )


def close_trade_in_zerodha(
    db: SQLServerConnection,
    trade_id: str,
    *,
    leg_limits: Optional[Dict[int, float]] = None,
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

        facade, master = _build_client()
        live_map, inst_map = _live_ltp_map(facade, master, open_exits)

        strategy = ""
        if trade.get("suggestion_id"):
            sug_row = SuggestionRepo(db).get(trade["suggestion_id"])
            strategy = str((sug_row or {}).get("strategy") or "")

        ordered = legs_in_execution_order(open_exits, strategy, mode="close")
        completed: List[LegFillOutcome] = []

        for leg in ordered:
            lo = int(leg["leg_order"])
            ltp = live_map.get(lo)
            if ltp is None:
                raise ZerodhaExecutionError(f"leg {lo}: live price unavailable for close")
            orig = str(leg.get("action") or "").upper()
            close_txn = "BUY" if orig == "SELL" else "SELL"
            user_lim = (leg_limits or {}).get(lo)
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
            )
            completed.append(outcome)

        exits = [
            {
                "leg_order": f.leg_order,
                "exit_price": f.fill_price,
                "exit_time": f.fill_time,
            }
            for f in completed
        ]
        close_trade_with_fills(db, trade_id, exits)

        broker_rows = BrokerOrderRepo(db).by_trade(trade_id, operation="EXIT")
        return ExecutionOutcome(
            ok=True,
            trade_id=trade_id,
            message="All close legs filled in Zerodha; trade closed",
            leg_fills=completed,
            broker_orders=broker_rows,
        )
    finally:
        lock.release()
