"""
Pre-trade margin / exposure checks and post-fill position reconciliation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import ZERODHA_EXECUTION_CONFIG
from providers.zerodha.instruments import Instrument


logger = logging.getLogger(__name__)


@dataclass
class MarginCheckResult:
    ok: bool
    required: float = 0.0
    available: float = 0.0
    message: str = ""


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


def _available_cash(margins: dict) -> Optional[float]:
    """Usable equity funds — same preference as account_snapshot (live_balance)."""
    eq = margins.get("equity") or {}
    avail = eq.get("available") or {}
    for key in ("live_balance", "cash"):
        val = avail.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    net = eq.get("net")
    if net is not None:
        try:
            return float(net)
        except (TypeError, ValueError):
            pass
    return None


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


def check_margin_for_orders(
    facade: Any,
    order_params: List[dict],
    *,
    fallback_required: Optional[float] = None,
) -> MarginCheckResult:
    """Estimate margin for planned orders and compare to usable Zerodha cash.

    Fail-closed: if Kite cannot report required margin or available funds,
    execution is blocked. Multi-leg structures use basket margins so hedge
    benefit is counted; a single-leg order can use ``order_margins``.
    """
    if not order_params:
        return MarginCheckResult(ok=True)
    if not ZERODHA_EXECUTION_CONFIG.get("margin_check_enabled", True):
        return MarginCheckResult(ok=True)

    required: Optional[float] = None
    errors: List[str] = []

    if hasattr(facade, "basket_order_margins"):
        try:
            required = _required_from_margin_response(
                facade.basket_order_margins(order_params)
            )
        except Exception as exc:
            errors.append(f"basket_order_margins: {exc}")
            logger.warning("basket_order_margins failed: %s", exc)

    if required is None and len(order_params) == 1:
        try:
            required = _required_from_margin_response(
                facade.order_margins(order_params)
            )
        except Exception as exc:
            errors.append(f"order_margins: {exc}")
            logger.warning("order_margins failed: %s", exc)

    if required is None and fallback_required is not None:
        try:
            required = abs(float(fallback_required))
        except (TypeError, ValueError):
            required = None

    if required is None:
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
            required=required,
            message=(
                "Execution blocked: could not read Zerodha account balance "
                f"({exc})"
            ),
        )
    available = _available_cash(margins)
    if available is None:
        return MarginCheckResult(
            ok=False,
            required=required,
            message="Execution blocked: Zerodha did not report usable funds",
        )
    buffer_pct = float(ZERODHA_EXECUTION_CONFIG.get("margin_buffer_pct", 5)) / 100.0
    need = required * (1.0 + buffer_pct)
    if available < need:
        return MarginCheckResult(
            ok=False,
            required=required,
            available=available,
            message=(
                f"Insufficient funds in Zerodha: need ~₹{need:,.0f} "
                f"(required ₹{required:,.0f} + {buffer_pct * 100:.0f}% buffer), "
                f"available ₹{available:,.0f}"
            ),
        )
    return MarginCheckResult(ok=True, required=required, available=available)


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
        logger.debug("positions fetch failed: %s", exc)
        return ExposureCheckResult(ok=True, message=f"exposure check skipped: {exc}")

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
