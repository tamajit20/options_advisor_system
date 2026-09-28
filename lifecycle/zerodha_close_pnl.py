"""Overwrite a closed Zerodha trade's P&L from that trade's Kite order ids.

Paper/manual closes never call this. Kite is queried only by the
``kite_order_id`` values owned by ``trade_id`` (plus EXIT fills from *this*
close). The day's full order book is never used, so two trades cannot mix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from config import ZERODHA_EXECUTION_CONFIG
from database.broker_order_repo import BrokerOrderRepo
from database.connection import SQLServerConnection
from database.models import TradeRepo


logger = logging.getLogger(__name__)

_ENTRY_OPS = frozenset({"ENTRY", "SUPPLEMENT"})
_EXIT_OPS = frozenset({"EXIT"})
_BOOKED_OPS = _ENTRY_OPS | _EXIT_OPS


@dataclass(frozen=True)
class _MappedOrder:
    kite_order_id: str
    leg_order: int
    operation: str
    tradingsymbol: str
    average_price: float
    quantity: int
    note_params: dict


def reconcile_closed_trade_from_kite(
    db: SQLServerConnection,
    trade_id: str,
    facade: Any,
    *,
    extra_fills: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Optional[dict]:
    """Replace estimated close P&L with Kite fills + virtual contract note.

    Returns the applied snapshot, or ``None`` when this is not a Zerodha
    trade / the pull is incomplete (estimated figures are left as-is).
    """
    tid = str(trade_id or "").strip()
    if not tid:
        return None
    broker = BrokerOrderRepo(db)
    if not broker.has_entry_kite_fills_for_trade(tid):
        logger.info(
            "Skip Kite close P&L for %s — no opening Kite fills (paper/manual)",
            tid,
        )
        return None

    mapped = _mapped_orders_for_trade(broker, facade, tid, extra_fills or ())
    if mapped is None:
        return None

    charges_total = _charges_for_mapped_orders(facade, tid, mapped)
    if charges_total is None:
        return None

    trd = TradeRepo(db)
    trade = trd.get(tid)
    if trade is None:
        logger.warning("Kite close P&L: trade %s missing after close", tid)
        return None

    legs = [l for l in (trd.legs_with_suggestion_info(tid) or []) if l.get("executed")]
    if not legs:
        logger.warning("Kite close P&L: trade %s has no executed legs", tid)
        return None

    gross = 0.0
    for leg in legs:
        lo = int(leg["leg_order"])
        lots = int(leg.get("lots_actual") or leg.get("lots") or 0)
        lot_size = int(leg.get("lot_size") or 0)
        entry_px = _price_for_leg(mapped, lo, _ENTRY_OPS)
        if entry_px is None:
            try:
                entry_px = float(leg.get("fill_price") or 0)
            except (TypeError, ValueError):
                entry_px = 0.0
        exit_px = _price_for_leg(mapped, lo, _EXIT_OPS)
        if exit_px is None:
            try:
                exit_px = float(leg.get("exit_price") or 0)
            except (TypeError, ValueError):
                exit_px = 0.0
        if entry_px <= 0 or exit_px <= 0 or lots <= 0 or lot_size <= 0:
            logger.warning(
                "Kite close P&L: trade %s leg %s missing fill/exit — keeping estimate",
                tid, lo,
            )
            return None
        sign = 1.0 if str(leg.get("action") or "").upper() == "SELL" else -1.0
        leg_pnl = sign * (entry_px - exit_px) * lots * lot_size
        gross += leg_pnl
        if abs(entry_px - float(leg.get("fill_price") or 0)) > 1e-9:
            trd.update_leg_fill(
                tid, lo, entry_px, leg.get("fill_time"),
                leg.get("lots_actual"),
            )
        trd.update_leg_exit(tid, lo, exit_px, leg.get("exit_time"), leg_pnl)

    gross = round(gross, 2)
    charges = round(float(charges_total), 2)
    net = round(gross - charges, 2)
    trd.update_pnl(tid, gross, charges, net)
    db.commit()
    logger.info(
        "Trade %s Kite close P&L applied: gross=%.2f charges=%.2f net=%.2f "
        "orders=%s",
        tid, gross, charges, net,
        ",".join(m.kite_order_id for m in mapped),
    )
    return {
        "trade_id": tid,
        "gross_pnl": gross,
        "total_charges": charges,
        "net_pnl": net,
        "kite_order_ids": [m.kite_order_id for m in mapped],
    }


def closed_zerodha_trade_ids_on(db: SQLServerConnection, day: date) -> List[str]:
    """CLOSED Zerodha trades whose ``closed_on`` date is ``day`` (IST).

    Paper/manual trades (no opening Kite fill) are omitted. Each id is
    returned once so a later pull can be scoped to that trade only.
    """
    rows = db.fetch_all(
        """
        SELECT t.trade_id
        FROM options_trades t
        WHERE t.status = 'CLOSED'
          AND CAST(t.closed_on AS DATE) = ?
        ORDER BY t.closed_on, t.trade_id
        """,
        [day],
    ) or []
    broker = BrokerOrderRepo(db)
    out: List[str] = []
    seen: Set[str] = set()
    for row in rows:
        tid = str((row or {}).get("trade_id") or "").strip()
        if not tid or tid in seen:
            continue
        if not broker.has_entry_kite_fills_for_trade(tid):
            continue
        seen.add(tid)
        out.append(tid)
    return out


def reconcile_closed_zerodha_trades_on(
    db: SQLServerConnection,
    facade: Any,
    day: date,
    *,
    dry_run: bool = False,
) -> List[dict]:
    """Pull Kite P&L for each closed Zerodha trade on ``day``, one id at a time."""
    results: List[dict] = []
    for tid in closed_zerodha_trade_ids_on(db, day):
        if dry_run:
            results.append({"trade_id": tid, "dry_run": True, "applied": False})
            continue
        snap = reconcile_closed_trade_from_kite(db, tid, facade)
        results.append({
            "trade_id": tid,
            "dry_run": False,
            "applied": snap is not None,
            "snapshot": snap,
        })
    return results


def _mapped_orders_for_trade(
    broker: BrokerOrderRepo,
    facade: Any,
    trade_id: str,
    extra_fills: Sequence[Mapping[str, Any]],
) -> Optional[List[_MappedOrder]]:
    owned = _owned_complete_rows(broker, trade_id)
    by_oid: Dict[str, dict] = {}
    for row in owned:
        oid = str(row.get("kite_order_id") or "").strip()
        if oid:
            by_oid[oid] = row

    for extra in extra_fills:
        oid = str(extra.get("kite_order_id") or "").strip()
        if not oid:
            continue
        try:
            lo = int(extra.get("leg_order"))
        except (TypeError, ValueError):
            logger.warning(
                "Kite close P&L: trade %s extra fill missing leg_order — skip pull",
                trade_id,
            )
            return None
        if oid in by_oid:
            continue
        owner = _other_trade_owning(broker, oid, trade_id)
        if owner:
            logger.warning(
                "Kite close P&L: kite order %s belongs to %s, not %s — skip pull",
                oid, owner, trade_id,
            )
            return None
        by_oid[oid] = {
            "kite_order_id": oid,
            "trade_id": trade_id,
            "leg_order": lo,
            "operation": "EXIT",
            "tradingsymbol": extra.get("tradingsymbol"),
            "exchange": extra.get("exchange"),
            "quantity": extra.get("quantity") or extra.get("filled_quantity"),
        }

    if not by_oid:
        logger.warning("Kite close P&L: trade %s has no kite_order_id values", trade_id)
        return None

    mapped: List[_MappedOrder] = []
    seen: Set[str] = set()
    for oid, row in by_oid.items():
        if oid in seen:
            continue
        seen.add(oid)
        owner = _other_trade_owning(broker, oid, trade_id)
        if owner:
            logger.warning(
                "Kite close P&L: kite order %s belongs to %s, not %s — skip pull",
                oid, owner, trade_id,
            )
            return None
        item = _map_one_order(facade, trade_id, oid, row)
        if item is None:
            return None
        mapped.append(item)
    return mapped


def _owned_complete_rows(broker: BrokerOrderRepo, trade_id: str) -> List[dict]:
    rows = broker.by_trade(trade_id) or []
    out: List[dict] = []
    for row in rows:
        if str(row.get("trade_id") or "") != trade_id:
            continue
        oid = str(row.get("kite_order_id") or "").strip()
        if not oid:
            continue
        if str(row.get("status") or "").upper() != "COMPLETE":
            continue
        op = str(row.get("operation") or "").upper()
        if op not in _BOOKED_OPS:
            continue
        out.append(row)
    return out


def _other_trade_owning(
    broker: BrokerOrderRepo, kite_order_id: str, trade_id: str,
) -> Optional[str]:
    try:
        rows = broker.by_kite_order_id(kite_order_id)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        other = str(row.get("trade_id") or "").strip()
        if other and other != trade_id:
            return other
    return None


def _map_one_order(
    facade: Any,
    trade_id: str,
    kite_order_id: str,
    row: dict,
) -> Optional[_MappedOrder]:
    try:
        history = facade.order_history(kite_order_id)
    except Exception:
        logger.exception(
            "Kite close P&L: order_history failed for %s (trade %s)",
            kite_order_id, trade_id,
        )
        return None
    snap = _complete_snapshot(history)
    if snap is None:
        logger.warning(
            "Kite close P&L: no COMPLETE snapshot for order %s (trade %s)",
            kite_order_id, trade_id,
        )
        return None
    snap_oid = str(snap.get("order_id") or kite_order_id).strip()
    if snap_oid and snap_oid != kite_order_id:
        logger.warning(
            "Kite close P&L: history order_id %s != requested %s (trade %s)",
            snap_oid, kite_order_id, trade_id,
        )
        return None

    our_sym = str(row.get("tradingsymbol") or "").strip()
    kite_sym = str(snap.get("tradingsymbol") or "").strip()
    if not kite_sym:
        logger.warning(
            "Kite close P&L: order %s has no tradingsymbol (trade %s)",
            kite_order_id, trade_id,
        )
        return None
    if our_sym and kite_sym != our_sym:
        logger.warning(
            "Kite close P&L: order %s symbol %s != our %s (trade %s)",
            kite_order_id, kite_sym, our_sym, trade_id,
        )
        return None

    try:
        avg = float(snap.get("average_price") or 0)
    except (TypeError, ValueError):
        avg = 0.0
    if avg <= 0:
        logger.warning(
            "Kite close P&L: order %s average_price missing (trade %s)",
            kite_order_id, trade_id,
        )
        return None
    try:
        qty = int(
            snap.get("filled_quantity")
            or snap.get("quantity")
            or row.get("filled_quantity")
            or row.get("quantity")
            or 0
        )
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        logger.warning(
            "Kite close P&L: order %s quantity missing (trade %s)",
            kite_order_id, trade_id,
        )
        return None

    cfg = ZERODHA_EXECUTION_CONFIG
    try:
        lo = int(row.get("leg_order"))
    except (TypeError, ValueError):
        logger.warning(
            "Kite close P&L: order %s has no leg_order (trade %s)",
            kite_order_id, trade_id,
        )
        return None
    op = str(row.get("operation") or "").upper() or "EXIT"
    txn = str(snap.get("transaction_type") or row.get("transaction_type") or "").upper()
    if txn not in ("BUY", "SELL"):
        logger.warning(
            "Kite close P&L: order %s has no BUY/SELL (trade %s)",
            kite_order_id, trade_id,
        )
        return None
    params = {
        "order_id": kite_order_id,
        "exchange": str(snap.get("exchange") or row.get("exchange") or "NFO"),
        "tradingsymbol": kite_sym,
        "transaction_type": txn,
        "variety": str(snap.get("variety") or cfg.get("variety") or "regular"),
        "product": str(snap.get("product") or cfg.get("product") or "NRML"),
        "order_type": str(snap.get("order_type") or row.get("order_type") or "LIMIT"),
        "quantity": qty,
        "average_price": avg,
    }
    return _MappedOrder(
        kite_order_id=kite_order_id,
        leg_order=lo,
        operation=op,
        tradingsymbol=kite_sym,
        average_price=avg,
        quantity=qty,
        note_params=params,
    )


def _complete_snapshot(history: Any) -> Optional[dict]:
    if not isinstance(history, list):
        return None
    complete: List[dict] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").upper() == "COMPLETE":
            complete.append(row)
    if complete:
        return complete[-1]
    return None


def _charges_for_mapped_orders(
    facade: Any,
    trade_id: str,
    mapped: List[_MappedOrder],
) -> Optional[float]:
    allow = {m.kite_order_id for m in mapped}
    params = [m.note_params for m in mapped]
    for p in params:
        if str(p.get("order_id") or "") not in allow:
            return None
    try:
        raw = facade.get_virtual_contract_note(params)
    except Exception:
        logger.exception(
            "Kite close P&L: virtual contract note failed for trade %s", trade_id,
        )
        return None
    rows = _as_charge_rows(raw)
    if rows is None or len(rows) != len(params):
        logger.warning(
            "Kite close P&L: virtual note size mismatch for trade %s "
            "(sent %s got %s)",
            trade_id, len(params), None if rows is None else len(rows),
        )
        return None
    total = 0.0
    for req, resp in zip(params, rows):
        resp_sym = str(resp.get("tradingsymbol") or "").strip()
        if resp_sym and resp_sym != str(req.get("tradingsymbol") or ""):
            logger.warning(
                "Kite close P&L: virtual note symbol mismatch %s vs %s (trade %s)",
                resp_sym, req.get("tradingsymbol"), trade_id,
            )
            return None
        charges = resp.get("charges") if isinstance(resp.get("charges"), dict) else {}
        try:
            total += float(charges.get("total") or 0)
        except (TypeError, ValueError):
            logger.warning(
                "Kite close P&L: virtual note missing charges.total (trade %s)",
                trade_id,
            )
            return None
    return total


def _as_charge_rows(raw: Any) -> Optional[List[dict]]:
    if raw is None:
        return None
    if isinstance(raw, dict) and "data" in raw:
        raw = raw["data"]
    if not isinstance(raw, list):
        return None
    if not all(isinstance(x, dict) for x in raw):
        return None
    return raw


def _price_for_leg(
    mapped: Iterable[_MappedOrder],
    leg_order: int,
    ops: Set[str],
) -> Optional[float]:
    items = [m for m in mapped if m.leg_order == leg_order and m.operation in ops]
    if not items:
        return None
    if len(items) == 1:
        return float(items[0].average_price)
    num = sum(m.average_price * m.quantity for m in items)
    den = sum(m.quantity for m in items)
    if den <= 0:
        return None
    return num / den
