"""
tests/test_providers/test_zerodha/test_subscription_manager.py
==============================================================

Unit tests for the dynamic `SubscriptionManager` (Phase 2b-ii).

We do NOT spin up a real `KiteWSRunner` here. The manager talks to the
runner only through three public methods (`set_token_meta`,
`replace_subscriptions`, `desired_tokens`) — a `FakeRunner` is enough.

`InstrumentMaster` is real but loaded from an in-memory list of dict rows
that mimic Kite's `instruments()` response.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Iterable, List, Optional, Set

import pytest

from providers.zerodha.instruments import InstrumentMaster
from providers.zerodha.subscription_manager import (
    DEFAULT_INDEX_SPECS,
    IndexSpec,
    SubscriptionManager,
    make_db_leg_loader,
    make_static_leg_loader,
)
from providers.zerodha.ws_runner import TokenMeta


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeRunner:
    """Stand-in for `KiteWSRunner` that records calls."""

    def __init__(self) -> None:
        self.token_meta: dict[int, TokenMeta] = {}
        self.replace_calls: List[Set[int]] = []
        self._desired: Set[int] = set()

    def set_token_meta(self, instrument_token: int, meta: TokenMeta) -> None:
        self.token_meta[int(instrument_token)] = meta

    def replace_subscriptions(self, tokens: Iterable[int]) -> None:
        new = {int(t) for t in tokens}
        self.replace_calls.append(new)
        self._desired = new

    def desired_tokens(self) -> Set[int]:
        return set(self._desired)


# Instrument-master rows. Tokens chosen to be obviously fake so any leak
# into a real call would be loud.
_NIFTY_22000_CE = {
    "instrument_token": 9000001,
    "exchange_token": 1,
    "tradingsymbol": "NIFTY26MAY22000CE",
    "name": "NIFTY",
    "expiry": date(2026, 5, 28),
    "strike": 22000.0,
    "tick_size": 0.05,
    "lot_size": 75,
    "instrument_type": "CE",
    "segment": "NFO-OPT",
    "exchange": "NFO",
}
_NIFTY_22000_PE = {**_NIFTY_22000_CE,
                    "instrument_token": 9000002,
                    "tradingsymbol": "NIFTY26MAY22000PE",
                    "instrument_type": "PE"}
_BANKNIFTY_50000_CE = {
    "instrument_token": 9000010,
    "exchange_token": 2,
    "tradingsymbol": "BANKNIFTY26MAY50000CE",
    "name": "BANKNIFTY",
    "expiry": date(2026, 5, 28),
    "strike": 50000.0,
    "tick_size": 0.05,
    "lot_size": 35,
    "instrument_type": "CE",
    "segment": "NFO-OPT",
    "exchange": "NFO",
}

# Indexes — note Kite reports their `expiry` as None and instrument_type "EQ"
# is not used; the "INDICES" segment uses an empty string. We mirror what we
# need for `get_by_tradingsymbol` lookups.
_NIFTY_50 = {
    "instrument_token": 256265,
    "exchange_token": 1,
    "tradingsymbol": "NIFTY 50",
    "name": "",
    "expiry": None,
    "strike": 0.0,
    "tick_size": 0.05,
    "lot_size": 0,
    "instrument_type": "",
    "segment": "INDICES",
    "exchange": "NSE",
}
_NIFTY_BANK = {**_NIFTY_50, "instrument_token": 260105, "tradingsymbol": "NIFTY BANK"}
_NIFTY_FIN = {**_NIFTY_50, "instrument_token": 257801, "tradingsymbol": "NIFTY FIN SERVICE"}
_INDIA_VIX = {**_NIFTY_50, "instrument_token": 264969, "tradingsymbol": "INDIA VIX"}


@pytest.fixture
def master():
    rows = [
        _NIFTY_22000_CE, _NIFTY_22000_PE, _BANKNIFTY_50000_CE,
        _NIFTY_50, _NIFTY_BANK, _NIFTY_FIN, _INDIA_VIX,
    ]
    m = InstrumentMaster(loader=lambda: rows, ttl_seconds=3600)
    m.refresh()
    return m


@pytest.fixture
def runner():
    return FakeRunner()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_rejects_non_positive_interval(master, runner):
    with pytest.raises(ValueError):
        SubscriptionManager(
            runner=runner,
            instrument_master=master,
            leg_loader=lambda: [],
            interval_seconds=0,
        )


# ---------------------------------------------------------------------------
# reconcile_once — index resolution
# ---------------------------------------------------------------------------
def test_default_indexes_resolved_and_subscribed(master, runner):
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [],  # no legs
    )
    tokens = mgr.reconcile_once()
    assert tokens == {256265, 260105, 257801, 264969}
    # Each is tagged is_index=True with the canonical internal symbol
    assert runner.token_meta[256265] == TokenMeta(symbol="NIFTY", is_index=True)
    assert runner.token_meta[260105] == TokenMeta(symbol="BANKNIFTY", is_index=True)
    assert runner.token_meta[257801] == TokenMeta(symbol="FINNIFTY", is_index=True)
    assert runner.token_meta[264969] == TokenMeta(symbol="VIX", is_index=True)
    assert runner.replace_calls == [{256265, 260105, 257801, 264969}]


def test_unresolved_index_increments_counter(master, runner):
    bad = (IndexSpec("UNKNOWN", "NSE", "NOT IN MASTER"),)
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [],
        index_loader=lambda: bad,
    )
    tokens = mgr.reconcile_once()
    assert tokens == set()
    assert mgr.status().last_unresolved_legs == 1


def test_custom_index_loader_overrides_default(master, runner):
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [],
        index_loader=lambda: [IndexSpec("NIFTY", "NSE", "NIFTY 50")],
    )
    tokens = mgr.reconcile_once()
    assert tokens == {256265}


# ---------------------------------------------------------------------------
# reconcile_once — option leg resolution
# ---------------------------------------------------------------------------
def test_option_legs_resolved_with_full_meta(master, runner):
    legs = [
        ("NIFTY", date(2026, 5, 28), 22000.0, "CE"),
        ("NIFTY", date(2026, 5, 28), 22000.0, "PE"),
    ]
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=make_static_leg_loader(legs),
        index_loader=lambda: [],  # focus on legs
    )
    tokens = mgr.reconcile_once()
    assert tokens == {9000001, 9000002}
    meta = runner.token_meta[9000001]
    assert meta.symbol == "NIFTY"
    assert meta.strike == 22000.0
    assert meta.option_type == "CE"
    assert meta.is_index is False
    assert meta.expiry == datetime(2026, 5, 28)


def test_unresolved_option_leg_logged_and_skipped(master, runner, caplog):
    legs = [
        ("NIFTY", date(2026, 5, 28), 22000.0, "CE"),
        ("NIFTY", date(2026, 5, 28), 99999.0, "CE"),  # not in master
    ]
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=make_static_leg_loader(legs),
        index_loader=lambda: [],
    )
    tokens = mgr.reconcile_once()
    assert tokens == {9000001}
    assert mgr.status().last_unresolved_legs == 1


# ---------------------------------------------------------------------------
# reconcile_once — diff & idempotency
# ---------------------------------------------------------------------------
def test_no_change_means_no_replace_call(master, runner):
    legs = [("NIFTY", date(2026, 5, 28), 22000.0, "CE")]
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=make_static_leg_loader(legs),
        index_loader=lambda: [],
    )
    mgr.reconcile_once()
    assert len(runner.replace_calls) == 1
    # Second reconcile with same legs → no second replace call
    mgr.reconcile_once()
    assert len(runner.replace_calls) == 1


def test_added_leg_triggers_replace(master, runner):
    legs = [("NIFTY", date(2026, 5, 28), 22000.0, "CE")]
    mutable = list(legs)
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: list(mutable),
        index_loader=lambda: [],
    )
    mgr.reconcile_once()
    mutable.append(("BANKNIFTY", date(2026, 5, 28), 50000.0, "CE"))
    mgr.reconcile_once()
    assert len(runner.replace_calls) == 2
    assert runner.replace_calls[-1] == {9000001, 9000010}


def test_empty_legs_and_no_indexes_yield_empty_set(master, runner):
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [], index_loader=lambda: [],
    )
    tokens = mgr.reconcile_once()
    assert tokens == set()
    # Even an empty set should be applied once, since the runner starts
    # with an empty desired set and there's nothing to diff. Our
    # implementation says "no change → skip", which is correct here.
    assert runner.replace_calls == []


def test_status_increments_reconcile_count(master, runner):
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [], index_loader=lambda: [],
    )
    mgr.reconcile_once()
    mgr.reconcile_once()
    assert mgr.status().reconcile_count == 2
    assert mgr.status().last_reconcile_at is not None


# ---------------------------------------------------------------------------
# Background loop start/stop
# ---------------------------------------------------------------------------
def test_start_runs_initial_reconcile_then_stop(master, runner):
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [],
        index_loader=lambda: [IndexSpec("NIFTY", "NSE", "NIFTY 50")],
        interval_seconds=60.0,  # long, only initial pass exercised
    )
    mgr.start()
    # Wait briefly for the initial reconcile
    deadline = time.time() + 2.0
    while time.time() < deadline and mgr.status().reconcile_count == 0:
        time.sleep(0.02)
    assert mgr.status().reconcile_count >= 1
    mgr.stop()
    # Idempotent stop
    mgr.stop()


def test_start_is_idempotent(master, runner):
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [], index_loader=lambda: [],
        interval_seconds=60.0,
    )
    mgr.start()
    first = mgr._thread  # noqa: SLF001
    mgr.start()
    assert mgr._thread is first  # noqa: SLF001
    mgr.stop()


def test_loop_swallows_loader_errors(master, runner):
    counter = {"calls": 0}

    def _flaky() -> Iterable:
        counter["calls"] += 1
        raise RuntimeError("DB blip")

    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=_flaky, index_loader=lambda: [],
        interval_seconds=0.05,
    )
    mgr.start()
    deadline = time.time() + 1.0
    while time.time() < deadline and counter["calls"] < 2:
        time.sleep(0.02)
    mgr.stop()
    # The loop kept running across the error; status carries the message
    assert counter["calls"] >= 2
    assert "DB blip" in (mgr.status().last_error or "")


# ---------------------------------------------------------------------------
# DB-backed loader factory
# ---------------------------------------------------------------------------
class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.last_sql: Optional[str] = None

    def fetch_all(self, sql, params=None):
        self.last_sql = sql
        return list(self.rows)


def test_make_db_leg_loader_yields_tuples():
    rows = [
        {"symbol": "NIFTY",     "expiry_date": date(2026, 5, 28), "strike": 22000.0, "option_type": "CE"},
        {"symbol": "BANKNIFTY", "expiry_date": datetime(2026, 5, 28, 0, 0), "strike": 50000.0, "option_type": "PE"},
    ]
    loader = make_db_leg_loader(_FakeDB(rows))
    out = list(loader())
    assert out == [
        ("NIFTY", date(2026, 5, 28), 22000.0, "CE"),
        ("BANKNIFTY", date(2026, 5, 28), 50000.0, "PE"),
    ]


def test_make_db_leg_loader_query_unions_active_and_pending():
    db = _FakeDB([])
    loader = make_db_leg_loader(db)
    list(loader())
    assert db.last_sql is not None
    assert "options_trades" in db.last_sql
    assert "options_suggestions" in db.last_sql
    assert "ACTIVE" in db.last_sql
    assert "PENDING" in db.last_sql
    assert "UNION" in db.last_sql.upper()


def test_make_static_leg_loader_snapshots():
    seed = [("NIFTY", date(2026, 5, 28), 22000.0, "CE")]
    loader = make_static_leg_loader(seed)
    seed.append(("X", date(2026, 5, 28), 0.0, "CE"))  # mutate after construction
    assert list(loader()) == [("NIFTY", date(2026, 5, 28), 22000.0, "CE")]


# ---------------------------------------------------------------------------
# DEFAULT_INDEX_SPECS shape
# ---------------------------------------------------------------------------
def test_default_index_specs_cover_all_underlyings_and_vix():
    syms = {s.internal_symbol for s in DEFAULT_INDEX_SPECS}
    assert {"NIFTY", "BANKNIFTY", "FINNIFTY", "VIX"} <= syms


# ---------------------------------------------------------------------------
# Phase 4 — kill-switch integration
# ---------------------------------------------------------------------------
def test_kill_switch_unsubscribes_all(master, runner):
    legs = [("NIFTY", date(2026, 5, 28), 22000.0, "CE")]
    flag = {"on": False}
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=make_static_leg_loader(legs),
        kill_switch_fn=lambda: flag["on"],
    )
    # First reconcile with switch OFF — normal subscribe
    mgr.reconcile_once()
    assert {9000001} <= runner.replace_calls[-1]
    # Flip the switch ON — expect empty replace
    flag["on"] = True
    mgr.reconcile_once()
    assert runner.replace_calls[-1] == set()
    # Status reflects 0 tokens
    assert mgr.status().last_token_count == 0


def test_kill_switch_off_no_replace_when_already_empty(master, runner):
    flag = {"on": True}
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=lambda: [],
        index_loader=lambda: [],
        kill_switch_fn=lambda: flag["on"],
    )
    mgr.reconcile_once()
    # No legs, no indexes, kill on → desired set already empty → no replace
    assert runner.replace_calls == []


def test_kill_switch_fn_error_treated_as_off(master, runner, caplog):
    def _broken() -> bool:
        raise RuntimeError("flag DB down")

    legs = [("NIFTY", date(2026, 5, 28), 22000.0, "CE")]
    mgr = SubscriptionManager(
        runner=runner, instrument_master=master,
        leg_loader=make_static_leg_loader(legs),
        index_loader=lambda: [],
        kill_switch_fn=_broken,
    )
    tokens = mgr.reconcile_once()
    # Kill switch error must NOT silence the system. Subscribe normally.
    assert tokens == {9000001}
