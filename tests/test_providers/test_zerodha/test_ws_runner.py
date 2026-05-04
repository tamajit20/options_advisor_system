"""
tests/test_providers/test_zerodha/test_ws_runner.py
====================================================

Unit tests for `KiteWSRunner`. We never touch real `kiteconnect.KiteTicker`
— a `FakeTicker` stand-in is injected via `ticker_factory=`. The runner's
`connect()` is not driven through its blocking loop here; we exercise the
callbacks (`on_connect`, `on_ticks`, `on_close`, `on_error`) directly to
verify behaviour deterministically.

What we cover:
    - singleton enforcement
    - tick → cache + event_bus publication
    - subscription reconcile (subscribe/unsubscribe/replace)
    - watchdog: no-tick + market-open → forced close
    - token-error path raises and runner becomes TOKEN_EXPIRED
    - graceful stop()
    - status() snapshot fields populated correctly
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, List, Optional
from unittest.mock import MagicMock

import pytest

from providers.base import DataSource, LiveQuote
from providers.cache import TTLCache
from providers.event_bus import EventBus, TOPIC_CONNECTION_STATE, TOPIC_TICK, TOPIC_TOKEN_EXPIRED
from providers.zerodha.ws_runner import (
    ConnState,
    KiteWSRunner,
    TokenMeta,
    _looks_like_token_error,
    _reset_singleton_for_tests,
    _tick_to_quote,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_singleton_for_tests()
    yield
    _reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeTicker:
    """Stand-in for `kiteconnect.KiteTicker`. Records subscribe/unsubscribe
    calls; lets the test fire `on_connect`, `on_ticks`, `on_close`,
    `on_error` directly."""

    MODE_FULL = "full"
    MODE_QUOTE = "quote"

    def __init__(self, api_key: str, access_token: str):
        self.api_key = api_key
        self.access_token = access_token
        self.subscribed: List[int] = []
        self.unsubscribed: List[int] = []
        self.modes: List[tuple] = []
        self.connect_called = False
        self.closed = False
        # Callbacks to be set by the runner
        self.on_ticks: Optional[Callable] = None
        self.on_connect: Optional[Callable] = None
        self.on_close: Optional[Callable] = None
        self.on_error: Optional[Callable] = None

    def connect(self, threaded: bool = False, disable_ssl_verification: bool = False) -> None:
        self.connect_called = True

    def subscribe(self, tokens: list) -> None:
        self.subscribed.extend(tokens)

    def unsubscribe(self, tokens: list) -> None:
        self.unsubscribed.extend(tokens)

    def set_mode(self, mode: str, tokens: list) -> None:
        self.modes.append((mode, list(tokens)))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_ticker_factory():
    holder: dict = {}

    def factory(api_key: str, access_token: str) -> FakeTicker:
        t = FakeTicker(api_key, access_token)
        holder["ticker"] = t
        return t

    factory.holder = holder  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def cache():
    return TTLCache(default_ttl_seconds=10.0)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def runner(cache, bus, fake_ticker_factory):
    r = KiteWSRunner(
        api_key="k",
        access_token="t",
        cache=cache,
        event_bus=bus,
        ticker_factory=fake_ticker_factory,
        is_market_open_fn=lambda: True,
        heartbeat_timeout=30.0,
    )
    return r


def _wire_runner(runner: KiteWSRunner) -> FakeTicker:
    """Build the ticker + wire handlers as the connect loop would, but
    without entering the blocking `connect()` call."""
    runner._build_ticker()
    runner._wire_handlers()
    return runner._ticker  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Construction / singleton
# ---------------------------------------------------------------------------
def test_requires_credentials(cache, bus, fake_ticker_factory):
    with pytest.raises(ValueError):
        KiteWSRunner(api_key="", access_token="t", cache=cache, event_bus=bus,
                     ticker_factory=fake_ticker_factory)


def test_singleton_enforced(cache, bus, fake_ticker_factory):
    KiteWSRunner(api_key="k", access_token="t", cache=cache, event_bus=bus,
                 ticker_factory=fake_ticker_factory)
    with pytest.raises(RuntimeError, match="singleton"):
        KiteWSRunner(api_key="k2", access_token="t2", cache=cache, event_bus=bus,
                     ticker_factory=fake_ticker_factory)


def test_singleton_resettable_for_tests(cache, bus, fake_ticker_factory):
    KiteWSRunner(api_key="k", access_token="t", cache=cache, event_bus=bus,
                 ticker_factory=fake_ticker_factory)
    _reset_singleton_for_tests()
    # Should not raise after reset
    KiteWSRunner(api_key="k", access_token="t", cache=cache, event_bus=bus,
                 ticker_factory=fake_ticker_factory)


# ---------------------------------------------------------------------------
# Tick parsing
# ---------------------------------------------------------------------------
def test_tick_to_quote_with_meta_option():
    expiry_dt = datetime(2026, 5, 28, tzinfo=timezone.utc)
    meta = TokenMeta(symbol="NIFTY", expiry=expiry_dt, strike=23000.0, option_type="CE")
    tick = {
        "instrument_token": 12345,
        "last_price": 152.75,
        "oi": 1_200_000,
        "depth": {"buy": [{"price": 152.50, "quantity": 50}],
                  "sell": [{"price": 153.00, "quantity": 75}]},
        "exchange_timestamp": datetime(2026, 5, 4, 10, 30, tzinfo=timezone.utc),
    }
    q = _tick_to_quote(tick, meta, "zerodha")
    assert isinstance(q, LiveQuote)
    assert q.symbol == "NIFTY"
    assert q.expiry == date(2026, 5, 28)
    assert q.strike == 23000.0
    assert q.option_type == "CE"
    assert q.last_price == 152.75
    assert q.bid == 152.50
    assert q.ask == 153.00
    assert q.open_interest == 1_200_000
    assert q.source == DataSource.LIVE
    assert q.provider == "zerodha"
    assert q.freshness_ms == 0


def test_tick_to_quote_without_meta_uses_token_placeholder():
    tick = {"instrument_token": 99, "last_price": 100.0}
    q = _tick_to_quote(tick, None, "zerodha")
    assert q.symbol == "token:99"
    assert q.expiry is None
    assert q.strike is None
    assert q.last_price == 100.0


def test_tick_to_quote_missing_depth_yields_none_bid_ask():
    tick = {"instrument_token": 1, "last_price": 50.0}
    q = _tick_to_quote(tick, None, "zerodha")
    assert q.bid is None and q.ask is None


# ---------------------------------------------------------------------------
# on_connect → reconcile + state
# ---------------------------------------------------------------------------
def test_on_connect_subscribes_desired_tokens(runner: KiteWSRunner, bus: EventBus):
    states: list = []
    bus.subscribe(TOPIC_CONNECTION_STATE, lambda p: states.append(p["state"]))

    runner.subscribe([100, 200, 300])
    fake = _wire_runner(runner)

    # Drive on_connect manually (as KiteTicker would).
    runner._on_connect(ws=None, response={})

    assert runner.status().state == ConnState.CONNECTED
    assert "connected" in states
    assert sorted(fake.subscribed) == [100, 200, 300]
    # Mode set in full mode by default
    assert fake.modes and fake.modes[0][0] == "full"
    assert sorted(fake.modes[0][1]) == [100, 200, 300]


def test_replace_subscriptions_diffs(runner: KiteWSRunner):
    runner.subscribe([1, 2, 3])
    fake = _wire_runner(runner)
    runner._on_connect(ws=None, response={})
    fake.subscribed.clear()
    fake.unsubscribed.clear()

    runner.replace_subscriptions([2, 3, 4])  # add 4, remove 1
    assert fake.subscribed == [4]
    assert fake.unsubscribed == [1]


def test_unsubscribe_applies(runner: KiteWSRunner):
    runner.subscribe([1, 2])
    fake = _wire_runner(runner)
    runner._on_connect(ws=None, response={})
    fake.unsubscribed.clear()

    runner.unsubscribe([1])
    assert fake.unsubscribed == [1]
    assert runner.desired_tokens() == {2}


# ---------------------------------------------------------------------------
# Tick → cache + event bus
# ---------------------------------------------------------------------------
def test_on_ticks_publishes_and_caches(runner: KiteWSRunner, cache: TTLCache, bus: EventBus):
    received: list = []
    bus.subscribe(TOPIC_TICK, received.append)

    expiry_dt = datetime(2026, 5, 28, tzinfo=timezone.utc)
    runner.set_token_meta(
        12345,
        TokenMeta(symbol="NIFTY", expiry=expiry_dt, strike=23000.0, option_type="CE"),
    )
    _wire_runner(runner)
    runner._on_connect(ws=None, response={})

    tick = {"instrument_token": 12345, "last_price": 152.0, "oi": 1000}
    runner._on_ticks(ws=None, ticks=[tick])

    assert len(received) == 1
    q = received[0]
    assert q.symbol == "NIFTY" and q.last_price == 152.0
    # Cached by token
    assert cache.get(12345) is q
    # Cached by option-tuple key
    key = ("NIFTY", date(2026, 5, 28), 23000.0, "CE")
    assert cache.get(key) is q
    # last_tick_at populated
    assert runner.status().last_tick_at is not None


def test_on_ticks_handles_empty_list(runner: KiteWSRunner):
    _wire_runner(runner)
    runner._on_connect(ws=None, response={})
    runner._on_ticks(ws=None, ticks=[])
    assert runner.status().last_tick_at is None


def test_on_ticks_with_index_meta_caches_spot_key(runner: KiteWSRunner, cache: TTLCache):
    runner.set_token_meta(256265, TokenMeta(symbol="NIFTY 50", is_index=True))
    _wire_runner(runner)
    runner._on_connect(ws=None, response={})

    runner._on_ticks(ws=None, ticks=[{"instrument_token": 256265, "last_price": 23456.5}])
    assert cache.get(("spot", "NIFTY 50")) is not None


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
def test_watchdog_triggers_close_after_idle(runner: KiteWSRunner):
    fake = _wire_runner(runner)
    runner._on_connect(ws=None, response={})

    # Simulate a tick 5 minutes ago
    runner._last_tick_at = datetime.now(tz=timezone.utc) - timedelta(seconds=300)
    alive = runner.watchdog_check()
    assert alive is False
    assert fake.closed is True


def test_watchdog_passive_when_market_closed(cache, bus, fake_ticker_factory):
    r = KiteWSRunner(
        api_key="k",
        access_token="t",
        cache=cache,
        event_bus=bus,
        ticker_factory=fake_ticker_factory,
        is_market_open_fn=lambda: False,
        heartbeat_timeout=10.0,
    )
    fake = _wire_runner(r)
    r._on_connect(ws=None, response={})
    r._last_tick_at = datetime.now(tz=timezone.utc) - timedelta(seconds=600)
    assert r.watchdog_check() is True
    assert fake.closed is False


def test_watchdog_degrades_after_repeated_resets(runner: KiteWSRunner):
    _wire_runner(runner)
    runner._on_connect(ws=None, response={})
    runner._last_tick_at = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    # Trigger 3 watchdog hits in quick succession; runner re-builds ticker each time.
    for _ in range(3):
        runner.watchdog_check()
        # State after close becomes whatever; rebuild a ticker so the next
        # watchdog_check sees CONNECTED again.
        runner._set_state(ConnState.CONNECTED)
        _wire_runner(runner)
        runner._last_tick_at = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    runner.watchdog_check()
    assert runner.status().state == ConnState.DEGRADED


# ---------------------------------------------------------------------------
# Token expiry
# ---------------------------------------------------------------------------
def test_on_error_raises_token_expired_for_403(runner: KiteWSRunner):
    _wire_runner(runner)
    from providers.zerodha.ws_runner import _TokenExpired  # noqa: WPS450
    with pytest.raises(_TokenExpired):
        runner._on_error(ws=None, code=403, reason="invalid token")


def test_on_error_does_not_raise_for_normal_error(runner: KiteWSRunner):
    _wire_runner(runner)
    runner._on_error(ws=None, code=1006, reason="abnormal closure")
    assert "abnormal" in (runner.status().last_error or "")


def test_handle_token_expiry_publishes_and_stops(runner: KiteWSRunner, bus: EventBus):
    seen: list = []
    bus.subscribe(TOPIC_TOKEN_EXPIRED, seen.append)
    _wire_runner(runner)

    runner._handle_token_expiry("403 invalid token")
    st = runner.status()
    assert st.state == ConnState.TOKEN_EXPIRED
    assert seen and seen[0]["provider"] == "zerodha"
    assert runner._stop_event.is_set()


def test_looks_like_token_error_helper():
    assert _looks_like_token_error(403, "Invalid api_key or access_token")
    assert _looks_like_token_error("error", "TOKEN expired")
    assert not _looks_like_token_error(1006, "connection lost")
    assert not _looks_like_token_error(500, "server error")


# ---------------------------------------------------------------------------
# Stop / shutdown
# ---------------------------------------------------------------------------
def test_stop_sets_stopped_and_closes_ticker(runner: KiteWSRunner):
    fake = _wire_runner(runner)
    runner._on_connect(ws=None, response={})
    runner.stop()
    assert runner.status().state == ConnState.STOPPED
    assert fake.closed is True
    assert runner._stop_event.is_set()


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------
def test_status_snapshot(runner: KiteWSRunner):
    _wire_runner(runner)
    runner.subscribe([1, 2, 3])
    runner._on_connect(ws=None, response={})
    s = runner.status()
    assert s.state == ConnState.CONNECTED
    assert s.subscribed_tokens == 3
    assert s.last_error is None
    assert s.reconnect_attempts == 0


def test_record_failure_starts_failure_window(runner: KiteWSRunner):
    runner._record_failure(RuntimeError("boom"))
    s = runner.status()
    assert s.reconnect_attempts == 1
    assert s.failure_started_at is not None
    assert "RuntimeError" in (s.last_error or "")
