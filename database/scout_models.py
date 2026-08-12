"""
database/scout_models.py — repositories for scout_* tables only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database.connection import SQLServerConnection
from utils import now_ist


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
    ) -> None:
        self.db.execute(
            "INSERT INTO scout_signals "
            "(scan_id, symbol, exchange, action, signal_type, reason, ltp, "
            " invalidation, strength, triggered_at, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                scan_id, symbol, exchange, action, signal_type, reason, ltp,
                invalidation, strength, triggered_at,
                json.dumps(meta) if meta else None,
            ],
        )

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

    def __init__(self, db: SQLServerConnection):
        self.db = db

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
        val = json.dumps(cleaned)
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
            [self.WATCHLIST_KEY, val, updated_by, updated_by],
        )


def _trade_pnl(action: str, entry: float, exit_px: float, qty: int) -> tuple[float, float]:
    qty = max(int(qty), 1)
    if str(action).upper() == "BUY":
        pnl = (exit_px - entry) * qty
    else:
        pnl = (entry - exit_px) * qty
    pct = (pnl / (entry * qty) * 100.0) if entry > 0 else 0.0
    return round(pnl, 4), round(pct, 4)


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
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO scout_trades "
            "(signal_id, symbol, action, signal_type, entry_price, quantity, "
            " executed_at, status, notes) "
            "OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)",
            [
                signal_id, symbol, action, signal_type, entry_price, quantity,
                executed_at, notes,
            ],
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0

    def open_trades(self) -> List[dict]:
        rows = self.db.fetch_all(
            "SELECT id, signal_id, symbol, action, signal_type, entry_price, quantity, "
            "       executed_at, status, notes "
            "FROM scout_trades WHERE status = 'OPEN' "
            "ORDER BY executed_at DESC",
        )
        return [_row(r) for r in rows]

    def open_signal_ids(self) -> set[int]:
        rows = self.db.fetch_all(
            "SELECT signal_id FROM scout_trades WHERE status = 'OPEN' AND signal_id IS NOT NULL",
        )
        return {int(r["signal_id"]) for r in rows if r.get("signal_id") is not None}

    def get(self, trade_id: int) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT * FROM scout_trades WHERE id = ?",
            [trade_id],
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
        if not trade or trade.get("status") != "OPEN":
            return None
        pnl, pnl_pct = _trade_pnl(
            trade["action"],
            float(trade["entry_price"]),
            float(exit_price),
            int(trade.get("quantity") or 1),
        )
        self.db.execute(
            "UPDATE scout_trades SET status='CLOSED', exit_price=?, closed_at=?, "
            "pnl=?, pnl_pct=?, exit_reason=? WHERE id=? AND status='OPEN'",
            [exit_price, closed_at, pnl, pnl_pct, exit_reason, trade_id],
        )
        return self.get(trade_id)

    def void(self, trade_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM scout_trades WHERE id = ? AND status = 'OPEN'",
            [trade_id],
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
            "SELECT TOP (?) t.*, s.reason AS signal_reason, s.strength AS signal_strength "
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
        sql = (
            "SELECT t.pnl, t.pnl_pct, t.action, t.signal_type, t.symbol "
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
        total = len(rows)
        wins = sum(1 for r in rows if r.get("pnl") is not None and float(r["pnl"]) > 0)
        losses = sum(1 for r in rows if r.get("pnl") is not None and float(r["pnl"]) < 0)
        flat = total - wins - losses
        total_pnl = sum(float(r["pnl"] or 0) for r in rows)
        by_type: Dict[str, dict] = {}
        for r in rows:
            st = str(r.get("signal_type") or "UNKNOWN")
            bucket = by_type.setdefault(st, {"count": 0, "wins": 0, "pnl": 0.0})
            bucket["count"] += 1
            pnl = float(r.get("pnl") or 0)
            bucket["pnl"] += pnl
            if pnl > 0:
                bucket["wins"] += 1
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "flat": flat,
            "win_rate_pct": round(wins / total * 100, 1) if total else 0.0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / total, 2) if total else 0.0,
            "by_signal_type": by_type,
        }


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
