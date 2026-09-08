"""Round-trip P&L + charges for auto-reverted Zerodha fills.

When entry (or a failed close) is flattened, Kite still has two real
orders per reversed quantity: the original fill and the opposite
ROLLBACK. Those are not stored on ``options_trades`` because no trade
row is created (suggestion stays PENDING for retry).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Iterable, List, Optional

from engine.charges import estimate_charges_per_txn


_FILLED = frozenset({"COMPLETE", "PARTIAL"})


def _op(row: dict) -> str:
    return str(row.get("operation") or "").upper()


def _status(row: dict) -> str:
    return str(row.get("status") or "").upper()


def _qty(row: dict) -> int:
    try:
        return int(row.get("filled_quantity") or 0)
    except (TypeError, ValueError):
        return 0


def _px(row: dict) -> float:
    try:
        return float(row.get("fill_price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _txn(row: dict) -> str:
    return str(row.get("transaction_type") or "").upper()


def _is_filled(row: dict) -> bool:
    return _status(row) in _FILLED and _qty(row) > 0


def order_fingerprint(rows: Iterable[dict]) -> str:
    ids = sorted(str(r.get("id")) for r in rows if r.get("id") is not None and _is_filled(r))
    raw = ",".join(ids) if ids else "empty"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def compute_reversal_from_orders(
    rows: Iterable[dict],
    *,
    open_operation: str = "ENTRY",
) -> Optional[dict]:
    """Return gross/charges/net for filled ``open_operation`` vs ROLLBACK.

    Gross uses the open order's side: BUY then sell-back is long P&L;
    SELL then buy-back is short P&L. Unmatched leftover qty is ignored
    for gross (still open) but every filled order still pays charges.
    """
    open_op = (open_operation or "ENTRY").upper()
    orders = [dict(r) for r in rows]
    filled = [r for r in orders if _is_filled(r)]
    if not filled:
        return None
    opens = [r for r in filled if _op(r) == open_op]
    closes = [r for r in filled if _op(r) == "ROLLBACK"]
    if not opens and not closes:
        return None

    by_leg_open: dict[int, List[dict]] = defaultdict(list)
    by_leg_close: dict[int, List[dict]] = defaultdict(list)
    for r in sorted(opens, key=lambda x: int(x.get("id") or 0)):
        by_leg_open[int(r.get("leg_order") or 0)].append({
            "remaining": _qty(r), "px": _px(r), "txn": _txn(r),
            "symbol": r.get("tradingsymbol"), "id": r.get("id"),
        })
    for r in sorted(closes, key=lambda x: int(x.get("id") or 0)):
        by_leg_close[int(r.get("leg_order") or 0)].append({
            "remaining": _qty(r), "px": _px(r), "txn": _txn(r),
            "symbol": r.get("tradingsymbol"), "id": r.get("id"),
        })

    gross = 0.0
    matched_qty = 0
    legs: List[dict] = []
    for leg in sorted(set(by_leg_open) | set(by_leg_close)):
        oqueue = by_leg_open.get(leg, [])
        cqueue = by_leg_close.get(leg, [])
        oi = ci = 0
        while oi < len(oqueue) and ci < len(cqueue):
            o, c = oqueue[oi], cqueue[ci]
            take = min(o["remaining"], c["remaining"])
            if take <= 0:
                if o["remaining"] <= 0:
                    oi += 1
                if c["remaining"] <= 0:
                    ci += 1
                continue
            side = o["txn"] or "BUY"
            if side == "SELL":
                piece = (o["px"] - c["px"]) * take
            else:
                piece = (c["px"] - o["px"]) * take
            gross += piece
            matched_qty += take
            legs.append({
                "leg_order": leg,
                "symbol": o.get("symbol") or c.get("symbol"),
                "entry_side": side,
                "entry_price": o["px"],
                "exit_price": c["px"],
                "quantity": take,
                "gross_pnl": round(piece, 4),
            })
            o["remaining"] -= take
            c["remaining"] -= take
            if o["remaining"] <= 0:
                oi += 1
            if c["remaining"] <= 0:
                ci += 1

    if matched_qty <= 0:
        return None
    charge_legs = []
    for r in filled:
        if _op(r) not in {open_op, "ROLLBACK"}:
            continue
        qty = _qty(r)
        charge_legs.append({
            "action": _txn(r) or "BUY",
            "price": _px(r),
            "lots": 1,
            "lot_size": qty,
        })
    charges = estimate_charges_per_txn(charge_legs)
    net = round(gross - charges.total, 2)
    return {
        "gross_pnl": round(gross, 2),
        "total_charges": charges.total,
        "net_pnl": net,
        "brokerage": charges.brokerage,
        "stt": charges.stt,
        "exchange_charges": charges.exchange,
        "sebi": charges.sebi,
        "stamp_duty": charges.stamp_duty,
        "gst": charges.gst,
        "matched_qty": matched_qty,
        "order_fingerprint": order_fingerprint(filled),
        "legs_json": json.dumps(legs, default=str),
        "notes": (
            f"{open_op} flattened: matched {matched_qty} shares, "
            f"gross {gross:.2f}, charges {charges.total:.2f}"
        ),
    }
