"""
tests/test_database/test_runtime_flags.py
=========================================

Unit tests for `RuntimeFlagsRepo` (Phase 4).

We use an in-memory `_StubDB` rather than `mock_db` because the repo issues
both `fetch_all` and parameterised `execute(MERGE…)`, and we want to verify
that values round-trip through serialize → DB → parse.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import pytest

from database.runtime_flags import (
    DEFAULT_FLAGS,
    FLAG_CLOSURE_ALERTS,
    FLAG_KILL_SWITCH,
    FLAG_OPPORTUNITY_ALERTS,
    FLAG_SL_ALERTS,
    FLAG_TRADE_EXECUTION_ENABLED,
    FlagRow,
    RuntimeFlagsRepo,
)


# ---------------------------------------------------------------------------
# In-memory DB stub
# ---------------------------------------------------------------------------
class _Cursor:
    def __init__(self) -> None:
        self.rowcount = 0
    def close(self) -> None: ...


class _StubDB:
    """Simulates exactly the surface RuntimeFlagsRepo uses:
    `execute(sql, params).close()` and `fetch_all(sql)`.
    """

    def __init__(self) -> None:
        # rows keyed by flag_key
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.executed: List[tuple] = []
        self.fetched: int = 0

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> _Cursor:
        params = list(params or [])
        self.executed.append((sql, params))
        sql_norm = " ".join(sql.split()).upper()
        if sql_norm.startswith("INSERT INTO OPTIONS_RUNTIME_FLAGS"):
            key, val, typ, desc = params[0], params[1], params[2], params[3]
            self.rows[key] = {
                "flag_key":      key,
                "flag_value":    val,
                "flag_type":     typ,
                "description":   desc,
                "last_modified": datetime.utcnow(),
                "modified_by":   "system",
            }
        elif "MERGE OPTIONS_RUNTIME_FLAGS" in sql_norm:
            # params for our MERGE: [k, val, typ, desc, modby, k, val, typ, desc, modby]
            key = params[0]
            val, typ, desc, modby = params[1], params[2], params[3], params[4]
            self.rows[key] = {
                "flag_key":      key,
                "flag_value":    val,
                "flag_type":     typ,
                "description":   desc,
                "last_modified": datetime.utcnow(),
                "modified_by":   modby,
            }
        return _Cursor()

    def fetch_all(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        self.fetched += 1
        # Return shallow copies so callers can't mutate our state.
        return [dict(r) for r in self.rows.values()]


# ---------------------------------------------------------------------------
# Construction / parsing helpers
# ---------------------------------------------------------------------------
def test_rejects_negative_cache_ttl():
    with pytest.raises(ValueError):
        RuntimeFlagsRepo(_StubDB(), cache_ttl_seconds=-1)


def test_default_flag_inventory_complete():
    keys = {f.key for f in DEFAULT_FLAGS}
    assert keys == {
        FLAG_KILL_SWITCH,
        FLAG_SL_ALERTS,
        FLAG_CLOSURE_ALERTS,
        FLAG_OPPORTUNITY_ALERTS,
        FLAG_TRADE_EXECUTION_ENABLED,
    }


# ---------------------------------------------------------------------------
# seed_defaults
# ---------------------------------------------------------------------------
def test_seed_defaults_inserts_all_when_empty():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    inserted = repo.seed_defaults()
    assert inserted == len(DEFAULT_FLAGS)
    assert set(db.rows) == {f.key for f in DEFAULT_FLAGS}
    # Every row tagged 'system' on initial seed.
    assert all(r["modified_by"] == "system" for r in db.rows.values())


def test_seed_defaults_is_idempotent_and_does_not_overwrite():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    repo.seed_defaults()
    # Operator flips a flag
    repo.set(FLAG_KILL_SWITCH, True, modified_by="operator")
    assert db.rows[FLAG_KILL_SWITCH]["flag_value"] == "true"
    # Re-seeding must not overwrite the operator's choice
    n = repo.seed_defaults()
    assert n == 0
    assert db.rows[FLAG_KILL_SWITCH]["flag_value"] == "true"
    assert db.rows[FLAG_KILL_SWITCH]["modified_by"] == "operator"


# ---------------------------------------------------------------------------
# get_bool / get_int / get_text
# ---------------------------------------------------------------------------
def test_get_bool_round_trips_through_serialize_parse():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    repo.seed_defaults()
    assert repo.get_bool(FLAG_KILL_SWITCH) is False
    repo.set(FLAG_KILL_SWITCH, True)
    assert repo.get_bool(FLAG_KILL_SWITCH) is True
    repo.set(FLAG_KILL_SWITCH, False)
    assert repo.get_bool(FLAG_KILL_SWITCH) is False


def test_get_bool_falls_back_to_registered_default_when_row_missing():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    # No seed call → no rows
    assert repo.get_bool(FLAG_SL_ALERTS) is True   # registered default true
    assert repo.get_bool(FLAG_KILL_SWITCH) is False


def test_get_bool_explicit_default_overrides_registered():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    assert repo.get_bool(FLAG_KILL_SWITCH, default=True) is True


def test_get_bool_raises_when_type_mismatch():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    repo.seed_defaults()
    # Manually corrupt a row to a different type
    db.rows[FLAG_KILL_SWITCH]["flag_type"] = "int"
    db.rows[FLAG_KILL_SWITCH]["flag_value"] = "1"
    repo.invalidate_cache()
    with pytest.raises(ValueError):
        repo.get_bool(FLAG_KILL_SWITCH)


def test_unparseable_value_falls_back_to_default(caplog):
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    repo.seed_defaults()
    # Corrupt the int parse path: register flag as "int" with garbage value.
    db.rows[FLAG_KILL_SWITCH]["flag_type"] = "int"
    db.rows[FLAG_KILL_SWITCH]["flag_value"] = "not-a-number"
    repo.invalidate_cache()
    # Falls back to the *registered* default ("false" → bool default), but
    # since registered type is bool, the row is rebuilt as bool(False).
    flags = {f.key: f for f in repo.all()}
    assert flags[FLAG_KILL_SWITCH].value is False


# ---------------------------------------------------------------------------
# set / writes invalidate cache
# ---------------------------------------------------------------------------
def test_set_unknown_key_raises_keyerror():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    with pytest.raises(KeyError):
        repo.set("not_a_real_flag", True)


def test_set_invalidates_cache_so_next_read_sees_new_value():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=60.0)  # long TTL
    repo.seed_defaults()
    assert repo.get_bool(FLAG_KILL_SWITCH) is False
    repo.set(FLAG_KILL_SWITCH, True)
    # Without invalidation, the long TTL would still return False
    assert repo.get_bool(FLAG_KILL_SWITCH) is True


# ---------------------------------------------------------------------------
# Cache TTL behaviour
# ---------------------------------------------------------------------------
def test_cache_short_circuits_repeated_fetch():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=60.0)
    repo.seed_defaults()
    db.fetched = 0
    repo.get_bool(FLAG_KILL_SWITCH)
    repo.get_bool(FLAG_SL_ALERTS)
    repo.get_bool(FLAG_CLOSURE_ALERTS)
    assert db.fetched == 1


def test_zero_ttl_means_every_read_hits_db():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    repo.seed_defaults()
    db.fetched = 0
    repo.get_bool(FLAG_KILL_SWITCH)
    repo.get_bool(FLAG_SL_ALERTS)
    assert db.fetched == 2


# ---------------------------------------------------------------------------
# all() ordering
# ---------------------------------------------------------------------------
def test_all_orders_registered_flags_first():
    db = _StubDB()
    repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
    repo.seed_defaults()
    rows = repo.all()
    expected_first = [f.key for f in DEFAULT_FLAGS]
    assert [r.key for r in rows][: len(expected_first)] == expected_first
