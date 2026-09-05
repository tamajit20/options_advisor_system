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


def _available_cash(margins: dict) -> float:
    eq = margins.get("equity") or {}
    avail = eq.get("available") or {}
    for key in ("live_balance", "cash", "net"):
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
    return 0.0


def check_margin_for_orders(
    facade: Any,
    order_params: List[dict],
) -> MarginCheckResult:
    """Estimate margin for planned orders via Kite ``order_margins``."""
    if not order_params:
        return MarginCheckResult(ok=True)
    if not ZERODHA_EXECUTION_CONFIG.get("margin_check_enabled", True):
        return MarginCheckResult(ok=True)
    try:
        resp = facade.order_margins(order_params)
    except Exception as exc:
        logger.warning("order_margins failed: %s", exc)
        return MarginCheckResult(ok=True, message=f"margin check skipped: {exc}")

    required = 0.0
    if isinstance(resp, list):
        for row in resp:
            req = row.get("total") if isinstance(row, dict) else None
            if req is not None:
                required += float(req)
    elif isinstance(resp, dict):
        req = resp.get("total")
        if req is not None:
            required = float(req)

    try:
        margins = facade.margins()
    except Exception as exc:
        return MarginCheckResult(
            ok=True,
            required=required,
            message=f"margin available unknown: {exc}",
        )
    available = _available_cash(margins)
    buffer_pct = float(ZERODHA_EXECUTION_CONFIG.get("margin_buffer_pct", 5)) / 100.0
    need = required * (1.0 + buffer_pct)
    if available < need:
        return MarginCheckResult(
            ok=False,
            required=required,
            available=available,
            message=(
                f"Insufficient margin: need ~{need:,.0f} (incl. buffer), "
                f"available {available:,.0f}"
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
