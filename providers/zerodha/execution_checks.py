"""
Pre-trade margin / exposure checks and post-fill position reconciliation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import ZERODHA_EXECUTION_CONFIG
from providers.zerodha.instruments import Instrument


logger = logging.getLogger(__name__)


@dataclass
class MarginCheckResult:
    ok: bool
    required: float = 0.0  # peak (gate); same as peak_required
    available: float = 0.0
    message: str = ""
    final_required: float = 0.0
    peak_required: float = 0.0
    buffer_pct: float = 0.0
    shortfall: float = 0.0  # max(0, need − available) including buffer
    path_steps: List[dict] = field(default_factory=list)


@dataclass
class _BasketQuote:
    """Place-time margin (initial) and margin left after hedge benefit (final)."""

    initial: float
    final: float
    option_premium: Optional[float] = None


@dataclass
class ExposureCheckResult:
    ok: bool
    conflicts: List[str] = field(default_factory=list)
    message: str = ""


@dataclass
class PositionReconcileResult:
    ok: bool
    mismatches: List[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ReducingPositionCheckResult:
    """Result of verifying broker inventory before EXIT / flatten / rollback."""

    ok: bool
    missing: List[str] = field(default_factory=list)
    message: str = ""


def _broker_net_by_symbol(facade: Any) -> Dict[str, int]:
    """Net qty by tradingsymbol from Kite positions (fail loudly to caller)."""
    pos = facade.positions()
    net_rows = pos.get("net") or pos.get("day") or []
    by_sym: Dict[str, int] = {}
    for row in net_rows:
        sym = str(row.get("tradingsymbol") or "")
        qty = int(row.get("quantity") or 0)
        if sym:
            by_sym[sym] = by_sym.get(sym, 0) + qty
    return by_sym


def _planned_qty(
    leg: dict,
    inst: Instrument,
    *,
    quantity_override: Optional[int] = None,
) -> int:
    if quantity_override is not None:
        return int(quantity_override)
    lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
    return lots * int(inst.lot_size or leg.get("lot_size") or 0)


def is_structure_flat_on_broker(
    facade: Any,
    legs: List[dict],
    inst_map: Dict[int, Instrument],
) -> bool:
    """True when every leg's tradingsymbol has broker net qty 0."""
    if not legs:
        return True
    by_sym = _broker_net_by_symbol(facade)
    for leg in legs:
        lo = int(leg["leg_order"])
        sym = inst_map[lo].tradingsymbol
        if int(by_sym.get(sym, 0)) != 0:
            return False
    return True


def find_external_exit_fills(
    facade: Any,
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    *,
    transaction_fn,
    after_ts: Optional[Any] = None,
    quantity_overrides: Optional[Dict[int, int]] = None,
) -> Tuple[Dict[int, dict], List[str]]:
    """Match COMPLETE Kite day-orders to close each leg (manual flatten case).

    Returns ``({leg_order: {fill_price, fill_time, kite_order_id, quantity}}, missing)``.
    """
    try:
        orders = list(facade.orders() or [])
    except Exception as exc:
        return {}, [f"Could not fetch Zerodha orders ({exc})"]

    complete = []
    for row in orders:
        status = str(row.get("status") or "").upper()
        if status != "COMPLETE":
            continue
        avg = row.get("average_price")
        try:
            avg_f = float(avg) if avg is not None else 0.0
        except (TypeError, ValueError):
            avg_f = 0.0
        if avg_f <= 0:
            continue
        complete.append(row)

    used_oids: set = set()
    found: Dict[int, dict] = {}
    missing: List[str] = []
    overrides = quantity_overrides or {}

    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_map[lo]
        sym = inst.tradingsymbol
        qty = _planned_qty(leg, inst, quantity_override=overrides.get(lo))
        txn = str(transaction_fn(leg) or "").upper()
        candidates = []
        for row in complete:
            oid = str(row.get("order_id") or "")
            if oid and oid in used_oids:
                continue
            if str(row.get("tradingsymbol") or "") != sym:
                continue
            if str(row.get("transaction_type") or "").upper() != txn:
                continue
            filled_qty = int(
                row.get("filled_quantity")
                or row.get("quantity")
                or 0
            )
            if qty > 0 and filled_qty > 0 and filled_qty != qty:
                # Prefer exact qty; keep as weaker candidate if nothing exact.
                pass
            ts_raw = (
                row.get("order_timestamp")
                or row.get("exchange_timestamp")
                or row.get("order_time")
            )
            if after_ts is not None and ts_raw is not None:
                try:
                    from datetime import datetime as _dt
                    if hasattr(ts_raw, "timestamp"):
                        ts = ts_raw
                    else:
                        ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    after = after_ts
                    if hasattr(after, "tzinfo") and after.tzinfo is None and hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                        ts = ts.replace(tzinfo=None)
                    if ts < after:
                        continue
                except Exception:
                    pass
            exact = filled_qty == qty
            candidates.append((0 if exact else 1, ts_raw or "", row))

        if not candidates:
            missing.append(
                f"leg {lo}: no COMPLETE {txn} fill for {sym} on Kite today"
            )
            continue
        candidates.sort(key=lambda x: (x[0], str(x[1])))
        # Prefer exact qty, then latest timestamp among those.
        exact = [c for c in candidates if c[0] == 0]
        pool = exact or candidates
        pool.sort(key=lambda x: str(x[1]), reverse=True)
        _rank, _ts, row = pool[0]
        oid = str(row.get("order_id") or "")
        if oid:
            used_oids.add(oid)
        avg_f = float(row.get("average_price"))
        found[lo] = {
            "fill_price": avg_f,
            "fill_time": row.get("order_timestamp") or row.get("exchange_timestamp"),
            "kite_order_id": oid,
            "quantity": int(row.get("filled_quantity") or row.get("quantity") or 0),
            "tradingsymbol": sym,
            "transaction_type": txn,
        }

    return found, missing


def assert_reducing_positions_exist(
    facade: Any,
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    *,
    transaction_fn,
    quantity_overrides: Optional[Dict[int, int]] = None,
) -> ReducingPositionCheckResult:
    """
    Fail closed before EXIT / close / entry-rollback orders.

    An EXIT (or flatten rollback) with no opposing broker position *opens*
    new exposure instead of closing. Require enough net inventory:

    - BUY exit/flatten needs short: net_qty <= -planned_qty
    - SELL exit/flatten needs long: net_qty >= planned_qty
    """
    if not ZERODHA_EXECUTION_CONFIG.get("exit_position_check_enabled", True):
        # Config flag retained for ops visibility, but EXIT/rollback inventory
        # checks are never skipped — disabling caused EXIT-without-position losses.
        logger.warning(
            "exit_position_check_enabled=False ignored; inventory gate always on"
        )
    if not legs:
        return ReducingPositionCheckResult(ok=True)
    try:
        by_sym = _broker_net_by_symbol(facade)
    except Exception as exc:
        return ReducingPositionCheckResult(
            ok=False,
            message=(
                "Could not fetch Zerodha positions to verify exit inventory "
                f"({exc})"
            ),
        )

    missing: List[str] = []
    overrides = quantity_overrides or {}
    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_map[lo]
        sym = inst.tradingsymbol
        qty = _planned_qty(leg, inst, quantity_override=overrides.get(lo))
        if qty <= 0:
            missing.append(f"leg {lo}: planned qty must be > 0 (got {qty})")
            continue
        txn = str(transaction_fn(leg) or "").upper()
        have = int(by_sym.get(sym, 0))
        if txn == "BUY":
            if have > -qty:
                missing.append(
                    f"leg {lo}: BUY exit needs short ≥{qty} of {sym} "
                    f"(broker net={have})"
                )
        elif txn == "SELL":
            if have < qty:
                missing.append(
                    f"leg {lo}: SELL exit needs long ≥{qty} of {sym} "
                    f"(broker net={have})"
                )
        else:
            missing.append(f"leg {lo}: unknown transaction_type={txn!r}")

    if missing:
        return ReducingPositionCheckResult(
            ok=False,
            missing=missing,
            message=(
                "Zerodha has no matching open position to close/revert — "
                "refusing EXIT that would open new exposure. "
                + "; ".join(missing)
            ),
        )
    return ReducingPositionCheckResult(ok=True)


def assert_exit_fill_reflected_for_reopen(
    facade: Any,
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    *,
    close_transaction_fn,
    quantity_overrides: Optional[Dict[int, int]] = None,
) -> ReducingPositionCheckResult:
    """
    Before re-opening after a failed EXIT leg (close rollback).

    Refuse the reopen order if broker inventory still looks like the EXIT
    never took effect — otherwise a "rollback" would add exposure on top
    of the still-open position.
    """
    if not ZERODHA_EXECUTION_CONFIG.get("exit_position_check_enabled", True):
        # Config flag retained for ops visibility, but EXIT/rollback inventory
        # checks are never skipped — disabling caused EXIT-without-position losses.
        logger.warning(
            "exit_position_check_enabled=False ignored; inventory gate always on"
        )
    if not legs:
        return ReducingPositionCheckResult(ok=True)
    try:
        by_sym = _broker_net_by_symbol(facade)
    except Exception as exc:
        return ReducingPositionCheckResult(
            ok=False,
            message=(
                "Could not fetch Zerodha positions to verify exit rollback "
                f"({exc})"
            ),
        )

    missing: List[str] = []
    overrides = quantity_overrides or {}
    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_map[lo]
        sym = inst.tradingsymbol
        qty = _planned_qty(leg, inst, quantity_override=overrides.get(lo))
        if qty <= 0:
            missing.append(f"leg {lo}: planned qty must be > 0 (got {qty})")
            continue
        close_txn = str(close_transaction_fn(leg) or "").upper()
        have = int(by_sym.get(sym, 0))
        # EXIT SELL flattened a long — after a real fill, net should be < qty.
        if close_txn == "SELL" and have >= qty:
            missing.append(
                f"leg {lo}: EXIT SELL not reflected for {sym} "
                f"(still long net={have}, refuse reopen BUY)"
            )
        elif close_txn == "BUY" and have <= -qty:
            missing.append(
                f"leg {lo}: EXIT BUY not reflected for {sym} "
                f"(still short net={have}, refuse reopen SELL)"
            )
        elif close_txn not in ("BUY", "SELL"):
            missing.append(f"leg {lo}: unknown close transaction_type={close_txn!r}")

    if missing:
        return ReducingPositionCheckResult(
            ok=False,
            missing=missing,
            message=(
                "Zerodha still shows the pre-exit position — refusing rollback "
                "reopen that would add exposure. " + "; ".join(missing)
            ),
        )
    return ReducingPositionCheckResult(ok=True)


def _available_cash(margins: dict) -> Optional[float]:
    """Cash that can pay option premium — live_balance, then cash."""
    eq = margins.get("equity") or {}
    avail = eq.get("available") or {}
    for key in ("live_balance", "cash"):
        val = avail.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _available_order_margin(margins: dict) -> Optional[float]:
    """Funds Zerodha's order check calls available.

    The reject text "Margin available" matches equity ``net`` (cash plus
    collateral). Cash alone is too small for a short option and too large
    a gate for a debit that collateral can fund.
    """
    eq = margins.get("equity") or {}
    net = eq.get("net")
    if net is not None:
        try:
            return float(net)
        except (TypeError, ValueError):
            pass
    return _available_cash(margins)


def _block_total(block: Any) -> Optional[float]:
    if isinstance(block, dict) and block.get("total") is not None:
        try:
            return float(block["total"])
        except (TypeError, ValueError):
            return None
    return None


def _basket_quote_from_response(resp: Any) -> Optional[_BasketQuote]:
    """Split a Kite basket payload into place-time and post-hedge margin.

    ``initial.total`` is what Zerodha requires to accept the orders.
    ``final.total`` is what stays blocked once the hedge benefit applies.
    A payload with only one of them uses that figure for both. A list of
    per-order totals (order-margins fallback) has no hedge split.
    """
    if resp is None:
        return None
    if isinstance(resp, list):
        total = _required_from_margin_response(resp)
        if total is None:
            return None
        return _BasketQuote(initial=total, final=total)
    if not isinstance(resp, dict):
        return None
    data = resp.get("data")
    payload = data if isinstance(data, dict) and (
        "final" in data or "initial" in data or "total" in data
    ) else resp
    if not isinstance(payload, dict):
        return None
    initial_block = payload.get("initial") if isinstance(payload.get("initial"), dict) else None
    final_block = payload.get("final") if isinstance(payload.get("final"), dict) else None
    initial = _block_total(initial_block)
    final = _block_total(final_block)
    if initial is None and final is None:
        total = _required_from_margin_response(payload)
        if total is None:
            return None
        initial = final = total
    elif initial is None:
        initial = final
    elif final is None:
        final = initial
    premium: Optional[float] = None
    if initial_block is not None and initial_block.get("option_premium") is not None:
        try:
            premium = abs(float(initial_block["option_premium"]))
        except (TypeError, ValueError):
            premium = None
    return _BasketQuote(initial=float(initial), final=float(final), option_premium=premium)


def _required_from_margin_response(resp: Any) -> Optional[float]:
    """Parse Kite basket / order-margins payload for the rupee total."""
    if resp is None:
        return None
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict) and (
            "final" in data or "initial" in data or "total" in data
        ):
            return _required_from_margin_response(data)
        for key in ("final", "initial"):
            block = resp.get(key)
            if isinstance(block, dict) and block.get("total") is not None:
                try:
                    return float(block["total"])
                except (TypeError, ValueError):
                    continue
        if resp.get("total") is not None:
            try:
                return float(resp["total"])
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(resp, list):
        total = 0.0
        found = False
        for row in resp:
            if not isinstance(row, dict) or row.get("total") is None:
                continue
            try:
                total += float(row["total"])
                found = True
            except (TypeError, ValueError):
                continue
        return total if found else None
    return None


_KITE_MARGIN_PARAM_KEYS = (
    "variety",
    "exchange",
    "tradingsymbol",
    "transaction_type",
    "quantity",
    "product",
    "order_type",
    "price",
)


def _params_for_kite(order_params: List[dict]) -> List[dict]:
    """Drop UI-only keys (e.g. leg_order) before calling Kite margin APIs."""
    out: List[dict] = []
    for p in order_params:
        out.append({k: p[k] for k in _KITE_MARGIN_PARAM_KEYS if k in p})
    return out


def _estimate_basket_quote(
    facade: Any,
    order_params: List[dict],
    *,
    errors: List[str],
) -> Optional[_BasketQuote]:
    kite_params = _params_for_kite(order_params)
    if hasattr(facade, "basket_order_margins"):
        try:
            quote = _basket_quote_from_response(
                facade.basket_order_margins(kite_params)
            )
            if quote is not None:
                return quote
        except Exception as exc:
            errors.append(f"basket_order_margins: {exc}")
            logger.warning("basket_order_margins failed: %s", exc)
    if len(kite_params) == 1 and hasattr(facade, "order_margins"):
        try:
            return _basket_quote_from_response(
                facade.order_margins(kite_params)
            )
        except Exception as exc:
            errors.append(f"order_margins: {exc}")
            logger.warning("order_margins failed: %s", exc)
    return None


def _build_margin_path_steps(
    order_params: List[dict],
    cumulatives: List[float],
) -> List[dict]:
    steps: List[dict] = []
    prev = 0.0
    for i, cum in enumerate(cumulatives):
        p = order_params[i] if i < len(order_params) else {}
        delta = cum - prev
        steps.append({
            "step": i + 1,
            "leg_order": p.get("leg_order"),
            "tradingsymbol": str(p.get("tradingsymbol") or ""),
            "transaction_type": str(p.get("transaction_type") or ""),
            "cumulative": round(cum, 2),
            "delta": round(delta, 2),
        })
        prev = cum
    return steps


def check_margin_for_orders(
    facade: Any,
    order_params: List[dict],
    *,
    fallback_required: Optional[float] = None,
) -> MarginCheckResult:
    """Estimate margin along the execution path; gate on the hedged peak.

    For multi-leg orders we call Kite basket margins on each prefix of the
    planned sequence (same order as placement). Peak and Final use the
    basket ``final`` total (margin once the hedge is counted). Equity
    **net** (Kite “Margin available”) must cover peak × (1 + buffer).
    Option premium, when Zerodha reports it, must also fit in cash.
    Shortfall is returned so the UI can show Add ₹X. A sell that follows
    a filled buy is held separately until the live order-margin check
    sees that hedge.
    """
    if not order_params:
        return MarginCheckResult(ok=True)
    if not ZERODHA_EXECUTION_CONFIG.get("margin_check_enabled", True):
        return MarginCheckResult(ok=True)

    errors: List[str] = []
    quotes: List[_BasketQuote] = []
    for i in range(1, len(order_params) + 1):
        quote = _estimate_basket_quote(
            facade, order_params[:i], errors=errors,
        )
        if quote is None:
            break
        quotes.append(quote)

    final_required: Optional[float] = None
    peak_required: Optional[float] = None
    cash_required: Optional[float] = None
    path_steps: List[dict] = []

    if len(quotes) == len(order_params):
        place_totals = [q.final for q in quotes]
        final_required = quotes[-1].final
        peak_required = max(place_totals)
        premiums = [q.option_premium for q in quotes if q.option_premium]
        cash_required = max(premiums) if premiums else None
        path_steps = _build_margin_path_steps(order_params, place_totals)
    elif fallback_required is not None:
        try:
            final_required = abs(float(fallback_required))
            peak_required = final_required
        except (TypeError, ValueError):
            final_required = None
            peak_required = None

    if peak_required is None or final_required is None:
        detail = "; ".join(errors) if errors else "empty Kite margin response"
        return MarginCheckResult(
            ok=False,
            message=(
                "Execution blocked: could not estimate required margin from "
                f"Zerodha ({detail})"
            ),
        )

    try:
        margins = facade.margins()
    except Exception as exc:
        return MarginCheckResult(
            ok=False,
            required=peak_required,
            peak_required=peak_required,
            final_required=final_required,
            path_steps=path_steps,
            message=(
                "Execution blocked: could not read Zerodha account balance "
                f"({exc})"
            ),
        )
    # SPAN / order gate uses equity net — same figure Kite shows as
    # "Margin available" on insufficient-funds rejects. Cash alone is for
    # option premium (checked separately below).
    available = _available_order_margin(margins)
    cash = _available_cash(margins)
    if available is None:
        return MarginCheckResult(
            ok=False,
            required=peak_required,
            peak_required=peak_required,
            final_required=final_required,
            path_steps=path_steps,
            message="Execution blocked: Zerodha did not report available margin",
        )
    buffer_pct = float(ZERODHA_EXECUTION_CONFIG.get("margin_buffer_pct", 5)) / 100.0
    need = peak_required * (1.0 + buffer_pct)
    shortfall = 0.0
    shortfalls: List[str] = []
    if available < need:
        shortfall = need - available
        shortfalls.append(
            f"Margin required: ₹{need:,.2f} "
            f"(peak ₹{peak_required:,.2f} + {buffer_pct * 100:.0f}% buffer; "
            f"final structure ₹{final_required:,.2f}). "
            f"Margin available: ₹{available:,.2f}. "
            f"Add ₹{shortfall:,.2f} to place this order."
        )
    if cash_required is not None and cash is not None:
        cash_need = cash_required * (1.0 + buffer_pct)
        if cash < cash_need:
            cash_short = cash_need - cash
            shortfall = max(shortfall, cash_short)
            shortfalls.append(
                f"Option premium needs ₹{cash_need:,.2f} cash "
                f"(₹{cash_required:,.2f} + {buffer_pct * 100:.0f}% buffer); "
                f"cash ₹{cash:,.2f}. Add ₹{cash_short:,.2f}."
            )
    if shortfalls:
        return MarginCheckResult(
            ok=False,
            required=peak_required,
            peak_required=peak_required,
            final_required=final_required,
            available=available,
            buffer_pct=buffer_pct * 100.0,
            shortfall=round(shortfall, 2),
            path_steps=path_steps,
            message="Insufficient funds. " + " ".join(shortfalls),
        )
    return MarginCheckResult(
        ok=True,
        required=peak_required,
        peak_required=peak_required,
        final_required=final_required,
        available=available,
        buffer_pct=buffer_pct * 100.0,
        shortfall=0.0,
        path_steps=path_steps,
        message=(
            f"Peak during place ₹{peak_required:,.0f}; "
            f"final structure ₹{final_required:,.0f} "
            f"(+{buffer_pct * 100:.0f}% buffer on peak)"
        ),
    )


def hedged_sell_block_reason(
    facade: Any,
    sell_order: dict,
    hedge_qty_by_symbol: Dict[str, int],
) -> Optional[str]:
    """None when this sell can be sent against hedges that already filled.

    The single-order margin call sees current positions. Until Zerodha's
    margin picture includes the long, that call still returns the naked
    short margin and this returns a reason to keep waiting.
    """
    if not hedge_qty_by_symbol:
        return None
    held = _broker_net_by_symbol(facade)
    for sym, qty in hedge_qty_by_symbol.items():
        have = int(held.get(sym) or 0)
        need = int(qty)
        if have < need:
            return f"{sym} position {have} is below filled hedge {need}"
    errors: List[str] = []
    quote = _estimate_basket_quote(facade, [sell_order], errors=errors)
    if quote is None:
        detail = "; ".join(errors) if errors else "empty margin response"
        return f"could not read sell margin ({detail})"
    try:
        margins = facade.margins()
    except Exception as exc:
        return f"could not read available margin ({exc})"
    available = _available_order_margin(margins)
    if available is None:
        return "Zerodha did not report available margin"
    required = float(quote.initial)
    if available < required:
        return (
            f"sell still needs ₹{required:,.2f} against available ₹{available:,.2f}"
        )
    return None


def wait_until_hedged_sell_fits(
    facade: Any,
    sell_order: dict,
    hedge_qty_by_symbol: Dict[str, int],
    *,
    wait_sec: float,
    poll_sec: float,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> Optional[str]:
    """Poll until the sell's live margin fits. Reason string if it never does."""
    if not hedge_qty_by_symbol:
        return None
    deadline = monotonic_fn() + max(0.0, float(wait_sec))
    poll = max(0.2, float(poll_sec))
    last = "hedge not confirmed"
    while True:
        try:
            last = hedged_sell_block_reason(facade, sell_order, hedge_qty_by_symbol)
        except Exception as exc:
            last = str(exc)
        if last is None:
            return None
        if monotonic_fn() >= deadline:
            return last
        sleep_fn(poll)


def build_order_margin_params(
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    *,
    transaction_fn,
    limit_fn,
    product: str,
    variety: str,
) -> List[dict]:
    out: List[dict] = []
    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_map[lo]
        txn = transaction_fn(leg)
        lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
        qty = lots * int(inst.lot_size or leg.get("lot_size") or 0)
        if qty <= 0:
            raise ValueError(
                f"leg {lo}: quantity is {qty} — cannot estimate margin"
            )
        limit_px = limit_fn(lo, inst, txn)
        out.append({
            "variety": variety,
            "exchange": inst.exchange,
            "tradingsymbol": inst.tradingsymbol,
            "transaction_type": txn,
            "quantity": qty,
            "product": product,
            "order_type": "LIMIT",
            "price": limit_px,
            "leg_order": lo,
        })
    return out


def check_exposure_conflicts(
    facade: Any,
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    *,
    transaction_fn,
    allow_existing_positions: bool = False,
) -> ExposureCheckResult:
    """Block if Kite already holds same symbol in conflicting direction.

    When ``allow_existing_positions`` is True (supplement / continue trade),
    skip the check — partial structures already have broker legs open.
    """
    if not ZERODHA_EXECUTION_CONFIG.get("exposure_check_enabled", True):
        return ExposureCheckResult(ok=True)
    if allow_existing_positions:
        return ExposureCheckResult(ok=True)
    try:
        pos = facade.positions()
    except Exception as exc:
        logger.warning("positions fetch failed for exposure check: %s", exc)
        return ExposureCheckResult(
            ok=False,
            message=(
                "Execution blocked: could not fetch Zerodha positions for "
                f"exposure check ({exc})"
            ),
        )

    net_rows = pos.get("net") or pos.get("day") or []
    by_sym: Dict[str, int] = {}
    for row in net_rows:
        sym = str(row.get("tradingsymbol") or "")
        qty = int(row.get("quantity") or 0)
        if sym and qty:
            by_sym[sym] = by_sym.get(sym, 0) + qty

    conflicts: List[str] = []
    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_map[lo]
        sym = inst.tradingsymbol
        existing = by_sym.get(sym, 0)
        if existing == 0:
            continue
        txn = transaction_fn(leg).upper()
        # Entry-only: block adding exposure in the same direction as an
        # existing manual position. Covering/reducing an existing opposite
        # position (BUY when short, SELL when long) is allowed.
        if txn == "BUY" and existing > 0:
            conflicts.append(
                f"leg {lo}: already long {existing} {sym} — entry BUY would add exposure"
            )
        elif txn == "SELL" and existing < 0:
            conflicts.append(
                f"leg {lo}: already short {abs(existing)} {sym} — entry SELL would add exposure"
            )

    if conflicts:
        return ExposureCheckResult(
            ok=False,
            conflicts=conflicts,
            message="; ".join(conflicts),
        )
    return ExposureCheckResult(ok=True)


def expected_net_qty_from_legs(
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    *,
    transaction_fn,
    sign: int = 1,
) -> Dict[str, int]:
    """Build tradingsymbol → signed net qty from leg plan (sign=1 entry, -1 exit)."""
    out: Dict[str, int] = {}
    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_map[lo]
        txn = transaction_fn(leg).upper()
        lots = int(leg.get("lots_actual") or leg.get("lots") or 1)
        qty = lots * int(inst.lot_size or leg.get("lot_size") or 0)
        delta = qty if txn == "BUY" else -qty
        out[inst.tradingsymbol] = out.get(inst.tradingsymbol, 0) + sign * delta
    return out


def reconcile_positions_after_fill(
    facade: Any,
    legs: List[dict],
    inst_map: Dict[int, Instrument],
    *,
    transaction_fn,
    mode: str = "entry",
) -> PositionReconcileResult:
    """Compare Kite net positions to expected structure qty — run once after all legs."""
    if not ZERODHA_EXECUTION_CONFIG.get("position_reconcile_enabled", True):
        return PositionReconcileResult(ok=True)
    try:
        pos = facade.positions()
    except Exception as exc:
        return PositionReconcileResult(
            ok=True,
            message=f"position reconcile skipped: {exc}",
        )

    net_rows = pos.get("net") or []
    kite_net: Dict[str, int] = {}
    for row in net_rows:
        sym = str(row.get("tradingsymbol") or "")
        qty = int(row.get("quantity") or 0)
        if sym:
            kite_net[sym] = qty

    sign = -1 if str(mode).lower() in ("close", "exit") else 1
    expected = expected_net_qty_from_legs(
        legs, inst_map, transaction_fn=transaction_fn, sign=sign,
    )

    mismatches: List[str] = []
    symbols = set(expected) | set(kite_net)
    for sym in sorted(symbols):
        exp_delta = expected.get(sym, 0)
        if exp_delta == 0:
            continue
        # After entry, kite qty should reflect cumulative position for sym.
        # We only flag if symbol is in our legs and kite qty is zero while we expected change.
        kite_qty = kite_net.get(sym, 0)
        if kite_qty == 0 and exp_delta != 0:
            mismatches.append(f"{sym}: expected broker position change, Kite shows flat")
        # For multi-leg same symbol (rare), skip strict qty match.

    if mismatches:
        return PositionReconcileResult(
            ok=False,
            mismatches=mismatches,
            message="; ".join(mismatches),
        )
    return PositionReconcileResult(ok=True)
