"""
database/models.py
==================

CRUD repositories for every business table.

Convention:
    * Each repo takes a `SQLServerConnection` (already connected) in its
      constructor. The CALLER is responsible for `commit()` / `rollback()`
      and `close()`.
    * Bulk inserts go through `executemany` with `fast_executemany`.
    * Idempotent upserts use SQL Server's `MERGE`.
    * Returns plain `dict`s for read methods (no ORM, no contracts here —
      contracts are produced by callers if needed).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _safe_float(value: Any) -> Optional[float]:
    """Coerce a numeric value into something SQL Server FLOAT will accept.

    SQL Server's `float`/`real` columns reject ``inf`` / ``-inf`` / ``NaN``
    with a TDS protocol error ("Parameter N (\"\"): not a valid instance of
    float"). Callers (e.g. unlimited-profit strategies that store
    ``float('inf')``) should not blow up the INSERT — store NULL instead.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f

from contracts import (
    FoBhavRow,
    SpotBhavRow,
    VixRow,
    FiiOiRow,
    Suggestion,
    SuggestionLeg,
    Notification,
    SimulationDayUpdate,
)
from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from utils import now_ist

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# F&O EOD bhav
# ---------------------------------------------------------------------------

class FoEodRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert_many(self, rows: Iterable[FoBhavRow]) -> int:
        items = list(rows)
        if not items:
            return 0
        # MERGE for idempotent upsert
        sql = """
        MERGE options_fo_eod AS T
        USING (SELECT ? AS trade_date, ? AS symbol, ? AS instrument, ? AS expiry_date,
                      ? AS strike, ? AS option_type,
                      ? AS open_price, ? AS high_price, ? AS low_price, ? AS close_price,
                      ? AS settle_price, ? AS contracts, ? AS open_interest, ? AS change_in_oi
              ) AS S
        ON  T.trade_date  = S.trade_date
        AND T.symbol      = S.symbol
        AND T.expiry_date = S.expiry_date
        AND T.strike      = S.strike
        AND T.option_type = S.option_type
        WHEN MATCHED THEN UPDATE SET
            instrument    = S.instrument,
            open_price    = S.open_price,
            high_price    = S.high_price,
            low_price     = S.low_price,
            close_price   = S.close_price,
            settle_price  = S.settle_price,
            contracts     = S.contracts,
            open_interest = S.open_interest,
            change_in_oi  = S.change_in_oi
        WHEN NOT MATCHED THEN INSERT
            (trade_date, symbol, instrument, expiry_date, strike, option_type,
             open_price, high_price, low_price, close_price, settle_price,
             contracts, open_interest, change_in_oi)
            VALUES (S.trade_date, S.symbol, S.instrument, S.expiry_date, S.strike, S.option_type,
                    S.open_price, S.high_price, S.low_price, S.close_price, S.settle_price,
                    S.contracts, S.open_interest, S.change_in_oi);
        """
        params = [
            (
                r.trade_date, r.symbol, r.instrument, r.expiry_date, r.strike, r.option_type,
                r.open_price, r.high_price, r.low_price, r.close_price, r.settle_price,
                r.contracts, r.open_interest, r.change_in_oi,
            )
            for r in items
        ]
        self.db.executemany(sql, params)
        return len(items)

    def get_chain(self, symbol: str, trade_date: date, expiry: date) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_fo_eod "
            "WHERE symbol = ? AND trade_date = ? AND expiry_date = ? "
            "ORDER BY strike, option_type",
            [symbol, trade_date, expiry],
        )

    def get_strikes(self, symbol: str, trade_date: date, expiry: date) -> List[float]:
        rows = self.db.fetch_all(
            "SELECT DISTINCT strike FROM options_fo_eod "
            "WHERE symbol = ? AND trade_date = ? AND expiry_date = ? ORDER BY strike",
            [symbol, trade_date, expiry],
        )
        return [float(r["strike"]) for r in rows]

    def get_one(
        self,
        symbol: str,
        trade_date: date,
        expiry: date,
        strike: float,
        option_type: str,
    ) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM options_fo_eod "
            "WHERE symbol = ? AND trade_date = ? AND expiry_date = ? "
            "AND strike = ? AND option_type = ?",
            [symbol, trade_date, expiry, strike, option_type],
        )

    def latest_trade_date(self) -> Optional[date]:
        v = self.db.scalar("SELECT MAX(trade_date) FROM options_fo_eod")
        return v

    def expiries_for(self, symbol: str, trade_date: date) -> List[date]:
        rows = self.db.fetch_all(
            "SELECT DISTINCT expiry_date FROM options_fo_eod "
            "WHERE symbol = ? AND trade_date = ? AND expiry_date >= ? "
            "ORDER BY expiry_date",
            [symbol, trade_date, trade_date],
        )
        return [r["expiry_date"] for r in rows]

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute("DELETE FROM options_fo_eod WHERE trade_date < ?", [cutoff])
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# Spot EOD
# ---------------------------------------------------------------------------

class SpotEodRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert_many(self, rows: Iterable[SpotBhavRow]) -> int:
        items = list(rows)
        if not items:
            return 0
        sql = """
        MERGE options_spot_eod AS T
        USING (SELECT ? AS trade_date, ? AS symbol,
                      ? AS open_price, ? AS high_price, ? AS low_price,
                      ? AS close_price, ? AS volume) AS S
        ON T.trade_date = S.trade_date AND T.symbol = S.symbol
        WHEN MATCHED THEN UPDATE SET
            open_price = S.open_price, high_price = S.high_price,
            low_price  = S.low_price,  close_price = S.close_price,
            volume     = S.volume
        WHEN NOT MATCHED THEN INSERT
            (trade_date, symbol, open_price, high_price, low_price, close_price, volume)
            VALUES (S.trade_date, S.symbol, S.open_price, S.high_price, S.low_price,
                    S.close_price, S.volume);
        """
        self.db.executemany(sql, [
            (r.trade_date, r.symbol, r.open_price, r.high_price, r.low_price, r.close_price, r.volume)
            for r in items
        ])
        return len(items)

    def latest(self, symbol: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT TOP 1 * FROM options_spot_eod WHERE symbol = ? ORDER BY trade_date DESC",
            [symbol],
        )

    def for_date(self, symbol: str, trade_date: date) -> Optional[dict]:
        """Return spot row for the exact trade_date, falling back to the most
        recent row on or before that date (never a future date)."""
        row = self.db.fetch_one(
            "SELECT TOP 1 * FROM options_spot_eod "
            "WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date DESC",
            [symbol, trade_date],
        )
        return row

    def history(self, symbol: str, since: date) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_spot_eod WHERE symbol = ? AND trade_date >= ? "
            "ORDER BY trade_date",
            [symbol, since],
        )

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute("DELETE FROM options_spot_eod WHERE trade_date < ?", [cutoff])
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# VIX
# ---------------------------------------------------------------------------

class VixRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert_many(self, rows: Iterable[VixRow]) -> int:
        items = list(rows)
        if not items:
            return 0
        sql = """
        MERGE options_vix_history AS T
        USING (SELECT ? AS trade_date, ? AS open_price, ? AS high_price,
                      ? AS low_price, ? AS close_price) AS S
        ON T.trade_date = S.trade_date
        WHEN MATCHED THEN UPDATE SET
            open_price=S.open_price, high_price=S.high_price,
            low_price=S.low_price,  close_price=S.close_price
        WHEN NOT MATCHED THEN INSERT (trade_date, open_price, high_price, low_price, close_price)
            VALUES (S.trade_date, S.open_price, S.high_price, S.low_price, S.close_price);
        """
        self.db.executemany(sql, [
            (r.trade_date, r.open_price, r.high_price, r.low_price, r.close_price) for r in items
        ])
        return len(items)

    def latest(self) -> Optional[dict]:
        return self.db.fetch_one("SELECT TOP 1 * FROM options_vix_history ORDER BY trade_date DESC")

    def count(self) -> int:
        row = self.db.fetch_one("SELECT COUNT(*) AS n FROM options_vix_history")
        return int(row["n"]) if row else 0

    def history(self, since: date) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_vix_history WHERE trade_date >= ? ORDER BY trade_date",
            [since],
        )

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute("DELETE FROM options_vix_history WHERE trade_date < ?", [cutoff])
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# FII data
# ---------------------------------------------------------------------------

class FiiRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert_many(self, rows: Iterable[FiiOiRow]) -> int:
        items = list(rows)
        if not items:
            return 0
        sql = """
        MERGE options_fii_data AS T
        USING (SELECT ? AS trade_date, ? AS client_type,
                      ? AS future_long, ? AS future_short,
                      ? AS option_call_long, ? AS option_call_short,
                      ? AS option_put_long, ? AS option_put_short) AS S
        ON T.trade_date = S.trade_date AND T.client_type = S.client_type
        WHEN MATCHED THEN UPDATE SET
            future_long=S.future_long, future_short=S.future_short,
            option_call_long=S.option_call_long, option_call_short=S.option_call_short,
            option_put_long=S.option_put_long,   option_put_short=S.option_put_short
        WHEN NOT MATCHED THEN INSERT
            (trade_date, client_type, future_long, future_short,
             option_call_long, option_call_short, option_put_long, option_put_short)
            VALUES (S.trade_date, S.client_type, S.future_long, S.future_short,
                    S.option_call_long, S.option_call_short, S.option_put_long, S.option_put_short);
        """
        self.db.executemany(sql, [
            (r.trade_date, r.client_type, r.future_long, r.future_short,
             r.option_call_long, r.option_call_short, r.option_put_long, r.option_put_short)
            for r in items
        ])
        return len(items)

    def latest(self) -> List[dict]:
        last_dt = self.db.scalar("SELECT MAX(trade_date) FROM options_fii_data")
        if last_dt is None:
            return []
        return self.db.fetch_all(
            "SELECT * FROM options_fii_data WHERE trade_date = ?", [last_dt]
        )

    def for_date(self, trade_date: date) -> List[dict]:
        """Return FII rows for the most recent date on or before trade_date.
        Prevents using future-dated FII data when running a backdated suggestion."""
        last_dt = self.db.scalar(
            "SELECT MAX(trade_date) FROM options_fii_data WHERE trade_date <= ?",
            [trade_date],
        )
        if last_dt is None:
            return []
        return self.db.fetch_all(
            "SELECT * FROM options_fii_data WHERE trade_date = ?", [last_dt]
        )

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute("DELETE FROM options_fii_data WHERE trade_date < ?", [cutoff])
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# IV history
# ---------------------------------------------------------------------------

class IvHistoryRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert_many(self, rows: Iterable[Dict[str, Any]]) -> int:
        items = list(rows)
        if not items:
            return 0
        sql = """
        MERGE options_iv_history AS T
        USING (SELECT ? AS trade_date, ? AS symbol, ? AS expiry_date,
                      ? AS strike, ? AS option_type,
                      ? AS spot, ? AS market_price,
                      ? AS iv, ? AS converged,
                      ? AS atm_iv, ? AS iv_rank, ? AS iv_percentile) AS S
        ON T.trade_date  = S.trade_date  AND T.symbol = S.symbol
        AND T.expiry_date = S.expiry_date AND T.strike = S.strike
        AND T.option_type = S.option_type
        WHEN MATCHED THEN UPDATE SET
            spot=S.spot, market_price=S.market_price, iv=S.iv, converged=S.converged,
            atm_iv=S.atm_iv, iv_rank=S.iv_rank, iv_percentile=S.iv_percentile
        WHEN NOT MATCHED THEN INSERT
            (trade_date, symbol, expiry_date, strike, option_type,
             spot, market_price, iv, converged, atm_iv, iv_rank, iv_percentile)
            VALUES (S.trade_date, S.symbol, S.expiry_date, S.strike, S.option_type,
                    S.spot, S.market_price, S.iv, S.converged,
                    S.atm_iv, S.iv_rank, S.iv_percentile);
        """
        self.db.executemany(sql, [
            (
                r["trade_date"], r["symbol"], r["expiry_date"], r["strike"], r["option_type"],
                r.get("spot"), r.get("market_price"), r.get("iv"),
                1 if r.get("converged") else 0,
                r.get("atm_iv"), r.get("iv_rank"), r.get("iv_percentile"),
            )
            for r in items
        ])
        return len(items)

    def atm_iv_history(self, symbol: str, since: date) -> List[dict]:
        # AVG collapses multiple expiries on the same trade_date into a single
        # representative value, preventing duplicate-date bias in IV Rank calc.
        return self.db.fetch_all(
            "SELECT trade_date, AVG(atm_iv) AS atm_iv FROM options_iv_history "
            "WHERE symbol = ? AND atm_iv IS NOT NULL AND trade_date >= ? "
            "GROUP BY trade_date ORDER BY trade_date",
            [symbol, since],
        )

    def latest_trade_date(self) -> Optional[date]:
        v = self.db.scalar("SELECT MAX(trade_date) FROM options_iv_history")
        return v

    def latest_for(self, symbol: str, trade_date: date) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_iv_history WHERE symbol = ? AND trade_date = ?",
            [symbol, trade_date],
        )

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute("DELETE FROM options_iv_history WHERE trade_date < ?", [cutoff])
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class LotSizeRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert(self, symbol: str, effective_from: date, lot_size: int) -> None:
        sql = """
        MERGE options_lot_sizes AS T
        USING (SELECT ? AS symbol, ? AS effective_from, ? AS lot_size) AS S
        ON T.symbol = S.symbol AND T.effective_from = S.effective_from
        WHEN MATCHED THEN UPDATE SET lot_size = S.lot_size
        WHEN NOT MATCHED THEN INSERT (symbol, effective_from, lot_size)
            VALUES (S.symbol, S.effective_from, S.lot_size);
        """
        self.db.execute(sql, [symbol, effective_from, lot_size]).close()

    def for_symbol(self, symbol: str, on_date: date) -> Optional[int]:
        row = self.db.fetch_one(
            "SELECT TOP 1 lot_size FROM options_lot_sizes "
            "WHERE symbol = ? AND effective_from <= ? "
            "ORDER BY effective_from DESC",
            [symbol, on_date],
        )
        return row["lot_size"] if row else None


class ExpiryCalendarRepo:
    """Maintains options_expiry_calendar from observed F&O bhav expiries.

    `is_monthly` is recomputed per (symbol, year, month) so the latest
    expiry of each calendar month is flagged 1, others 0. Safe to call
    repeatedly — uses MERGE + UPDATE.
    """

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def upsert_from_fo_rows(self, rows: Iterable[FoBhavRow]) -> int:
        # Limit to strategy underlyings to keep table small and relevant.
        targets = set(STRATEGY_CONFIG["underlyings"])
        pairs = {(r.symbol, r.expiry_date) for r in rows if r.symbol in targets}
        if not pairs:
            return 0

        merge_sql = """
        MERGE options_expiry_calendar AS T
        USING (SELECT ? AS symbol, ? AS expiry_date) AS S
        ON T.symbol = S.symbol AND T.expiry_date = S.expiry_date
        WHEN NOT MATCHED THEN INSERT (symbol, expiry_date, is_monthly)
            VALUES (S.symbol, S.expiry_date, 0);
        """
        self.db.executemany(merge_sql, [(s, d) for s, d in pairs])

        # Recompute is_monthly for the affected (symbol, year, month) windows.
        symbols = sorted({s for s, _ in pairs})
        placeholders = ",".join("?" for _ in symbols)
        recompute_sql = f"""
        WITH ranked AS (
            SELECT symbol, expiry_date,
                   CASE WHEN expiry_date = MAX(expiry_date) OVER (
                            PARTITION BY symbol, YEAR(expiry_date), MONTH(expiry_date))
                        THEN 1 ELSE 0 END AS new_flag
            FROM options_expiry_calendar
            WHERE symbol IN ({placeholders})
        )
        UPDATE c SET c.is_monthly = r.new_flag
        FROM options_expiry_calendar c
        JOIN ranked r ON r.symbol = c.symbol AND r.expiry_date = c.expiry_date
        WHERE c.is_monthly <> r.new_flag;
        """
        self.db.execute(recompute_sql, symbols).close()
        return len(pairs)


class EventCalendarRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def add(self, event_date: date, event_type: str, description: str, impact: str = "MEDIUM") -> None:
        self.db.execute(
            "INSERT INTO options_events_calendar (event_date, event_type, description, impact) "
            "VALUES (?, ?, ?, ?)",
            [event_date, event_type, description, impact],
        ).close()

    def in_range(self, start: date, end: date) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_events_calendar WHERE event_date BETWEEN ? AND ? "
            "ORDER BY event_date",
            [start, end],
        )

    def has_high_impact(self, start: date, end: date) -> bool:
        v = self.db.scalar(
            "SELECT COUNT(*) FROM options_events_calendar "
            "WHERE event_date BETWEEN ? AND ? AND impact = 'HIGH'",
            [start, end],
        )
        return bool(v and v > 0)

    def first_high_impact_event(self, start: date, end: date) -> Optional[dict]:
        """Return the first HIGH-impact event in the range, or None."""
        return self.db.fetch_one(
            "SELECT TOP 1 event_date, event_type, description FROM options_events_calendar "
            "WHERE event_date BETWEEN ? AND ? AND impact = 'HIGH' ORDER BY event_date",
            [start, end],
        )

    def count_all(self) -> int:
        """Total rows in the calendar table — 0 means it has never been seeded."""
        n = self.db.scalar("SELECT COUNT(*) FROM options_events_calendar")
        return int(n) if n is not None else 0


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

class SuggestionRepo:
    # Class-level counter cache so all SuggestionRepo instances using the
    # same DB connection share state. _evaluate_underlying creates a fresh
    # repo per symbol, but persistence is batched at the end of the run —
    # so a per-instance cache would let every symbol's first ID collide at
    # 001 (MAX returns the same pre-batch value for all of them).
    # Key: (id(db), date) — different DB connections (e.g. test mocks) get
    # independent caches and don't pollute each other.
    _next_seq_cache: dict[tuple[int, date], int] = {}

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(self, s: Suggestion) -> None:
        self.db.execute(
            """
            INSERT INTO options_suggestions
              (suggestion_id, trade_name, generated_on, strategy, strategy_type,
               underlying, expiry_date, expiry_type, dte, spot_at_generation, confidence_score,
               conditions_json, status,
               net_credit_suggested, max_profit, max_loss,
               upper_breakeven, lower_breakeven, stop_loss_level,
               probability_of_profit, estimated_charges_total, estimated_net_pnl,
               execution_window, plain_english,
               data_date, entry_date, spot_data_date, fii_data_date, vix_data_date,
               oi_pcr_change, edge_score, credit_grade)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING',
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                s.suggestion_id, s.trade_name, s.generated_on, s.strategy, s.strategy_type,
                s.underlying, s.expiry_date, s.expiry_type, s.dte, _safe_float(s.spot_at_generation), s.confidence.score,
                json.dumps([c.__dict__ for c in s.confidence.checks]),
                _safe_float(s.economics.net_credit), _safe_float(s.economics.max_profit), _safe_float(s.economics.max_loss),
                _safe_float(s.economics.upper_breakeven), _safe_float(s.economics.lower_breakeven), _safe_float(s.economics.stop_loss_level),
                _safe_float(s.economics.probability_of_profit), _safe_float(s.economics.estimated_charges.total), _safe_float(s.economics.estimated_net_pnl),
                s.execution_window, s.plain_english,
                s.data_date, s.entry_date, s.spot_data_date, s.fii_data_date, s.vix_data_date,
                _safe_float(s.oi_pcr_change),
                _safe_float(getattr(s.economics, "edge_score", None)),
                getattr(s.economics, "credit_grade", None),
            ],
        ).close()

    def insert_legs(self, suggestion_id: str, legs: Sequence[SuggestionLeg]) -> None:
        sql = (
            "INSERT INTO options_suggestion_legs "
            "(suggestion_id, leg_order, hedge_pair_leg, symbol, expiry_date, strike, "
            " option_type, action, lots, lot_size, "
            " suggested_price, suggested_price_low, suggested_price_high, leg_purpose_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        self.db.executemany(sql, [
            (
                suggestion_id, leg.leg_order, leg.hedge_pair_leg, leg.symbol, leg.expiry_date,
                _safe_float(leg.strike), leg.option_type, leg.action, leg.lots, leg.lot_size,
                _safe_float(leg.suggested_price), _safe_float(leg.suggested_price_low), _safe_float(leg.suggested_price_high),
                leg.leg_purpose_note,
            )
            for leg in legs
        ])

    def insert_no_suggestion(
        self,
        suggestion_id: str,
        underlying: str,
        generated_on: datetime,
        confidence_score: int,
        conditions_json: str,
        reason: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO options_suggestions
              (suggestion_id, generated_on, strategy, strategy_type, underlying,
               confidence_score, conditions_json, status, no_suggestion_reason)
            VALUES (?, ?, 'NONE', 'NONE', ?, ?, ?, 'NO_SUGGESTION', ?)
            """,
            [suggestion_id, generated_on, underlying, confidence_score, conditions_json, reason],
        ).close()

    def next_suggestion_id(self, today: date) -> str:
        # Use MAX(seq), not COUNT(*) — when an old suggestion is deleted the
        # row count drops and we'd hand out an ID that already exists,
        # producing a PK violation on insert. Parse the trailing 3-digit
        # sequence and bump it.
        #
        # Per-instance cache: once the first call has queried MAX, subsequent
        # calls increment the cached value locally. This prevents collisions
        # when several IDs are issued before any INSERT is committed (e.g.
        # primary + companion suggestions built in one engine run).
        prefix = f"SUG-{today.strftime('%Y%m%d')}-"
        key = (id(self.db), today)
        cached = self._next_seq_cache.get(key)
        if cached is None:
            row = self.db.fetch_one(
                "SELECT MAX(suggestion_id) AS m FROM options_suggestions "
                "WHERE suggestion_id LIKE ?",
                [prefix + "%"],
            )
            last = (row or {}).get("m")
            try:
                n = (int(str(last).rsplit("-", 1)[-1]) if last else 0) + 1
            except (ValueError, AttributeError):
                n = 1
        else:
            n = cached + 1
        self._next_seq_cache[key] = n
        return f"{prefix}{n:03d}"
    def has_suggestion_for(self, underlying: str, day: date, expiry_type: str = "",
                            strategy: str = "", entry_date: "Optional[date]" = None) -> bool:
        """Return True if a PENDING/real suggestion already exists for this
        underlying + expiry_type + strategy WITH THE SAME entry_date.

        When entry_date is provided, the check is scoped to that exact execution
        date — so running the engine on Saturday for Monday entry does NOT block
        even if a Friday-entry suggestion already exists for the same data date.
        When entry_date is None (legacy) we fall back to generated_on date-range.
        """
        if entry_date is not None:
            base = (
                "SELECT COUNT(*) FROM options_suggestions "
                "WHERE underlying = ? "
                "AND strategy <> 'NONE' AND status <> 'NO_SUGGESTION' "
                "AND entry_date = ?"
            )
            params: list = [underlying, entry_date]
            if expiry_type and strategy:
                base += " AND expiry_type = ? AND strategy = ?"
                params += [expiry_type, strategy]
            elif expiry_type:
                base += " AND expiry_type = ?"
                params += [expiry_type]
            v = self.db.scalar(base, params)
        else:
            start = datetime.combine(day, datetime.min.time())
            end   = start + timedelta(days=1)
            if expiry_type and strategy:
                v = self.db.scalar(
                    "SELECT COUNT(*) FROM options_suggestions "
                    "WHERE underlying = ? AND generated_on >= ? AND generated_on < ? "
                    "AND strategy <> 'NONE' AND status <> 'NO_SUGGESTION' "
                    "AND expiry_type = ? AND strategy = ?",
                    [underlying, start, end, expiry_type, strategy],
                )
            elif expiry_type:
                v = self.db.scalar(
                    "SELECT COUNT(*) FROM options_suggestions "
                    "WHERE underlying = ? AND generated_on >= ? AND generated_on < ? "
                    "AND strategy <> 'NONE' AND status <> 'NO_SUGGESTION' "
                    "AND expiry_type = ?",
                    [underlying, start, end, expiry_type],
                )
            else:
                v = self.db.scalar(
                    "SELECT COUNT(*) FROM options_suggestions "
                    "WHERE underlying = ? AND generated_on >= ? AND generated_on < ? "
                    "AND strategy <> 'NONE' AND status <> 'NO_SUGGESTION'",
                    [underlying, start, end],
                )
        return bool(v and v > 0)

    def expire_stale_pending(self, underlying: str, expiry_type: str,
                             strategy: str, new_entry_date: date) -> int:
        """Mark PENDING suggestions for the same underlying+expiry_type+strategy
        as IGNORED when their entry_date is older than new_entry_date (or NULL).

        Called before inserting a fresh suggestion so the old stale one is
        hidden from the dashboard immediately rather than lingering.
        Returns number of rows updated.
        """
        cur = self.db.execute(
            "UPDATE options_suggestions SET status = 'IGNORED' "
            "WHERE underlying = ? AND expiry_type = ? AND strategy = ? "
            "AND status = 'PENDING' "
            "AND (entry_date IS NULL OR entry_date < ?)",
            [underlying, expiry_type, strategy, new_entry_date],
        )
        n = cur.rowcount or 0
        cur.close()
        return n

    def get(self, suggestion_id: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM options_suggestions WHERE suggestion_id = ?", [suggestion_id]
        )

    def legs(self, suggestion_id: str) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_suggestion_legs WHERE suggestion_id = ? ORDER BY leg_order",
            [suggestion_id],
        )

    def update_status(self, suggestion_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE options_suggestions SET status = ? WHERE suggestion_id = ?",
            [status, suggestion_id],
        ).close()

    def write_provenance(
        self,
        suggestion_id: str,
        *,
        data_source: Optional[str] = None,
        provider: Optional[str] = None,
        data_as_of: Optional[datetime] = None,
        trigger_type: Optional[str] = None,
        trigger_reason: Optional[str] = None,
        market_state_at_gen: Optional[str] = None,
        live_data_freshness_ms: Optional[int] = None,
        engine_version: Optional[str] = None,
    ) -> None:
        """Phase 2c: stamp provenance columns after insert.

        Best-effort. Migration may not have run on legacy DBs \u2014 callers
        should swallow exceptions and log. We update only non-None fields
        so partial provenance (e.g. WS regen knows trigger_type but not
        live_data_freshness_ms) doesn't NULL-out previously written values.
        """
        sets: list[str] = []
        params: list = []
        for col, val in [
            ("data_source",            data_source),
            ("provider",               provider),
            ("data_as_of",             data_as_of),
            ("trigger_type",           trigger_type),
            ("trigger_reason",         trigger_reason),
            ("market_state_at_gen",    market_state_at_gen),
            ("live_data_freshness_ms", live_data_freshness_ms),
            ("engine_version",         engine_version),
        ]:
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if not sets:
            return
        params.append(suggestion_id)
        self.db.execute(
            f"UPDATE options_suggestions SET {', '.join(sets)} "
            "WHERE suggestion_id = ?",
            params,
        ).close()

    def by_date(self, day: date) -> List[dict]:
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        return self.db.fetch_all(
            "SELECT * FROM options_suggestions "
            "WHERE generated_on >= ? AND generated_on < ? "
            "ORDER BY generated_on DESC",
            [start, end],
        )

    def latest_pending(self, limit: int = 10) -> List[dict]:
        return self.db.fetch_all(
            "SELECT TOP (?) * FROM options_suggestions WHERE status = 'PENDING' "
            "ORDER BY generated_on DESC",
            [limit],
        )

    def open_or_pending(self) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_suggestions "
            "WHERE status IN ('PENDING', 'IGNORED') "
            "ORDER BY generated_on DESC"
        )

    def active_pending(self) -> List[dict]:
        """Return suggestions that are still relevant on the Suggestions page.

        Display rules by status:
          PENDING  — shown while the execution window is open:
                     entry_date >= today (before 09:45) or entry_date > today (after 09:45)
          EXECUTED — always shown for entry_date >= today so the user can see
                     what trade was placed, regardless of the time of day.
          IGNORED  — never shown (superseded; a fresh PENDING row exists instead).

        Fallback: legacy rows without entry_date are shown for up to 1 calendar day.
        """
        from utils import now_ist
        now = now_ist()
        today = now.date()
        from datetime import time as _time
        exec_window_close = _time(9, 45)

        if now.time() <= exec_window_close:
            # Within execution window: PENDING and EXECUTED for today and future
            rows = self.db.fetch_all(
                "SELECT * FROM options_suggestions "
                "WHERE status IN ('PENDING', 'EXECUTED') "
                "AND entry_date >= ? "
                "ORDER BY generated_on DESC",
                [today],
            )
        else:
            # After execution window: PENDING only for future days,
            # but EXECUTED still visible for today (already acted on) and future.
            rows = self.db.fetch_all(
                "SELECT * FROM options_suggestions "
                "WHERE (status = 'PENDING' AND entry_date > ?) "
                "   OR (status = 'EXECUTED' AND entry_date >= ?) "
                "ORDER BY generated_on DESC",
                [today, today],
            )

        # Fallback for legacy rows without entry_date: only show if generated
        # within the last 1 calendar day — anything older is definitively stale
        # (we can't tell the entry day, so we err on the side of hiding).
        if not rows:
            rows = self.db.fetch_all(
                "SELECT TOP (5) * FROM options_suggestions "
                "WHERE status IN ('PENDING', 'IGNORED') "
                "AND entry_date IS NULL "
                "AND generated_on >= DATEADD(day, -1, SYSDATETIME()) "
                "ORDER BY generated_on DESC"
            )
        return rows

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute(
            "DELETE FROM options_suggestion_legs WHERE suggestion_id IN "
            "(SELECT suggestion_id FROM options_suggestions WHERE generated_on < ?)",
            [datetime.combine(cutoff, datetime.min.time())],
        )
        cur.close()
        cur = self.db.execute(
            "DELETE FROM options_suggestions WHERE generated_on < ?",
            [datetime.combine(cutoff, datetime.min.time())],
        )
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

class TradeRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(self, trade: Dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO options_trades
              (trade_id, suggestion_id, trade_name, executed_on, position_type,
               net_credit_actual, actual_max_profit, actual_max_loss,
               actual_upper_breakeven, actual_lower_breakeven, actual_stop_loss_level,
               spot_at_execution,
               status, daily_status, exit_instruction, broken_state_json,
               gross_pnl, total_charges, net_pnl, closed_on)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trade["trade_id"], trade["suggestion_id"], trade.get("trade_name"),
                trade["executed_on"], trade["position_type"],
                trade.get("net_credit_actual"), trade.get("actual_max_profit"),
                trade.get("actual_max_loss"), trade.get("actual_upper_breakeven"),
                trade.get("actual_lower_breakeven"), trade.get("actual_stop_loss_level"),
                trade.get("spot_at_execution"),
                trade.get("status", "ACTIVE"), trade.get("daily_status"),
                trade.get("exit_instruction"), trade.get("broken_state_json"),
                trade.get("gross_pnl"), trade.get("total_charges"), trade.get("net_pnl"),
                trade.get("closed_on"),
            ],
        ).close()

    def insert_legs(self, trade_id: str, legs: Iterable[Dict[str, Any]]) -> None:
        sql = (
            "INSERT INTO options_trade_legs "
            "(trade_id, suggestion_leg_id, leg_order, executed, fill_price, fill_time, "
            " not_filled_reason, exit_price, exit_time, leg_pnl, leg_charges, lots_actual) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        self.db.executemany(sql, [
            (
                trade_id, l["suggestion_leg_id"], l["leg_order"], 1 if l.get("executed") else 0,
                l.get("fill_price"), l.get("fill_time"), l.get("not_filled_reason"),
                l.get("exit_price"), l.get("exit_time"), l.get("leg_pnl"), l.get("leg_charges"),
                l.get("lots_actual"),
            )
            for l in legs
        ])

    def get(self, trade_id: str) -> Optional[dict]:
        return self.db.fetch_one("SELECT * FROM options_trades WHERE trade_id = ?", [trade_id])

    def legs(self, trade_id: str) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_trade_legs WHERE trade_id = ? ORDER BY leg_order",
            [trade_id],
        )

    def open_trades(self) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_trades "
            "WHERE status NOT IN ('CLOSED', 'EXPIRED', 'VOID') "
            "ORDER BY executed_on DESC"
        )

    def void_trade(self, trade_id: str) -> None:
        self.db.execute(
            "UPDATE options_trades SET status = 'VOID' WHERE trade_id = ?",
            [trade_id],
        ).close()

    def update_monitor(self, trade_id: str, sl_level: Optional[float],
                       spot_at_execution: Optional[float]) -> None:
        self.db.execute(
            "UPDATE options_trades SET actual_stop_loss_level = ?, spot_at_execution = ? "
            "WHERE trade_id = ?",
            [sl_level, spot_at_execution, trade_id],
        ).close()

    def silence_alerts_until(self, trade_id: str, until: Optional[datetime]) -> None:
        """Suppress live SL/target alerts for this trade until ``until``.
        Pass ``None`` to clear the silence."""
        self.db.execute(
            "UPDATE options_trades SET alerts_silenced_until = ? WHERE trade_id = ?",
            [until, trade_id],
        ).close()

    def update_trailing(
        self,
        trade_id: str,
        *,
        trailing_pnl_floor: Optional[float],
        trailing_step_idx: int,
    ) -> None:
        """Phase 3 (#4) \u2014 persist the trailing SL state after a step trigger.

        ``trailing_pnl_floor`` is the live PnL value (rupees) below which we
        fire SL_TRIGGER. ``trailing_step_idx`` is the next step to consider
        (0 = no step armed yet, len(steps) = all steps consumed)."""
        self.db.execute(
            "UPDATE options_trades SET trailing_pnl_floor = ?, "
            "trailing_step_idx = ? WHERE trade_id = ?",
            [trailing_pnl_floor, int(trailing_step_idx), trade_id],
        ).close()

    def update_status(self, trade_id: str, status: str, daily_status: Optional[str] = None,
                      exit_instruction: Optional[str] = None) -> None:
        self.db.execute(
            "UPDATE options_trades SET status = ?, daily_status = ?, exit_instruction = ? "
            "WHERE trade_id = ?",
            [status, daily_status, exit_instruction, trade_id],
        ).close()

    def write_execution_provenance(
        self,
        trade_id: str,
        *,
        execution_data_source: Optional[str] = None,
        execution_provider: Optional[str] = None,
        execution_freshness_ms: Optional[int] = None,
        gate_passed: Optional[bool] = None,
        time_from_suggestion_sec: Optional[int] = None,
    ) -> None:
        """Phase 2c: stamp execution-time provenance after `insert`.

        Best-effort \u2014 callers swallow exceptions to fail-open on legacy
        DBs without the migration. Only non-None fields are written.
        """
        sets: list[str] = []
        params: list = []
        for col, val in [
            ("execution_data_source",    execution_data_source),
            ("execution_provider",       execution_provider),
            ("execution_freshness_ms",   execution_freshness_ms),
            ("gate_passed",              gate_passed),
            ("time_from_suggestion_sec", time_from_suggestion_sec),
        ]:
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if not sets:
            return
        params.append(trade_id)
        self.db.execute(
            f"UPDATE options_trades SET {', '.join(sets)} WHERE trade_id = ?",
            params,
        ).close()

    def update_pnl(self, trade_id: str, gross: float, charges: float, net: float) -> None:
        self.db.execute(
            "UPDATE options_trades SET gross_pnl = ?, total_charges = ?, net_pnl = ? "
            "WHERE trade_id = ?",
            [gross, charges, net, trade_id],
        ).close()

    def close_trade(self, trade_id: str, gross: float, charges: float, net: float) -> None:
        self.db.execute(
            "UPDATE options_trades SET status = 'CLOSED', closed_on = ?, "
            "gross_pnl = ?, total_charges = ?, net_pnl = ? WHERE trade_id = ?",
            [now_ist(), gross, charges, net, trade_id],
        ).close()
        # Best-effort: notify listeners (live risk monitor) that the watchlist
        # needs refreshing. Failure to publish must never break trade closure.
        try:
            from providers.event_bus import TOPIC_TRADE_CLOSED, get_event_bus
            get_event_bus().publish(TOPIC_TRADE_CLOSED, {"trade_id": trade_id})
        except Exception:  # pragma: no cover - defensive
            pass

    def next_trade_id(self, today: date) -> str:
        # Use MAX(seq), not COUNT(*) — see next_suggestion_id for rationale.
        prefix = f"TRD-{today.strftime('%Y%m%d')}-"
        row = self.db.fetch_one(
            "SELECT MAX(trade_id) AS m FROM options_trades "
            "WHERE trade_id LIKE ?",
            [prefix + "%"],
        )
        last = (row or {}).get("m")
        try:
            n = (int(str(last).rsplit("-", 1)[-1]) if last else 0) + 1
        except (ValueError, AttributeError):
            n = 1
        return f"{prefix}{n:03d}"

    def by_date(self, day: date) -> List[dict]:
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        return self.db.fetch_all(
            "SELECT * FROM options_trades WHERE executed_on >= ? AND executed_on < ? "
            "ORDER BY executed_on DESC",
            [start, end],
        )

    def legs_with_suggestion_info(self, trade_id: str) -> List[dict]:
        """Return trade legs joined with suggestion leg details (for supplement/close forms)."""
        return self.db.fetch_all(
            "SELECT tl.leg_order, tl.executed, tl.fill_price, tl.fill_time, tl.lots_actual, "
            "  tl.exit_price, tl.exit_time, tl.leg_pnl, "
            "  tl.suggestion_leg_id, "
            "  sl.symbol, sl.expiry_date, sl.strike, sl.option_type, sl.action, "
            "  sl.lots, sl.lot_size, sl.suggested_price, "
            "  sl.suggested_price_low, sl.suggested_price_high, sl.leg_purpose_note, "
            "  os.strategy "
            "FROM options_trade_legs tl "
            "JOIN options_suggestion_legs sl ON sl.id = tl.suggestion_leg_id "
            "JOIN options_suggestions os ON os.suggestion_id = sl.suggestion_id "
            "WHERE tl.trade_id = ? ORDER BY tl.leg_order",
            [trade_id],
        )

    def update_leg_fill(self, trade_id: str, leg_order: int,
                        fill_price: float, fill_time, lots_actual: Optional[int]) -> None:
        self.db.execute(
            "UPDATE options_trade_legs SET executed=1, fill_price=?, fill_time=?, "
            "not_filled_reason=NULL, lots_actual=? "
            "WHERE trade_id=? AND leg_order=?",
            [fill_price, fill_time, lots_actual, trade_id, leg_order],
        ).close()

    def update_leg_exit(self, trade_id: str, leg_order: int,
                        exit_price: float, exit_time, leg_pnl: float) -> None:
        self.db.execute(
            "UPDATE options_trade_legs SET exit_price=?, exit_time=?, leg_pnl=? "
            "WHERE trade_id=? AND leg_order=?",
            [exit_price, exit_time, leg_pnl, trade_id, leg_order],
        ).close()

    def update_position(self, trade_id: str, net_credit: float,
                        position_type: str, broken_json: Optional[str]) -> None:
        self.db.execute(
            "UPDATE options_trades SET net_credit_actual=?, position_type=?, "
            "broken_state_json=? WHERE trade_id=?",
            [net_credit, position_type, broken_json, trade_id],
        ).close()


# ---------------------------------------------------------------------------
# Re-suggestions
# ---------------------------------------------------------------------------

class ResuggestionRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(self, original_suggestion_id: str, generated_on: datetime,
               revised_legs: list, combined_economics: dict) -> None:
        self.db.execute(
            "INSERT INTO options_resuggestions "
            "(original_suggestion_id, generated_on, revised_legs_json, combined_economics_json) "
            "VALUES (?, ?, ?, ?)",
            [
                original_suggestion_id, generated_on,
                json.dumps(revised_legs, default=str),
                json.dumps(combined_economics, default=str),
            ],
        ).close()

    def for_suggestion(self, suggestion_id: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM options_resuggestions WHERE original_suggestion_id = ?",
            [suggestion_id],
        )


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class SimulationRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def ensure_simulation_row(self, suggestion_id: str, started_on: date) -> None:
        existing = self.db.fetch_one(
            "SELECT id FROM options_simulations WHERE suggestion_id = ?",
            [suggestion_id],
        )
        if existing is None:
            self.db.execute(
                "INSERT INTO options_simulations (suggestion_id, started_on, overall_quality) "
                "VALUES (?, ?, 'PENDING')",
                [suggestion_id, started_on],
            ).close()

    def upsert_leg_day(self, u: SimulationDayUpdate) -> None:
        sql = """
        MERGE options_simulation_legs AS T
        USING (SELECT ? AS suggestion_id, ? AS leg_order, ? AS leg_symbol, ? AS sim_date,
                      ? AS suggested_price, ? AS sim_entry_price,
                      ? AS open_price, ? AS high_price, ? AS low_price, ? AS settle_price,
                      ? AS quality, ? AS adjustment_note,
                      ? AS day_pnl, ? AS cumulative_pnl,
                      ? AS is_expiry_day, ? AS final_settle) AS S
        ON T.suggestion_id = S.suggestion_id AND T.leg_order = S.leg_order AND T.sim_date = S.sim_date
        WHEN MATCHED THEN UPDATE SET
            leg_symbol=S.leg_symbol, suggested_price=S.suggested_price,
            sim_entry_price=S.sim_entry_price, open_price=S.open_price,
            high_price=S.high_price, low_price=S.low_price, settle_price=S.settle_price,
            quality=S.quality, adjustment_note=S.adjustment_note,
            day_pnl=S.day_pnl, cumulative_pnl=S.cumulative_pnl,
            is_expiry_day=S.is_expiry_day, final_settle=S.final_settle
        WHEN NOT MATCHED THEN INSERT
            (suggestion_id, leg_order, leg_symbol, sim_date, suggested_price, sim_entry_price,
             open_price, high_price, low_price, settle_price,
             quality, adjustment_note, day_pnl, cumulative_pnl, is_expiry_day, final_settle)
            VALUES (S.suggestion_id, S.leg_order, S.leg_symbol, S.sim_date,
                    S.suggested_price, S.sim_entry_price, S.open_price, S.high_price,
                    S.low_price, S.settle_price, S.quality, S.adjustment_note,
                    S.day_pnl, S.cumulative_pnl, S.is_expiry_day, S.final_settle);
        """
        self.db.execute(sql, [
            u.suggestion_id, u.leg_order, "", u.sim_date,
            u.suggested_price, u.sim_entry_price,
            u.open_price, u.high_price, u.low_price, u.settle_price,
            u.quality, u.adjustment_note,
            u.day_pnl, u.cumulative_pnl,
            1 if u.is_expiry_day else 0, u.final_settle,
        ]).close()

    def update_summary(self, suggestion_id: str, completed_on: Optional[date],
                        overall_quality: str, sim_net_credit: float,
                        sim_final_pnl: float, sim_charges: float, sim_net_pnl: float,
                        notes: str = "") -> None:
        self.db.execute(
            "UPDATE options_simulations SET completed_on=?, overall_quality=?, "
            "sim_net_credit=?, sim_final_pnl=?, sim_charges=?, sim_net_pnl=?, notes=? "
            "WHERE suggestion_id = ?",
            [completed_on, overall_quality, sim_net_credit, sim_final_pnl,
             sim_charges, sim_net_pnl, notes, suggestion_id],
        ).close()

    def get_summary(self, suggestion_id: str) -> Optional[dict]:
        return self.db.fetch_one(
            "SELECT * FROM options_simulations WHERE suggestion_id = ?", [suggestion_id]
        )

    def get_legs(self, suggestion_id: str) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_simulation_legs WHERE suggestion_id = ? "
            "ORDER BY sim_date, leg_order",
            [suggestion_id],
        )


# ---------------------------------------------------------------------------
# Config (runtime overrides)
# ---------------------------------------------------------------------------

class ConfigRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def get_all(self) -> List[dict]:
        return self.db.fetch_all(
            "SELECT * FROM options_config ORDER BY category, config_key"
        )

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.fetch_one(
            "SELECT config_value FROM options_config WHERE config_key = ?", [key]
        )
        if row is None or row["config_value"] is None:
            return default
        v = row["config_value"]
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v

    def set(self, key: str, value: Any, category: Optional[str] = None,
            description: Optional[str] = None, default_value: Any = None,
            modified_by: str = "ui") -> None:
        v_json = json.dumps(value, default=str) if not isinstance(value, str) else value
        d_json = json.dumps(default_value, default=str) if default_value is not None else None
        sql = """
        MERGE options_config AS T
        USING (SELECT ? AS config_key, ? AS config_value, ? AS default_value,
                      ? AS category, ? AS description) AS S
        ON T.config_key = S.config_key
        WHEN MATCHED THEN UPDATE SET
            config_value = S.config_value,
            description  = COALESCE(S.description, T.description),
            category     = COALESCE(S.category, T.category),
            last_modified = SYSDATETIME(),
            modified_by  = ?
        WHEN NOT MATCHED THEN INSERT
            (config_key, config_value, default_value, category, description, modified_by)
            VALUES (S.config_key, S.config_value, S.default_value, S.category, S.description, ?);
        """
        self.db.execute(sql, [key, v_json, d_json, category, description, modified_by, modified_by]).close()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotificationRepo:
    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert(self, n: Notification) -> None:
        self.db.execute(
            "INSERT INTO options_notifications "
            "(created_at, notif_type, severity, title, body, "
            " related_suggestion_id, related_trade_id, "
            " source_event_id, provider, tick_age_ms, flag_state_at_dispatch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                n.created_at, n.notif_type, n.severity, n.title[:200], n.body,
                n.related_suggestion_id, n.related_trade_id,
                getattr(n, "source_event_id", None),
                getattr(n, "provider", None),
                getattr(n, "tick_age_ms", None),
                getattr(n, "flag_state_at_dispatch", None),
            ],
        ).close()

    def unread(self, limit: int = 50) -> List[dict]:
        return self.db.fetch_all(
            "SELECT TOP (?) * FROM options_notifications "
            "WHERE read_at IS NULL ORDER BY created_at DESC",
            [limit],
        )

    def recent(self, limit: int = 50) -> List[dict]:
        return self.db.fetch_all(
            "SELECT TOP (?) * FROM options_notifications ORDER BY created_at DESC",
            [limit],
        )

    def mark_read(self, notification_id: int) -> None:
        self.db.execute(
            "UPDATE options_notifications SET read_at = ? WHERE id = ?",
            [now_ist(), notification_id],
        ).close()

    def mark_all_read(self) -> None:
        self.db.execute(
            "UPDATE options_notifications SET read_at = ? WHERE read_at IS NULL",
            [now_ist()],
        ).close()

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute(
            "DELETE FROM options_notifications WHERE created_at < ?",
            [datetime.combine(cutoff, datetime.min.time())],
        )
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# IntradayCloseSnapshotRepo (Phase 2b.1)
# ---------------------------------------------------------------------------
class IntradayCloseSnapshotRepo:
    """Persistence layer for the 15:35 IST live-LTP capture used by the
    19:35 EOD-vs-live drift verifier. See `lifecycle/snapshot_orchestrator.py`.

    Rows are unique on `(snapshot_date, trade_id, leg_order)` so a re-run
    of the 15:35 job (or a manual trigger) cleanly replaces the day's
    capture for that leg.
    """

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert_many(self, rows: List[dict]) -> int:
        """Bulk insert/replace today's snapshot rows.

        Each `row` dict must contain:
            snapshot_date, captured_at, trade_id, leg_order, symbol,
            expiry_date, strike, option_type, ltp, source, provider,
            freshness_ms

        Idempotent via DELETE-then-INSERT scoped to (snapshot_date, trade_id,
        leg_order). Returns the count of rows inserted.
        """
        if not rows:
            return 0
        keys = {(r["snapshot_date"], r["trade_id"], r["leg_order"]) for r in rows}
        for snap_date, trade_id, leg_order in keys:
            self.db.execute(
                "DELETE FROM options_intraday_close_snapshot "
                "WHERE snapshot_date = ? AND trade_id = ? AND leg_order = ?",
                [snap_date, trade_id, leg_order],
            ).close()
        for r in rows:
            self.db.execute(
                "INSERT INTO options_intraday_close_snapshot "
                "(snapshot_date, captured_at, trade_id, leg_order, symbol, "
                " expiry_date, strike, option_type, ltp, source, provider, "
                " freshness_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    r["snapshot_date"], r["captured_at"], r["trade_id"],
                    r["leg_order"], r["symbol"], r["expiry_date"],
                    r["strike"], r["option_type"], r.get("ltp"),
                    r.get("source"), r.get("provider"), r.get("freshness_ms"),
                ],
            ).close()
        return len(rows)

    def get_by_date(self, snapshot_date: date) -> List[dict]:
        """Return all snapshot rows captured on `snapshot_date`."""
        return self.db.fetch_all(
            "SELECT * FROM options_intraday_close_snapshot "
            "WHERE snapshot_date = ? ORDER BY trade_id, leg_order",
            [snapshot_date],
        )

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute(
            "DELETE FROM options_intraday_close_snapshot WHERE snapshot_date < ?",
            [cutoff],
        )
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# 5-min chain aggregate timeseries (Zerodha WS)
# ---------------------------------------------------------------------------
class ChainTimeseriesRepo:
    """Persistence for 5-min chain aggregates produced by the WS aggregator.

    See `lifecycle/chain_aggregator.py`. Idempotent on
    (snapshot_at, symbol, expiry_date) — re-running the same flush replaces
    existing rows for that key.
    """

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert_many(self, rows: List[dict]) -> int:
        if not rows:
            return 0
        keys = {(r["snapshot_at"], r["symbol"], r["expiry_date"]) for r in rows}
        for snap_at, sym, exp in keys:
            self.db.execute(
                "DELETE FROM options_chain_5min "
                "WHERE snapshot_at = ? AND symbol = ? AND expiry_date = ?",
                [snap_at, sym, exp],
            ).close()
        for r in rows:
            self.db.execute(
                "INSERT INTO options_chain_5min "
                "(snapshot_at, symbol, expiry_date, spot, atm_strike, "
                " sum_call_oi, sum_put_oi, sum_call_oi_delta, sum_put_oi_delta, "
                " sum_call_volume, sum_put_volume, atm_call_mid, atm_put_mid, "
                " atm_call_spread_bps, atm_put_spread_bps, sample_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    r["snapshot_at"], r["symbol"], r["expiry_date"],
                    r.get("spot"), r.get("atm_strike"),
                    r.get("sum_call_oi"), r.get("sum_put_oi"),
                    r.get("sum_call_oi_delta"), r.get("sum_put_oi_delta"),
                    r.get("sum_call_volume"), r.get("sum_put_volume"),
                    r.get("atm_call_mid"), r.get("atm_put_mid"),
                    r.get("atm_call_spread_bps"), r.get("atm_put_spread_bps"),
                    r.get("sample_count"),
                ],
            ).close()
        return len(rows)

    def recent_window(
        self, symbol: str, expiry: date, since: datetime, limit: int = 24
    ) -> List[dict]:
        """Return up to `limit` most-recent rows for (symbol, expiry) at-or-after
        `since`, ordered ascending by snapshot_at."""
        rows = self.db.fetch_all(
            "SELECT TOP (?) * FROM options_chain_5min "
            "WHERE symbol = ? AND expiry_date = ? AND snapshot_at >= ? "
            "ORDER BY snapshot_at DESC",
            [int(limit), symbol, expiry, since],
        )
        return list(reversed(rows))

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute(
            "DELETE FROM options_chain_5min WHERE snapshot_at < ?",
            [datetime.combine(cutoff, datetime.min.time())],
        )
        n = cur.rowcount or 0
        cur.close()
        return n


# ---------------------------------------------------------------------------
# 5-min ATM IV timeseries (Zerodha WS)
# ---------------------------------------------------------------------------
class AtmIvTimeseriesRepo:
    """Persistence for 5-min ATM IV samples computed from WS ticks.

    Mirrors `ChainTimeseriesRepo` lifecycle. Idempotent on
    (snapshot_at, symbol, expiry_date).
    """

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def insert_many(self, rows: List[dict]) -> int:
        if not rows:
            return 0
        keys = {(r["snapshot_at"], r["symbol"], r["expiry_date"]) for r in rows}
        for snap_at, sym, exp in keys:
            self.db.execute(
                "DELETE FROM options_atm_iv_5min "
                "WHERE snapshot_at = ? AND symbol = ? AND expiry_date = ?",
                [snap_at, sym, exp],
            ).close()
        for r in rows:
            self.db.execute(
                "INSERT INTO options_atm_iv_5min "
                "(snapshot_at, symbol, expiry_date, atm_strike, spot, dte, atm_iv) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    r["snapshot_at"], r["symbol"], r["expiry_date"],
                    r.get("atm_strike"), r.get("spot"),
                    r.get("dte"), r.get("atm_iv"),
                ],
            ).close()
        return len(rows)

    def recent_window(
        self, symbol: str, expiry: date, since: datetime, limit: int = 24
    ) -> List[dict]:
        rows = self.db.fetch_all(
            "SELECT TOP (?) * FROM options_atm_iv_5min "
            "WHERE symbol = ? AND expiry_date = ? AND snapshot_at >= ? "
            "ORDER BY snapshot_at DESC",
            [int(limit), symbol, expiry, since],
        )
        return list(reversed(rows))

    def delete_older_than(self, cutoff: date) -> int:
        cur = self.db.execute(
            "DELETE FROM options_atm_iv_5min WHERE snapshot_at < ?",
            [datetime.combine(cutoff, datetime.min.time())],
        )
        n = cur.rowcount or 0
        cur.close()
        return n

