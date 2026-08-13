"""
database/scout_models.py — repositories for scout_* tables only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database.connection import SQLServerConnection
from utils import now_ist

ACTIVE_TRADE_STATUSES = ("OPEN", "PENDING_ENTRY", "CLOSING", "UNPROTECTED")


def _row(d: Dict[str, Any]) -> Dict[str, Any]:
    if not d:
        return d
    out = dict(d)
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat(sep=" ", timespec="seconds")
    return out


class ScoutSignalRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(
        self,
        *,
        scan_id: str,
        symbol: str,
        exchange: str,
        action: str,
        signal_type: str,
        reason: str,
        ltp: float,
        invalidation: Optional[float],
        strength: str,
        triggered_at: datetime,
        meta: Optional[dict] = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO scout_signals "
            "(scan_id, symbol, exchange, action, signal_type, reason, ltp, "
            " invalidation, strength, triggered_at, meta_json) "
            "OUTPUT INSERTED.id "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                scan_id, symbol, exchange, action, signal_type, reason, ltp,
                invalidation, strength, triggered_at,
                json.dumps(meta) if meta else None,
            ],
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0

    def recent(self, limit: int = 50, since_minutes: int = 120) -> List[dict]:
        since = now_ist() - timedelta(minutes=since_minutes)
        rows = self.db.fetch_all(
            "SELECT TOP (?) id, scan_id, symbol, exchange, action, signal_type, "
            "       reason, ltp, invalidation, strength, triggered_at, meta_json "
            "FROM scout_signals "
            "WHERE triggered_at >= ? "
            "ORDER BY triggered_at DESC",
            [limit, since],
        )
        out = []
        for r in rows:
            row = _row(r)
            if row.get("meta_json"):
                try:
                    row["meta"] = json.loads(row["meta_json"])
                except json.JSONDecodeError:
                    row["meta"] = None
            else:
                row["meta"] = None
            out.append(row)
        return out

    def last_signal(self) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT TOP 1 symbol, action, signal_type, triggered_at "
            "FROM scout_signals ORDER BY triggered_at DESC",
        )
        return _row(row) if row else None

    def signal_ids_without_trade(self, *, since_minutes: int = 120) -> List[int]:
        """Signal rows with no scout_trades row yet — candidates for auto-enter poll."""
        since = now_ist() - timedelta(minutes=max(5, since_minutes))
        rows = self.db.fetch_all(
            "SELECT s.id FROM scout_signals s "
            "WHERE s.triggered_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM scout_trades t WHERE t.signal_id = s.id) "
            "ORDER BY s.triggered_at DESC",
            [since],
        )
        out: List[int] = []
        for r in rows:
            try:
                out.append(int(r["id"]))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def get(self, signal_id: int) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT id, scan_id, symbol, exchange, action, signal_type, "
            "       reason, ltp, invalidation, strength, triggered_at, meta_json "
            "FROM scout_signals WHERE id = ?",
            [signal_id],
        )
        if not row:
            return None
        out = _row(row)
        if out.get("meta_json"):
            try:
                out["meta"] = json.loads(out["meta_json"])
            except json.JSONDecodeError:
                out["meta"] = None
        else:
            out["meta"] = None
        return out


class ScoutConfigRepo:
    WATCHLIST_KEY = "watchlist"
    AUTOMATION_KEY = "automation"
    SETTINGS_KEY = "settings"

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def _merge_json(self, key: str, payload: dict, *, updated_by: str = "ui") -> None:
        val = json.dumps(payload)
        self.db.execute(
            """
            MERGE scout_config AS T
            USING (SELECT ? AS config_key, ? AS config_value) AS S
            ON T.config_key = S.config_key
            WHEN MATCHED THEN UPDATE SET
                config_value = S.config_value,
                updated_at = SYSUTCDATETIME(),
                updated_by = ?
            WHEN NOT MATCHED THEN INSERT
                (config_key, config_value, updated_by)
                VALUES (S.config_key, S.config_value, ?);
            """,
            [key, val, updated_by, updated_by],
        )

    def get_json(self, key: str) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT config_value FROM scout_config WHERE config_key = ?",
            [key],
        )
        if not row or not row.get("config_value"):
            return None
        try:
            data = json.loads(row["config_value"])
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def get_automation(self) -> Optional[dict]:
        return self.get_json(self.AUTOMATION_KEY)

    def get_settings(self) -> Optional[dict]:
        return self.get_json(self.SETTINGS_KEY)

    def set_automation(self, settings: dict, *, updated_by: str = "ui") -> None:
        self._merge_json(self.AUTOMATION_KEY, settings, updated_by=updated_by)

    def set_settings(self, settings: dict, *, updated_by: str = "ui") -> None:
        self._merge_json(self.SETTINGS_KEY, settings, updated_by=updated_by)

    def get_watchlist(self) -> Optional[List[str]]:
        row = self.db.fetch_one(
            "SELECT config_value FROM scout_config WHERE config_key = ?",
            [self.WATCHLIST_KEY],
        )
        if not row or not row.get("config_value"):
            return None
        try:
            data = json.loads(row["config_value"])
            if isinstance(data, list):
                return [str(s).upper() for s in data if s]
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def set_watchlist(self, symbols: List[str], *, updated_by: str = "ui") -> None:
        cleaned = sorted({str(s).upper().strip() for s in symbols if s and str(s).strip()})
        self._merge_json(self.WATCHLIST_KEY, cleaned, updated_by=updated_by)


def _trade_pnl(action: str, entry: float, exit_px: float, qty: int) -> tuple[float, float]:
    qty = max(int(qty), 1)
    if str(action).upper() == "BUY":
        pnl = (exit_px - entry) * qty
    else:
        pnl = (entry - exit_px) * qty
    pct = (pnl / (entry * qty) * 100.0) if entry > 0 else 0.0
    return round(pnl, 4), round(pct, 4)


def _trade_net_pnl(
    action: str,
    entry: float,
    exit_px: float,
    qty: int,
) -> tuple[float, float, float]:
    """Return (gross_pnl, total_charges, net_pnl)."""
    from engine.equity_charges import estimate_equity_intraday_charges

    gross, _ = _trade_pnl(action, entry, exit_px, qty)
    charges = estimate_equity_intraday_charges(
        entry=float(entry), exit_px=float(exit_px), qty=max(int(qty), 1),
    ).total
    net = round(gross - charges, 4)
    return round(gross, 4), round(charges, 4), net


class ScoutTradeRepo:
    """Executed scout trades — same idea as options_trades (fills from Zerodha entered in UI)."""

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def mark_taken(
        self,
        *,
        signal_id: int,
        symbol: str,
        action: str,
        signal_type: str,
        entry_price: float,
        quantity: int,
        executed_at: datetime,
        notes: Optional[str] = None,
        status: str = "OPEN",
        execution_mode: str = "paper",
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO scout_trades "
            "(signal_id, symbol, action, signal_type, entry_price, quantity, "
            " executed_at, status, notes, execution_mode) "
            "OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                signal_id, symbol, action, signal_type, entry_price, quantity,
                executed_at, status, notes, execution_mode,
            ],
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0

    def mark_pending_entry(
        self,
        *,
        signal_id: int,
        symbol: str,
        action: str,
        signal_type: str,
        entry_price: float,
        quantity: int,
        executed_at: datetime,
        notes: Optional[str] = None,
        execution_mode: str = "zerodha",
    ) -> int:
        return self.mark_taken(
            signal_id=signal_id,
            symbol=symbol,
            action=action,
            signal_type=signal_type,
            entry_price=entry_price,
            quantity=quantity,
            executed_at=executed_at,
            notes=notes,
            status="PENDING_ENTRY",
            execution_mode=execution_mode,
        )

    def activate_from_fill(
        self,
        trade_id: int,
        *,
        entry_price: float,
        executed_at: Optional[datetime] = None,
    ) -> None:
        self.db.execute(
            "UPDATE scout_trades SET status='OPEN', entry_price=?, "
            "executed_at=COALESCE(?, executed_at) WHERE id=? AND status='PENDING_ENTRY'",
            [entry_price, executed_at, trade_id],
        )

    def update_effective_stop(self, trade_id: int, *, stop_price: float) -> None:
        self.db.execute(
            "UPDATE scout_trades SET effective_stop_price=? WHERE id=? AND status IN ('OPEN', 'UNPROTECTED')",
            [stop_price, trade_id],
        )

    def set_status(self, trade_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE scout_trades SET status=? WHERE id=?",
            [status, trade_id],
        )

    def mark_failed(self, trade_id: int, *, reason: str) -> None:
        self.db.execute(
            "UPDATE scout_trades SET status='FAILED', exit_reason=? WHERE id=? "
            "AND status IN ('PENDING_ENTRY', 'OPEN', 'UNPROTECTED')",
            [str(reason)[:256], trade_id],
        )

    def deployed_capital_inr(self) -> float:
        """Sum of entry notional for active trades (pending + open + unprotected)."""
        placeholders = ", ".join("?" for _ in ACTIVE_TRADE_STATUSES)
        row = self.db.fetch_one(
            "SELECT COALESCE(SUM(entry_price * quantity), 0) AS deployed "
            f"FROM scout_trades WHERE status IN ({placeholders})",
            list(ACTIVE_TRADE_STATUSES),
        )
        if not row or row.get("deployed") is None:
            return 0.0
        return float(row["deployed"])

    def unprotected_trades(self) -> List[dict]:
        rows = self.db.fetch_all(
            "SELECT id, signal_id, symbol, action, entry_price, quantity, "
            "       executed_at, status, execution_mode "
            "FROM scout_trades WHERE status = 'UNPROTECTED' ORDER BY executed_at DESC",
        )
        return [_row(r) for r in rows]

    def open_trades(self) -> List[dict]:
        placeholders = ", ".join("?" for _ in ACTIVE_TRADE_STATUSES)
        rows = self.db.fetch_all(
            "SELECT id, signal_id, symbol, action, signal_type, entry_price, quantity, "
            "       executed_at, status, notes, peak_price, execution_mode, effective_stop_price "
            f"FROM scout_trades WHERE status IN ({placeholders}) "
            "ORDER BY executed_at DESC",
            list(ACTIVE_TRADE_STATUSES),
        )
        return [_row(r) for r in rows]

    def pending_entry_trades(self) -> List[dict]:
        rows = self.db.fetch_all(
            "SELECT id, signal_id, symbol, action, signal_type, entry_price, quantity, "
            "       executed_at, status, notes, execution_mode "
            "FROM scout_trades WHERE status = 'PENDING_ENTRY' "
            "ORDER BY executed_at DESC",
        )
        return [_row(r) for r in rows]

    def update_peak_price(self, trade_id: int, *, peak_price: float) -> None:
        self.db.execute(
            "UPDATE scout_trades SET peak_price = ? WHERE id = ? AND status = 'OPEN'",
            [peak_price, trade_id],
        )

    def open_signal_ids(self) -> set[int]:
        placeholders = ", ".join("?" for _ in ACTIVE_TRADE_STATUSES)
        rows = self.db.fetch_all(
            f"SELECT signal_id FROM scout_trades WHERE status IN ({placeholders}) "
            "AND signal_id IS NOT NULL",
            list(ACTIVE_TRADE_STATUSES),
        )
        return {int(r["signal_id"]) for r in rows if r.get("signal_id") is not None}

    def count_trades_opened_today(self) -> int:
        from utils import today_ist

        row = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM scout_trades WHERE CONVERT(date, executed_at) = ?",
            [today_ist().isoformat()],
        )
        return int(row["n"]) if row and row.get("n") is not None else 0

    def symbol_has_trade_today(self, symbol: str) -> bool:
        from utils import today_ist

        row = self.db.fetch_one(
            "SELECT TOP 1 id FROM scout_trades WHERE symbol = ? AND CONVERT(date, executed_at) = ?",
            [str(symbol).upper(), today_ist().isoformat()],
        )
        return row is not None

    def get(self, trade_id: int) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT * FROM scout_trades WHERE id = ?",
            [trade_id],
        )
        return _row(row) if row else None

    def get_by_signal_id(self, signal_id: int) -> Optional[dict]:
        placeholders = ", ".join("?" for _ in ACTIVE_TRADE_STATUSES)
        row = self.db.fetch_one(
            f"SELECT TOP 1 * FROM scout_trades WHERE signal_id = ? "
            f"AND status IN ({placeholders}) ORDER BY id DESC",
            [signal_id, *ACTIVE_TRADE_STATUSES],
        )
        return _row(row) if row else None

    def close(
        self,
        trade_id: int,
        *,
        exit_price: float,
        closed_at: datetime,
        exit_reason: Optional[str] = None,
    ) -> Optional[dict]:
        trade = self.get(trade_id)
        closable = ("OPEN", "UNPROTECTED")
        if not trade or str(trade.get("status") or "") not in closable:
            return None
        pnl, pnl_pct = _trade_pnl(
            trade["action"],
            float(trade["entry_price"]),
            float(exit_price),
            int(trade.get("quantity") or 1),
        )
        gross, charges, net = _trade_net_pnl(
            trade["action"],
            float(trade["entry_price"]),
            float(exit_price),
            int(trade.get("quantity") or 1),
        )
        placeholders = ", ".join("?" for _ in closable)
        self.db.execute(
            "UPDATE scout_trades SET status='CLOSED', exit_price=?, closed_at=?, "
            "pnl=?, pnl_pct=?, gross_pnl=?, total_charges=?, net_pnl=?, exit_reason=? "
            f"WHERE id=? AND status IN ({placeholders})",
            [exit_price, closed_at, pnl, pnl_pct, gross, charges, net, exit_reason, trade_id, *closable],
        )
        return self.get(trade_id)

    def void(self, trade_id: int) -> bool:
        placeholders = ", ".join("?" for _ in ACTIVE_TRADE_STATUSES)
        cur = self.db.execute(
            f"DELETE FROM scout_trades WHERE id = ? AND status IN ({placeholders})",
            [trade_id, *ACTIVE_TRADE_STATUSES],
        )
        n = cur.rowcount
        cur.close()
        return n > 0

    def closed_trades(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        sql = (
            "SELECT TOP (?) t.*, s.reason AS signal_reason, s.strength AS signal_strength, "
            "       s.ltp AS signal_ltp, s.invalidation, s.triggered_at AS signal_triggered_at, "
            "       s.meta_json, s.action AS signal_action "
            "FROM scout_trades t "
            "LEFT JOIN scout_signals s ON s.id = t.signal_id "
            "WHERE t.status = 'CLOSED' "
        )
        params: List[Any] = [limit]
        if from_date:
            sql += " AND CONVERT(date, t.closed_at) >= ? "
            params.append(from_date)
        if to_date:
            sql += " AND CONVERT(date, t.closed_at) <= ? "
            params.append(to_date)
        if symbol:
            sql += " AND t.symbol = ? "
            params.append(symbol.upper())
        sql += " ORDER BY t.closed_at DESC"
        return [_row(r) for r in self.db.fetch_all(sql, params)]

    def performance_stats(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> dict:
        from engine.equity_charges import estimate_equity_intraday_charges

        sql = (
            "SELECT t.pnl, t.pnl_pct, t.gross_pnl, t.total_charges, t.net_pnl, "
            "       t.action, t.signal_type, t.symbol, t.notes, t.exit_reason, "
            "       t.entry_price, t.exit_price, t.quantity "
            "FROM scout_trades t WHERE t.status = 'CLOSED' "
        )
        params: List[Any] = []
        if from_date:
            sql += " AND CONVERT(date, t.closed_at) >= ? "
            params.append(from_date)
        if to_date:
            sql += " AND CONVERT(date, t.closed_at) <= ? "
            params.append(to_date)
        rows = self.db.fetch_all(sql, params)

        def _row_net(r: dict) -> float:
            if r.get("net_pnl") is not None:
                return float(r["net_pnl"])
            gross = r.get("gross_pnl")
            if gross is None:
                gross = r.get("pnl")
            if gross is None:
                return 0.0
            gross_f = float(gross)
            if r.get("total_charges") is not None:
                return gross_f - float(r["total_charges"])
            entry = float(r.get("entry_price") or 0)
            exit_px = float(r.get("exit_price") or 0)
            qty = max(int(r.get("quantity") or 1), 1)
            if entry > 0 and exit_px > 0:
                return gross_f - estimate_equity_intraday_charges(
                    entry=entry, exit_px=exit_px, qty=qty,
                ).total
            return gross_f

        total = len(rows)
        win_pnls: List[float] = []
        loss_pnls: List[float] = []
        gross_wins = gross_losses = net_wins = net_losses = flat = 0
        total_gross = total_net = total_charges = 0.0
        by_type: Dict[str, dict] = {}
        auto_entry = manual_entry = auto_exit = manual_exit = 0
        auto_pnl = manual_pnl = 0.0
        auto_net = manual_net = 0.0

        for r in rows:
            net = _row_net(r)
            gross = float(r.get("gross_pnl") if r.get("gross_pnl") is not None else (r.get("pnl") or 0))
            charges = float(r.get("total_charges") or max(0.0, gross - net))
            total_gross += gross
            total_net += net
            total_charges += charges

            if net > 0:
                net_wins += 1
                win_pnls.append(net)
            elif net < 0:
                net_losses += 1
                loss_pnls.append(net)
            else:
                flat += 1

            if gross > 0:
                gross_wins += 1
            elif gross < 0:
                gross_losses += 1

            st = str(r.get("signal_type") or "UNKNOWN")
            bucket = by_type.setdefault(
                st,
                {"count": 0, "wins": 0, "pnl": 0.0, "net_pnl": 0.0},
            )
            bucket["count"] += 1
            bucket["net_pnl"] += net
            bucket["pnl"] += gross
            if net > 0:
                bucket["wins"] += 1

            from scout.trade_audit import _exit_mode, _norm_exit_code, _parse_notes_audit

            notes_audit = _parse_notes_audit(r.get("notes"))
            if notes_audit and notes_audit.get("mode") == "auto":
                auto_entry += 1
                auto_pnl += gross
                auto_net += net
            elif str(r.get("notes") or "").strip().lower() in ("auto_execute", "auto_enter"):
                auto_entry += 1
                auto_pnl += gross
                auto_net += net
            else:
                manual_entry += 1
                manual_pnl += gross
                manual_net += net

            if _exit_mode(_norm_exit_code(r.get("exit_reason"))) == "auto":
                auto_exit += 1
            else:
                manual_exit += 1

        win_sum = sum(win_pnls)
        loss_sum = abs(sum(loss_pnls))
        profit_factor = round(win_sum / loss_sum, 2) if loss_sum > 0 else None

        return {
            "total_trades": total,
            "wins": net_wins,
            "losses": net_losses,
            "flat": flat,
            "win_rate_pct": round(net_wins / total * 100, 1) if total else 0.0,
            "gross_wins": gross_wins,
            "gross_losses": gross_losses,
            "gross_win_rate_pct": round(gross_wins / total * 100, 1) if total else 0.0,
            "total_pnl": round(total_gross, 2),
            "total_net_pnl": round(total_net, 2),
            "total_charges": round(total_charges, 2),
            "avg_pnl": round(total_gross / total, 2) if total else 0.0,
            "avg_net_pnl": round(total_net / total, 2) if total else 0.0,
            "avg_win": round(win_sum / len(win_pnls), 2) if win_pnls else 0.0,
            "avg_loss": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0,
            "profit_factor": profit_factor,
            "by_signal_type": by_type,
            "automation": {
                "auto_entry_count": auto_entry,
                "manual_entry_count": manual_entry,
                "auto_exit_count": auto_exit,
                "manual_exit_count": manual_exit,
                "auto_entry_pnl": round(auto_pnl, 2),
                "manual_entry_pnl": round(manual_pnl, 2),
                "auto_entry_net_pnl": round(auto_net, 2),
                "manual_entry_net_pnl": round(manual_net, 2),
            },
        }


class ScoutTradeOrderRepo:
    """Broker order legs for a scout trade (entry, stop, target, exit)."""

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(
        self,
        *,
        trade_id: int,
        step_num: int,
        leg: str,
        quantity: int,
        order_type: Optional[str] = None,
        transaction_type: Optional[str] = None,
        product: Optional[str] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        status: str = "PENDING",
        kite_order_id: Optional[str] = None,
        exchange_order_id: Optional[str] = None,
        status_message: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> int:
        placed_at = now_ist() if kite_order_id else None
        cur = self.db.execute(
            "INSERT INTO scout_trade_orders "
            "(trade_id, step_num, leg, kite_order_id, exchange_order_id, order_type, "
            " transaction_type, product, quantity, price, trigger_price, status, "
            " status_message, placed_at, meta_json) "
            "OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                trade_id, step_num, leg, kite_order_id, exchange_order_id,
                order_type, transaction_type, product, quantity, price,
                trigger_price, status, status_message, placed_at,
                json.dumps(meta) if meta else None,
            ],
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0

    def for_trade(self, trade_id: int) -> List[dict]:
        rows = self.db.fetch_all(
            "SELECT * FROM scout_trade_orders WHERE trade_id = ? "
            "ORDER BY step_num, id",
            [trade_id],
        )
        out = []
        for r in rows:
            row = _row(r)
            if row.get("meta_json"):
                try:
                    row["meta"] = json.loads(row["meta_json"])
                except json.JSONDecodeError:
                    row["meta"] = None
            else:
                row["meta"] = None
            out.append(row)
        return out

    def get_leg(self, trade_id: int, leg: str) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT TOP 1 * FROM scout_trade_orders "
            "WHERE trade_id = ? AND leg = ? ORDER BY id DESC",
            [trade_id, leg],
        )
        if not row:
            return None
        out = _row(row)
        if out.get("meta_json"):
            try:
                out["meta"] = json.loads(out["meta_json"])
            except json.JSONDecodeError:
                out["meta"] = None
        return out

    def leg_placed(self, trade_id: int, leg: str) -> bool:
        """True if a non-failed order row exists for this leg."""
        row = self.db.fetch_one(
            "SELECT TOP 1 id FROM scout_trade_orders "
            "WHERE trade_id = ? AND leg = ? "
            "AND status NOT IN ('FAILED', 'CANCELLED', 'REJECTED') "
            "ORDER BY id DESC",
            [trade_id, leg],
        )
        return row is not None

    def count_step_attempts(self, trade_id: int, *, step_num: int, leg: str) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM scout_trade_orders "
            "WHERE trade_id = ? AND step_num = ? AND leg = ?",
            [trade_id, step_num, leg],
        )
        if not row:
            return 0
        return int(row.get("n") or 0)

    def update_status(
        self,
        order_row_id: int,
        *,
        status: str,
        status_message: Optional[str] = None,
        kite_order_id: Optional[str] = None,
        exchange_order_id: Optional[str] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        filled_quantity: Optional[int] = None,
    ) -> None:
        sets = ["status = ?", "updated_at = SYSUTCDATETIME()"]
        params: List[Any] = [status]
        if status_message is not None:
            sets.append("status_message = ?")
            params.append(status_message)
        if kite_order_id is not None:
            sets.append("kite_order_id = ?")
            params.append(kite_order_id)
            sets.append("placed_at = COALESCE(placed_at, SYSUTCDATETIME())")
        if exchange_order_id is not None:
            sets.append("exchange_order_id = ?")
            params.append(exchange_order_id)
        if price is not None:
            sets.append("price = ?")
            params.append(price)
        if trigger_price is not None:
            sets.append("trigger_price = ?")
            params.append(trigger_price)
        if filled_quantity is not None:
            sets.append("filled_quantity = ?")
            params.append(int(filled_quantity))
        params.append(order_row_id)
        self.db.execute(
            f"UPDATE scout_trade_orders SET {', '.join(sets)} WHERE id = ?",
            params,
        )


class ScoutScanLogRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def start(self, scan_id: str, started_at: datetime) -> None:
        self.db.execute(
            "INSERT INTO scout_scan_log (scan_id, started_at, status, symbols_scanned, signals_found) "
            "VALUES (?, ?, 'RUNNING', 0, 0)",
            [scan_id, started_at],
        )

    def finish(
        self,
        scan_id: str,
        *,
        status: str,
        finished_at: datetime,
        symbols_scanned: int,
        signals_found: int,
        error_message: Optional[str] = None,
    ) -> None:
        self.db.execute(
            "UPDATE scout_scan_log SET finished_at=?, status=?, symbols_scanned=?, "
            "signals_found=?, error_message=? WHERE scan_id=?",
            [finished_at, status, symbols_scanned, signals_found, error_message, scan_id],
        )

    def last_success(self) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT TOP 1 scan_id, started_at, finished_at, symbols_scanned, signals_found "
            "FROM scout_scan_log WHERE status='SUCCESS' ORDER BY finished_at DESC",
        )
        return _row(row) if row else None


class ScoutZerodhaLogRepo:
    """Persistent Zerodha permission / connectivity log for Scout Errors UI."""

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(
        self,
        *,
        run_id: str,
        trigger_source: str,
        severity: str,
        code: str,
        message: str,
        detail: Optional[str] = None,
        user_id: Optional[str] = None,
        logged_at: Optional[datetime] = None,
    ) -> int:
        ts = logged_at or now_ist().replace(tzinfo=None)
        cur = self.db.execute(
            "INSERT INTO scout_zerodha_log "
            "(logged_at, run_id, trigger_source, severity, code, message, detail, user_id) "
            "OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [ts, run_id, trigger_source, severity, code, message, detail, user_id],
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0

    def fetch(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        severity: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        sql = (
            "SELECT id, logged_at, run_id, trigger_source, severity, code, message, detail, user_id "
            "FROM scout_zerodha_log WHERE 1=1 "
        )
        params: List[Any] = []
        if from_date:
            sql += " AND CONVERT(date, logged_at) >= ? "
            params.append(from_date)
        if to_date:
            sql += " AND CONVERT(date, logged_at) <= ? "
            params.append(to_date)
        if severity:
            sql += " AND severity = ? "
            params.append(severity.upper())
        if search:
            sql += " AND (message LIKE ? OR code LIKE ? OR detail LIKE ?) "
            pat = f"%{search}%"
            params.extend([pat, pat, pat])
        sql += " ORDER BY logged_at DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        params.extend([offset, limit])
        return [_row(r) for r in self.db.fetch_all(sql, params)]

    def count(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        severity: Optional[str] = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM scout_zerodha_log WHERE 1=1 "
        params: List[Any] = []
        if from_date:
            sql += " AND CONVERT(date, logged_at) >= ? "
            params.append(from_date)
        if to_date:
            sql += " AND CONVERT(date, logged_at) <= ? "
            params.append(to_date)
        if severity:
            sql += " AND severity = ? "
            params.append(severity.upper())
        row = self.db.fetch_one(sql, params)
        return int(row["n"]) if row else 0

    def latest_summary(self) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT TOP 1 id, logged_at, run_id, trigger_source, severity, code, message, detail, user_id "
            "FROM scout_zerodha_log WHERE code = 'check_summary' ORDER BY logged_at DESC",
        )
        return _row(row) if row else None

    def latest_summary_at(self) -> Optional[datetime]:
        row = self.db.fetch_one(
            "SELECT TOP 1 logged_at FROM scout_zerodha_log "
            "WHERE code = 'check_summary' ORDER BY logged_at DESC",
        )
        if not row:
            return None
        ts = row.get("logged_at")
        return ts if isinstance(ts, datetime) else None
