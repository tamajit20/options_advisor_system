"""
database/runtime_flags.py
=========================

Runtime kill switches (Phase 4).

A small, single-table key/value store the live components poll for "should I
do X right now?" decisions. The operator can flip any flag from the
dashboard without restarting any container.

Design
------
* One row per flag, primary key `flag_key`.
* `flag_value` is always stored as text. `flag_type` ∈ {`bool`, `int`,
  `text`} drives parsing on read.
* Booleans use the strings `"true"` and `"false"` (case-insensitive on read).
* The repo keeps a small in-process TTL cache (default 5s) so polling
  callers — like the WS subscription manager and intraday monitor — don't
  hammer the DB. Writes invalidate the cache.
* `seed_defaults()` ensures every known flag exists with its default value
  on first boot. Subsequent boots are a no-op for already-present rows.

Flag inventory
--------------
- `kill_switch`            (bool, default False) — master OFF for live data.
                                                   When True, the WS subscription
                                                   manager unsubscribes everything.
- `sl_alerts`              (bool, default True)  — emit SL_TRIGGER notifications.
- `closure_alerts`         (bool, default True)  — emit PERFECT_CLOSURE notifications.
- `opportunity_alerts`     (bool, default True)  — emit PERFECT_ENTRY notifications.
- `trade_execution_enabled`(bool, default False) — placeholder; we do not place
                                                   orders today, but the flag is
                                                   reserved so the dashboard UI
                                                   can show it as "off".
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from database.connection import SQLServerConnection


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Flag keys (string constants — referenced from other modules)
# ---------------------------------------------------------------------------

FLAG_KILL_SWITCH = "kill_switch"
FLAG_SL_ALERTS = "sl_alerts"
FLAG_CLOSURE_ALERTS = "closure_alerts"
FLAG_OPPORTUNITY_ALERTS = "opportunity_alerts"
FLAG_TRADE_EXECUTION_ENABLED = "trade_execution_enabled"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FlagSpec:
    key: str
    default: str
    type: str          # "bool" | "int" | "text"
    description: str


# Order matters only for predictable seeding.
DEFAULT_FLAGS: List[_FlagSpec] = [
    _FlagSpec(
        key=FLAG_KILL_SWITCH,
        default="false",
        type="bool",
        description=(
            "Master OFF for live data. When True, the WS subscription manager "
            "unsubscribes all tokens. Use to silence the system without "
            "restarting any container."
        ),
    ),
    _FlagSpec(
        key=FLAG_SL_ALERTS,
        default="true",
        type="bool",
        description="Emit SL_TRIGGER notifications when a short premium breaches the SL multiplier.",
    ),
    _FlagSpec(
        key=FLAG_CLOSURE_ALERTS,
        default="true",
        type="bool",
        description="Emit PERFECT_CLOSURE notifications when a leg reaches its target close.",
    ),
    _FlagSpec(
        key=FLAG_OPPORTUNITY_ALERTS,
        default="true",
        type="bool",
        description="Emit PERFECT_ENTRY notifications when a PENDING suggestion's net credit re-enters band.",
    ),
    _FlagSpec(
        key=FLAG_TRADE_EXECUTION_ENABLED,
        default="false",
        type="bool",
        description="Reserved. The system does not place broker orders today; flag exists for forward compatibility.",
    ),
]

_DEFAULTS_BY_KEY: Dict[str, _FlagSpec] = {f.key: f for f in DEFAULT_FLAGS}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse(value: str, flag_type: str) -> Any:
    if flag_type == "bool":
        return str(value).strip().lower() == "true"
    if flag_type == "int":
        return int(value)
    return str(value)


def _serialize(value: Any, flag_type: str) -> str:
    if flag_type == "bool":
        return "true" if bool(value) else "false"
    if flag_type == "int":
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

@dataclass
class FlagRow:
    key: str
    value: Any
    type: str
    description: Optional[str]
    last_modified: Optional[datetime]
    modified_by: Optional[str]


class RuntimeFlagsRepo:
    """Read/write access to `options_runtime_flags` with an in-process TTL
    cache. Thread-safe.

    Parameters
    ----------
    db:
        A connected `SQLServerConnection`.
    cache_ttl_seconds:
        How long a cache snapshot stays fresh (default 5s). Set to 0 to
        disable caching (useful in tests).
    """

    def __init__(self, db: SQLServerConnection, *, cache_ttl_seconds: float = 5.0):
        if cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must be >= 0")
        self.db = db
        self._cache_ttl = float(cache_ttl_seconds)
        self._cache: Dict[str, FlagRow] = {}
        self._cache_loaded_at: Optional[float] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ seed
    def seed_defaults(self) -> int:
        """Insert any missing default rows. Returns the count actually inserted.

        Existing rows are NOT overwritten — this is intentional so an operator
        toggle survives a re-init. Caller commits.
        """
        existing = self._fetch_all_uncached()
        inserted = 0
        for spec in DEFAULT_FLAGS:
            if spec.key in existing:
                continue
            self.db.execute(
                """
                INSERT INTO options_runtime_flags
                  (flag_key, flag_value, flag_type, description, modified_by)
                VALUES (?, ?, ?, ?, 'system')
                """,
                [spec.key, spec.default, spec.type, spec.description],
            ).close()
            inserted += 1
        if inserted:
            logger.info("runtime_flags: seeded %d default rows", inserted)
            self._invalidate()
        return inserted

    # ------------------------------------------------------------------ reads
    def get_bool(self, key: str, *, default: Optional[bool] = None) -> bool:
        """Read a boolean flag. Falls back to `default` (or the registered
        default if no override) when the row is missing."""
        row = self._get_cached(key)
        if row is None:
            return self._fallback_default(key, default, expected_type="bool")
        if row.type != "bool":
            raise ValueError(f"flag {key!r} is type {row.type}, not bool")
        return bool(row.value)

    def get_int(self, key: str, *, default: Optional[int] = None) -> int:
        row = self._get_cached(key)
        if row is None:
            return self._fallback_default(key, default, expected_type="int")
        if row.type != "int":
            raise ValueError(f"flag {key!r} is type {row.type}, not int")
        return int(row.value)

    def get_text(self, key: str, *, default: Optional[str] = None) -> str:
        row = self._get_cached(key)
        if row is None:
            return self._fallback_default(key, default, expected_type="text")
        return str(row.value)

    def all(self) -> List[FlagRow]:
        """Return every flag row (cached). Order: registered defaults first,
        in registration order, then any unregistered keys alphabetically."""
        snapshot = self._snapshot()
        ordered: List[FlagRow] = []
        seen: set = set()
        for spec in DEFAULT_FLAGS:
            if spec.key in snapshot:
                ordered.append(snapshot[spec.key])
                seen.add(spec.key)
        for key in sorted(snapshot.keys()):
            if key not in seen:
                ordered.append(snapshot[key])
        return ordered

    # ------------------------------------------------------------------ writes
    def set(self, key: str, value: Any, *, modified_by: str = "dashboard") -> None:
        """Update an existing flag row. Raises `KeyError` if the flag isn't
        registered (we don't allow free-form keys to keep the schema tidy).
        Caller commits."""
        spec = _DEFAULTS_BY_KEY.get(key)
        if spec is None:
            raise KeyError(f"unknown runtime flag: {key!r}")
        serialized = _serialize(value, spec.type)
        # MERGE so the row is created if seed_defaults() somehow hasn't run.
        sql = """
        MERGE options_runtime_flags AS tgt
        USING (SELECT ? AS flag_key) AS src
        ON tgt.flag_key = src.flag_key
        WHEN MATCHED THEN UPDATE SET
            flag_value    = ?,
            flag_type     = ?,
            description   = ?,
            last_modified = SYSDATETIME(),
            modified_by   = ?
        WHEN NOT MATCHED THEN
            INSERT (flag_key, flag_value, flag_type, description, modified_by)
            VALUES (?, ?, ?, ?, ?);
        """
        self.db.execute(
            sql,
            [
                key,
                serialized, spec.type, spec.description, modified_by,
                key, serialized, spec.type, spec.description, modified_by,
            ],
        ).close()
        self._invalidate()

    def invalidate_cache(self) -> None:
        """Force the next read to hit the DB."""
        self._invalidate()

    # ------------------------------------------------------------------ cache
    def _get_cached(self, key: str) -> Optional[FlagRow]:
        return self._snapshot().get(key)

    def _snapshot(self) -> Dict[str, FlagRow]:
        with self._lock:
            if (
                self._cache_loaded_at is not None
                and self._cache_ttl > 0
                and (time.monotonic() - self._cache_loaded_at) < self._cache_ttl
            ):
                return dict(self._cache)
            self._cache = self._fetch_all_uncached()
            self._cache_loaded_at = time.monotonic()
            return dict(self._cache)

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = {}
            self._cache_loaded_at = None

    def _fetch_all_uncached(self) -> Dict[str, FlagRow]:
        rows = self.db.fetch_all(
            "SELECT flag_key, flag_value, flag_type, description, "
            "       last_modified, modified_by "
            "FROM options_runtime_flags"
        )
        out: Dict[str, FlagRow] = {}
        for r in rows:
            try:
                parsed = _parse(r["flag_value"], r["flag_type"])
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "runtime_flags: cannot parse %s=%r as %s (%s); "
                    "falling back to registered default",
                    r["flag_key"], r["flag_value"], r["flag_type"], exc,
                )
                spec = _DEFAULTS_BY_KEY.get(r["flag_key"])
                if spec is None:
                    continue
                parsed = _parse(spec.default, spec.type)
            out[r["flag_key"]] = FlagRow(
                key=r["flag_key"],
                value=parsed,
                type=r["flag_type"],
                description=r.get("description"),
                last_modified=r.get("last_modified"),
                modified_by=r.get("modified_by"),
            )
        return out

    def _fallback_default(self, key: str, override: Any, *, expected_type: str) -> Any:
        if override is not None:
            return override
        spec = _DEFAULTS_BY_KEY.get(key)
        if spec is None:
            raise KeyError(f"no default registered for flag {key!r}")
        if spec.type != expected_type:
            raise ValueError(
                f"flag {key!r} is registered as {spec.type}, not {expected_type}"
            )
        return _parse(spec.default, spec.type)
