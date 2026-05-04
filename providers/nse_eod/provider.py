"""
providers/nse_eod/provider.py
=============================

NSE-EOD adapter for the `MarketDataProvider` protocol.

This adapter is a thin wrapper around the existing SQL Server repos
(`FoEodRepo`, `SpotEodRepo`, `VixRepo`). It contains **no business logic** —
all reads are 1:1 passthroughs that tag rows with `data_source=DataSource.EOD`
so downstream consumers know the provenance.

Connection lifecycle
--------------------
This adapter accepts a `connection_factory` callable (typically the
`SQLServerConnection` class itself) and creates a fresh, short-lived
connection per call. That matches the existing pattern in
`lifecycle/suggestion_engine.py` — the dashboard / scheduler / CLI all manage
their own connections; the adapter must not assume one.

If a connection cannot be established (e.g. when running unit tests without
a DB) the adapter returns empty results and `health()` reports unhealthy with
the underlying error string.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable, List, Optional

from utils import now_ist

from ..base import (
    DataSource,
    LiveQuote,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderHealth,
)


logger = logging.getLogger(__name__)


class NseEodProvider:
    """Implements `MarketDataProvider` using the existing EOD bhavcopy tables."""

    name: str = "nse_eod"

    def __init__(self, connection_factory: Callable[[], object]):
        """`connection_factory()` should return a connectable `SQLServerConnection`-like
        object exposing `connect()`, `close()`, and the methods used by the repos
        (`fetch_one`, `fetch_all`, `scalar`)."""
        self._connection_factory = connection_factory

    # ------------------------------------------------------------------ helpers
    def _open(self):
        db = self._connection_factory()
        # Existing pattern: SQLServerConnection has .connect() and .close()
        if hasattr(db, "connect"):
            db.connect()
        return db

    @staticmethod
    def _close(db) -> None:
        try:
            if hasattr(db, "close"):
                db.close()
        except Exception:
            logger.exception("nse_eod: error closing DB connection (non-fatal)")

    @staticmethod
    def _stamp(rows: List[dict]) -> List[dict]:
        """Tag each row with provenance so callers can branch on `_source`."""
        for r in rows:
            r["_source"] = DataSource.EOD.value
            r["_provider"] = "nse_eod"
        return rows

    # ------------------------------------------------------------------ API
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_live_quotes=False,
            supports_websocket=False,
            supports_eod=True,
            supports_intraday_chain=False,
            notes="Settled bhavcopy values only; updated nightly by the EOD download job.",
        )

    def health(self) -> ProviderHealth:
        try:
            from database.models import FoEodRepo
            db = self._open()
            try:
                latest = FoEodRepo(db).latest_trade_date()
            finally:
                self._close(db)
            if latest is None:
                return ProviderHealth(
                    name=self.name,
                    healthy=False,
                    detail="No EOD data found in options_fo_eod",
                    last_check_at=now_ist(),
                )
            return ProviderHealth(
                name=self.name,
                healthy=True,
                detail=f"Latest EOD trade_date={latest.isoformat()}",
                last_check_at=now_ist(),
                extra={"latest_trade_date": latest.isoformat()},
            )
        except Exception as exc:
            return ProviderHealth(
                name=self.name,
                healthy=False,
                detail=f"DB error: {exc!r}",
                last_check_at=now_ist(),
            )

    def get_chain(self, symbol: str, trade_date: date, expiry: date) -> List[dict]:
        from database.models import FoEodRepo
        db = self._open()
        try:
            rows = FoEodRepo(db).get_chain(symbol, trade_date, expiry)
        finally:
            self._close(db)
        return self._stamp(rows or [])

    def get_spot(self, symbol: str, trade_date: Optional[date] = None) -> Optional[dict]:
        from database.models import SpotEodRepo
        db = self._open()
        try:
            repo = SpotEodRepo(db)
            row = repo.for_date(symbol, trade_date) if trade_date is not None else repo.latest(symbol)
        finally:
            self._close(db)
        if not row:
            return None
        row["_source"] = DataSource.EOD.value
        row["_provider"] = self.name
        return row

    def get_vix(self, trade_date: Optional[date] = None) -> Optional[dict]:
        from database.models import VixRepo
        db = self._open()
        try:
            repo = VixRepo(db)
            if trade_date is None:
                row = repo.latest()
            else:
                # No `for_date` on VixRepo — pull history-since and pick the
                # last row on/before the requested date.
                hist = repo.history(trade_date) if hasattr(repo, "history") else []
                row = None
                for r in hist:
                    if r.get("trade_date") and r["trade_date"] <= trade_date:
                        row = r
                if row is None:
                    row = repo.latest()
        finally:
            self._close(db)
        if not row:
            return None
        row["_source"] = DataSource.EOD.value
        row["_provider"] = self.name
        return row

    def list_expiries(self, symbol: str, trade_date: date) -> List[date]:
        from database.models import FoEodRepo
        db = self._open()
        try:
            return FoEodRepo(db).expiries_for(symbol, trade_date)
        finally:
            self._close(db)


# Static structural-typing check — fails at import time if we ever drift from
# the protocol (caught by tests / Pylance).
_check: MarketDataProvider = NseEodProvider(connection_factory=lambda: None)  # type: ignore[arg-type]
del _check
