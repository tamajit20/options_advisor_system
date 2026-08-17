"""
database/arb_models.py — repositories for arb_* tables.
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


class ArbPairRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert_pair(
        self,
        *,
        symbol: str,
        nse_symbol: str,
        bse_symbol: str,
        isin: Optional[str],
        nse_token: int,
        bse_token: int,
        active: bool = True,
    ) -> None:
        self.db.execute(
            """
            MERGE arb_pairs AS T
            USING (SELECT ? AS symbol) AS S ON T.symbol = S.symbol
            WHEN MATCHED THEN UPDATE SET
                nse_symbol = ?, bse_symbol = ?, isin = ?,
                nse_token = ?, bse_token = ?, active = ?, updated_at = SYSDATETIME()
            WHEN NOT MATCHED THEN INSERT
                (symbol, nse_symbol, bse_symbol, isin, nse_token, bse_token, active)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            [
                symbol.upper(),
                nse_symbol, bse_symbol, isin, nse_token, bse_token, 1 if active else 0,
                symbol.upper(), nse_symbol, bse_symbol, isin, nse_token, bse_token,
                1 if active else 0,
            ],
        )

    def deactivate_missing(self, symbols: List[str]) -> None:
        if not symbols:
            cur = self.db.execute("UPDATE arb_pairs SET active = 0, updated_at = SYSDATETIME()")
            cur.close()
            return
        placeholders = ", ".join("?" for _ in symbols)
        cur = self.db.execute(
            f"UPDATE arb_pairs SET active = 0, updated_at = SYSDATETIME() "
            f"WHERE symbol NOT IN ({placeholders})",
            [s.upper() for s in symbols],
        )
        cur.close()

    def list_active(self) -> List[dict]:
        rows = self.db.fetch_all(
            "SELECT symbol, nse_symbol, bse_symbol, isin, nse_token, bse_token, active, updated_at "
            "FROM arb_pairs WHERE active = 1 ORDER BY symbol",
        )
        return [_row(r) for r in rows]

    def list_all(self) -> List[dict]:
        rows = self.db.fetch_all(
            "SELECT symbol, nse_symbol, bse_symbol, isin, nse_token, bse_token, active, updated_at "
            "FROM arb_pairs ORDER BY symbol",
        )
        return [_row(r) for r in rows]

    def count_active(self) -> int:
        row = self.db.fetch_one("SELECT COUNT(*) AS n FROM arb_pairs WHERE active = 1")
        return int(row["n"]) if row and row.get("n") is not None else 0


class ArbGapRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert_open(self, payload: dict) -> int:
        cur = self.db.execute(
            """
            INSERT INTO arb_gaps (
                symbol, isin, started_at, ended_at, duration_sec,
                nse_ltp, bse_ltp, gap_abs, gap_pct, direction,
                nse_bid, nse_ask, nse_bid_qty, nse_ask_qty,
                bse_bid, bse_ask, bse_bid_qty, bse_ask_qty,
                max_gap_pct, sample_count
            )
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                payload["symbol"],
                payload.get("isin"),
                payload["started_at"],
                payload.get("duration_sec", 0),
                payload.get("nse_ltp"),
                payload.get("bse_ltp"),
                payload.get("gap_abs"),
                payload.get("gap_pct"),
                payload.get("direction"),
                payload.get("nse_bid"),
                payload.get("nse_ask"),
                payload.get("nse_bid_qty"),
                payload.get("nse_ask_qty"),
                payload.get("bse_bid"),
                payload.get("bse_ask"),
                payload.get("bse_bid_qty"),
                payload.get("bse_ask_qty"),
                payload.get("max_gap_pct"),
                payload.get("sample_count", 1),
            ],
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0

    def update_open(self, gap_id: int, payload: dict) -> None:
        self.db.execute(
            """
            UPDATE arb_gaps SET
                duration_sec = ?, nse_ltp = ?, bse_ltp = ?,
                gap_abs = ?, gap_pct = ?, direction = ?,
                nse_bid = ?, nse_ask = ?, nse_bid_qty = ?, nse_ask_qty = ?,
                bse_bid = ?, bse_ask = ?, bse_bid_qty = ?, bse_ask_qty = ?,
                max_gap_pct = ?, sample_count = ?, updated_at = SYSDATETIME()
            WHERE id = ? AND ended_at IS NULL
            """,
            [
                payload.get("duration_sec"),
                payload.get("nse_ltp"),
                payload.get("bse_ltp"),
                payload.get("gap_abs"),
                payload.get("gap_pct"),
                payload.get("direction"),
                payload.get("nse_bid"),
                payload.get("nse_ask"),
                payload.get("nse_bid_qty"),
                payload.get("nse_ask_qty"),
                payload.get("bse_bid"),
                payload.get("bse_ask"),
                payload.get("bse_bid_qty"),
                payload.get("bse_ask_qty"),
                payload.get("max_gap_pct"),
                payload.get("sample_count"),
                gap_id,
            ],
        )

    def close(self, gap_id: int, *, ended_at: datetime, duration_sec: int, payload: dict) -> None:
        self.db.execute(
            """
            UPDATE arb_gaps SET
                ended_at = ?, duration_sec = ?,
                nse_ltp = ?, bse_ltp = ?, gap_abs = ?, gap_pct = ?, direction = ?,
                nse_bid = ?, nse_ask = ?, nse_bid_qty = ?, nse_ask_qty = ?,
                bse_bid = ?, bse_ask = ?, bse_bid_qty = ?, bse_ask_qty = ?,
                max_gap_pct = ?, sample_count = ?, updated_at = SYSDATETIME()
            WHERE id = ?
            """,
            [
                ended_at,
                duration_sec,
                payload.get("nse_ltp"),
                payload.get("bse_ltp"),
                payload.get("gap_abs"),
                payload.get("gap_pct"),
                payload.get("direction"),
                payload.get("nse_bid"),
                payload.get("nse_ask"),
                payload.get("nse_bid_qty"),
                payload.get("nse_ask_qty"),
                payload.get("bse_bid"),
                payload.get("bse_ask"),
                payload.get("bse_bid_qty"),
                payload.get("bse_ask_qty"),
                payload.get("max_gap_pct"),
                payload.get("sample_count"),
                gap_id,
            ],
        )

    def list_gaps(
        self,
        *,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
        symbol: Optional[str] = None,
        min_gap_pct: Optional[float] = None,
        min_duration_sec: Optional[int] = None,
        open_only: bool = False,
        limit: int = 200,
    ) -> List[dict]:
        sql = (
            "SELECT TOP (?) id, symbol, isin, started_at, ended_at, duration_sec, "
            "       nse_ltp, bse_ltp, gap_abs, gap_pct, direction, "
            "       nse_bid, nse_ask, nse_bid_qty, nse_ask_qty, "
            "       bse_bid, bse_ask, bse_bid_qty, bse_ask_qty, "
            "       max_gap_pct, sample_count "
            "FROM arb_gaps WHERE 1=1 "
        )
        params: List[Any] = [limit]
        if open_only:
            sql += " AND ended_at IS NULL "
        if from_dt:
            sql += " AND started_at >= ? "
            params.append(from_dt)
        if to_dt:
            sql += " AND started_at <= ? "
            params.append(to_dt)
        if symbol:
            sql += " AND symbol = ? "
            params.append(symbol.upper())
        if min_gap_pct is not None:
            sql += " AND ABS(gap_pct) >= ? "
            params.append(float(min_gap_pct))
        if min_duration_sec is not None:
            sql += " AND COALESCE(duration_sec, 0) >= ? "
            params.append(int(min_duration_sec))
        sql += " ORDER BY started_at DESC"
        return [_row(r) for r in self.db.fetch_all(sql, params)]

    def open_gaps(self) -> List[dict]:
        return self.list_gaps(open_only=True, limit=500)


class ArbConfigRepo:
    ENABLED_KEY = "enabled"
    UNIVERSE_KEY = "universe"
    SETTINGS_KEY = "settings"

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def _merge_json(self, key: str, payload: dict, *, updated_by: str = "ui") -> None:
        val = json.dumps(payload)
        self.db.execute(
            """
            MERGE arb_config AS T
            USING (SELECT ? AS config_key, ? AS config_value) AS S
            ON T.config_key = S.config_key
            WHEN MATCHED THEN UPDATE SET config_value = S.config_value,
                updated_at = SYSUTCDATETIME(), updated_by = ?
            WHEN NOT MATCHED THEN INSERT (config_key, config_value, updated_by)
                VALUES (S.config_key, S.config_value, ?);
            """,
            [key, val, updated_by, updated_by],
        )

    def get_json(self, key: str) -> Optional[Any]:
        row = self.db.fetch_one(
            "SELECT config_value FROM arb_config WHERE config_key = ?",
            [key],
        )
        if not row or not row.get("config_value"):
            return None
        try:
            return json.loads(row["config_value"])
        except (json.JSONDecodeError, TypeError):
            return row["config_value"]

    def set_json(self, key: str, value: Any, *, updated_by: str = "ui") -> None:
        val = json.dumps(value) if not isinstance(value, str) else value
        self.db.execute(
            """
            MERGE arb_config AS T
            USING (SELECT ? AS config_key, ? AS config_value) AS S
            ON T.config_key = S.config_key
            WHEN MATCHED THEN UPDATE SET config_value = S.config_value,
                updated_at = SYSUTCDATETIME(), updated_by = ?
            WHEN NOT MATCHED THEN INSERT (config_key, config_value, updated_by)
                VALUES (S.config_key, S.config_value, ?);
            """,
            [key, val, updated_by, updated_by],
        )

    def get_enabled(self, default: bool = True) -> bool:
        val = self.get_json(self.ENABLED_KEY)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("1", "true", "yes", "on")

    def get_universe(self, default: str = "nifty50_dual") -> str:
        val = self.get_json(self.UNIVERSE_KEY)
        return str(val) if val else default

    def get_settings(self) -> Optional[dict]:
        val = self.get_json(self.SETTINGS_KEY)
        return val if isinstance(val, dict) else None

    def set_settings(self, settings: dict, *, updated_by: str = "ui") -> None:
        self._merge_json(self.SETTINGS_KEY, settings, updated_by=updated_by)
