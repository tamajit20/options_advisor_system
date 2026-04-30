"""
database/connection.py
======================

Standalone SQL Server connection wrapper for the options advisor.

Notes:
    * Borrows the pyodbc driver-pick + Trusted/SQL-Auth pattern from the equity
      stock analyzer system, BUT deliberately COPIED (not imported) — the two
      systems must remain independent. See ARCHITECTURE.txt.
    * Every public method is wrapped in try/except with logging; rollback is
      always safe (silently ignores a dead-connection rollback).
    * Connections are NOT pooled here. Each scheduler job and each Flask
      request opens + closes its own connection (short-lived).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List, Optional, Sequence

import pyodbc

from config import DATABASE_CONFIG

logger = logging.getLogger(__name__)


_DRIVER_PREFERENCES = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)


def _pick_driver() -> str:
    available = set(pyodbc.drivers())
    for d in _DRIVER_PREFERENCES:
        if d in available:
            return d
    # Fallback — pyodbc.connect will raise a helpful error if it's wrong
    return _DRIVER_PREFERENCES[0]


class SQLServerConnection:
    """Thin wrapper around `pyodbc.Connection` with explicit transaction
    semantics. Does NOT autocommit — callers MUST `commit()` or `rollback()`
    explicitly."""

    def __init__(
        self,
        server: Optional[str] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.server   = server   or DATABASE_CONFIG["server"]
        self.database = database or DATABASE_CONFIG["database"]
        self.username = username if username is not None else DATABASE_CONFIG["username"]
        self.password = password if password is not None else DATABASE_CONFIG["password"]
        self.timeout  = DATABASE_CONFIG["connection_timeout"]
        self.connection: Optional[pyodbc.Connection] = None
        self._connection_string: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def _build_connection_string(self, override_database: Optional[str] = None) -> str:
        driver = _pick_driver()
        db = override_database or self.database
        if self.username and self.password:
            cs = (
                f"DRIVER={{{driver}}};"
                f"SERVER={self.server};"
                f"DATABASE={db};"
                f"UID={self.username};"
                f"PWD={self.password};"
                f"TrustServerCertificate=yes;"
                f"MARS_Connection=Yes;"
            )
        else:
            cs = (
                f"DRIVER={{{driver}}};"
                f"SERVER={self.server};"
                f"DATABASE={db};"
                f"Trusted_Connection=yes;"
                f"TrustServerCertificate=yes;"
                f"MARS_Connection=Yes;"
            )
        return cs

    def connect(self, override_database: Optional[str] = None) -> pyodbc.Connection:
        cs = self._build_connection_string(override_database=override_database)
        self._connection_string = cs
        self.connection = pyodbc.connect(cs, timeout=self.timeout, autocommit=False)
        logger.info(
            "DB connected: server=%s database=%s auth=%s",
            self.server,
            override_database or self.database,
            "SQL" if self.username else "Windows",
        )
        return self.connection

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception as exc:
                logger.warning("Error closing DB connection: %s", exc)
            finally:
                self.connection = None

    # Context manager support
    def __enter__(self) -> "SQLServerConnection":
        if self.connection is None:
            self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Transaction control
    # ------------------------------------------------------------------
    def commit(self) -> None:
        if self.connection is not None:
            try:
                self.connection.commit()
            except Exception as exc:
                logger.error("commit() failed: %s", exc)
                raise

    def rollback(self) -> None:
        if self.connection is not None:
            try:
                self.connection.rollback()
            except Exception as exc:
                logger.warning("rollback() ignored (connection may be dead): %s", exc)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def _ensure_connected(self) -> None:
        if self.connection is None:
            self.connect()

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        self._ensure_connected()
        cur = self.connection.cursor()  # type: ignore[union-attr]
        cur.execute(sql, params or [])
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> int:
        self._ensure_connected()
        cur = self.connection.cursor()  # type: ignore[union-attr]
        try:
            cur.fast_executemany = True
        except AttributeError:
            pass
        rows = list(seq_of_params)
        if not rows:
            return 0
        cur.executemany(sql, rows)
        try:
            return cur.rowcount if cur.rowcount is not None else len(rows)
        finally:
            cur.close()

    def fetch_one(self, sql: str, params: Optional[Sequence[Any]] = None):
        cur = self.execute(sql, params)
        try:
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            cur.close()

    def fetch_all(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[dict]:
        cur = self.execute(sql, params)
        try:
            rows = cur.fetchall()
            if not rows or cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            cur.close()

    def scalar(self, sql: str, params: Optional[Sequence[Any]] = None):
        cur = self.execute(sql, params)
        try:
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------
    @staticmethod
    def with_retry(fn, *args, attempts: int = 3, backoff_seconds: float = 1.0, **kwargs):
        """Run `fn(*args, **kwargs)` with retries on transient pyodbc errors."""
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                return fn(*args, **kwargs)
            except pyodbc.OperationalError as exc:
                last_exc = exc
                logger.warning(
                    "DB transient error (attempt %d/%d): %s", attempt, attempts, exc
                )
                time.sleep(backoff_seconds * attempt)
        raise last_exc  # type: ignore[misc]
