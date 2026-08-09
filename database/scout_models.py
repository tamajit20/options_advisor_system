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
