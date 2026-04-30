"""
database/log_repo.py
====================

DB-first logging for the options advisor.

Provides:
    * `DBLogHandler` — a `logging.Handler` that writes each record to
      `options_system_logs`.
    * `install_db_logging(...)` — convenience to attach the handler to the
      root logger after the DB is up.
    * `LogRepo` — synchronous reader for the dashboard logs tab.
    * `JobLogRepo` — write/read for the `options_job_log` table.

Failure semantics:
    The handler must NEVER raise back into the calling code. If the DB write
    fails it falls back to stderr and continues.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from datetime import datetime, timedelta
from queue import Empty, Queue
from typing import Iterable, List, Optional

from config import LOGGING_CONFIG
from database.connection import SQLServerConnection
from utils import now_ist


# ---------------------------------------------------------------------------
# Async handler
# ---------------------------------------------------------------------------

class DBLogHandler(logging.Handler):
    """Buffered, threaded log handler that writes to `options_system_logs`.

    Records are queued and flushed on a background worker thread so that
    log calls never block the main path on a slow DB. On shutdown the
    queue is drained.
    """

    _SENTINEL = object()

    def __init__(self, batch_size: int = 50, flush_interval_sec: float = 2.0):
        super().__init__()
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self._queue: Queue = Queue(maxsize=10_000)
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="db-log-writer", daemon=True
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(self._SENTINEL)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=timeout)

    # ------------------------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            entry = self._record_to_row(record)
            self._queue.put_nowait(entry)
        except Exception:
            # Never let the logger blow up the program
            try:
                sys.stderr.write("DBLogHandler.emit failed:\n")
                traceback.print_exc(file=sys.stderr)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _record_to_row(self, record: logging.LogRecord) -> dict:
        exc_text = None
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
        ctx = getattr(record, "ctx", None)
        ctx_json = None
        if ctx is not None:
            try:
                ctx_json = json.dumps(ctx, default=str)[:8000]
            except Exception:
                ctx_json = str(ctx)[:8000]
        return {
            "logged_at":    now_ist(),
            "level":        record.levelname,
            "module":       record.name,
            "job_id":       getattr(record, "job_id", None),
            "message":      self.format(record)[:8000],
            "exception":    exc_text[:8000] if exc_text else None,
            "context_json": ctx_json,
        }

    # ------------------------------------------------------------------
    def _run(self) -> None:
        buffer: List[dict] = []
        last_flush = now_ist()
        while not self._stop.is_set() or not self._queue.empty() or buffer:
            try:
                item = self._queue.get(timeout=self.flush_interval_sec)
            except Empty:
                item = None
            if item is self._SENTINEL:
                break
            if isinstance(item, dict):
                buffer.append(item)
            elapsed = (now_ist() - last_flush).total_seconds()
            if buffer and (len(buffer) >= self.batch_size or elapsed >= self.flush_interval_sec):
                self._flush(buffer)
                buffer.clear()
                last_flush = now_ist()
        if buffer:
            self._flush(buffer)

    def _flush(self, rows: List[dict]) -> None:
        sql = (
            "INSERT INTO options_system_logs "
            "(logged_at, level, module, job_id, message, exception, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        params = [
            (
                r["logged_at"], r["level"], r["module"], r["job_id"],
                r["message"], r["exception"], r["context_json"],
            )
            for r in rows
        ]
        db = SQLServerConnection()
        try:
            db.connect()
            db.executemany(sql, params)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            sys.stderr.write(f"DBLogHandler flush failed for {len(rows)} rows\n")
            traceback.print_exc(file=sys.stderr)
        finally:
            db.close()


_HANDLER_SINGLETON: Optional[DBLogHandler] = None


def install_db_logging() -> Optional[DBLogHandler]:
    """Install the DB log handler on the root logger. Idempotent."""
    global _HANDLER_SINGLETON
    if _HANDLER_SINGLETON is not None:
        return _HANDLER_SINGLETON
    level = getattr(logging, LOGGING_CONFIG["db_level"].upper(), logging.INFO)
    handler = DBLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.start()
    logging.getLogger().addHandler(handler)
    _HANDLER_SINGLETON = handler
    return handler


def shutdown_db_logging() -> None:
    global _HANDLER_SINGLETON
    if _HANDLER_SINGLETON is not None:
        _HANDLER_SINGLETON.stop()
        logging.getLogger().removeHandler(_HANDLER_SINGLETON)
        _HANDLER_SINGLETON = None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

class LogRepo:
    """Read access for `options_system_logs`."""

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def fetch(
        self,
        level: Optional[str] = None,
        module: Optional[str] = None,
        job_id: Optional[str] = None,
        since: Optional[datetime] = None,
        search: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[dict]:
        clauses: List[str] = []
        params: list = []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if module:
            clauses.append("module LIKE ?")
            params.append(f"%{module}%")
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        if since is not None:
            clauses.append("logged_at >= ?")
            params.append(since)
        if search:
            clauses.append("(message LIKE ? OR exception LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT id, logged_at, level, module, job_id, message, exception, context_json "
            f"FROM options_system_logs {where} "
            f"ORDER BY logged_at DESC, id DESC "
            f"OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        )
        params.extend([offset, limit])
        return self.db.fetch_all(sql, params)

    def counts_by_level(self, since_hours: int = 24) -> dict:
        cutoff = now_ist() - timedelta(hours=since_hours)
        rows = self.db.fetch_all(
            "SELECT level, COUNT(*) AS n FROM options_system_logs "
            "WHERE logged_at >= ? GROUP BY level",
            [cutoff],
        )
        return {r["level"]: r["n"] for r in rows}


class JobLogRepo:
    """Write/read for `options_job_log`."""

    def __init__(self, db: SQLServerConnection):
        self.db = db

    def start(self, job_id: str, job_name: str) -> None:
        # Upsert: a re-run of the same job_id replaces the prior row.
        self.db.execute(
            "DELETE FROM options_job_log WHERE job_id = ?", [job_id]
        ).close()
        self.db.execute(
            "INSERT INTO options_job_log (job_id, job_name, started_at, status) "
            "VALUES (?, ?, ?, 'RUNNING')",
            [job_id, job_name, now_ist()],
        ).close()
        self.db.commit()

    def finish(
        self,
        job_id: str,
        status: str,
        rows_processed: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        self.db.execute(
            "UPDATE options_job_log "
            "SET finished_at = ?, status = ?, rows_processed = ?, error_message = ? "
            "WHERE job_id = ?",
            [now_ist(), status, rows_processed, (error_message or "")[:8000] if error_message else None, job_id],
        ).close()
        self.db.commit()

    def latest(self, limit: int = 50) -> List[dict]:
        return self.db.fetch_all(
            "SELECT TOP (?) job_id, job_name, started_at, finished_at, status, "
            "rows_processed, error_message "
            "FROM options_job_log ORDER BY started_at DESC",
            [limit],
        )

    def latest_status_per_job(self) -> List[dict]:
        return self.db.fetch_all(
            "SELECT job_name, status, started_at, finished_at, error_message "
            "FROM options_job_log j "
            "WHERE started_at = (SELECT MAX(started_at) FROM options_job_log "
            "                    WHERE job_name = j.job_name)"
        )

    def last_status(self, job_name: str) -> Optional[str]:
        row = self.db.fetch_one(
            "SELECT TOP 1 status FROM options_job_log "
            "WHERE job_name = ? ORDER BY started_at DESC",
            [job_name],
        )
        return row["status"] if row else None
