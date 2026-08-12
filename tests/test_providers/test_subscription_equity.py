"""Subscription manager scout equity tagging tests."""

from __future__ import annotations

from datetime import date

import pytest

from providers.zerodha.instruments import InstrumentMaster
from providers.zerodha.subscription_manager import IndexSpec, SubscriptionManager
from providers.zerodha.ws_runner import TokenMeta


_RELIANCE = {
    "instrument_token": 738561,
    "exchange_token": 10,
    "tradingsymbol": "RELIANCE",
    "name": "RELIANCE INDUSTRIES",
    "expiry": None,
    "strike": 0.0,
    "tick_size": 0.05,
    "lot_size": 1,
    "instrument_type": "EQ",
    "segment": "NSE",
    "exchange": "NSE",
}

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


class FakeRunner:
    def __init__(self):
        self.token_meta = {}
        self.replace_calls = []
        self._desired = set()

    def set_token_meta(self, token, meta):
        self.token_meta[token] = meta

    def replace_subscriptions(self, tokens):
        self.replace_calls.append(set(tokens))
        self._desired = set(tokens)

    def desired_tokens(self):
        return set(self._desired)


@pytest.fixture
def master_with_equity():
    m = InstrumentMaster(loader=lambda: [_NIFTY_50, _RELIANCE], ttl_seconds=3600)
    m.refresh()
    return m


def _mgr(runner, master, *, equities=None):
    return SubscriptionManager(
        runner=runner,
        instrument_master=master,
        leg_loader=lambda: [],
        index_loader=lambda: [
            IndexSpec(internal_symbol="NIFTY", exchange="NSE", tradingsymbol="NIFTY 50"),
        ],
        equity_loader=lambda: list(equities or []),
    )


def test_equity_loader_tags_scout_equity_product(master_with_equity):
    runner = FakeRunner()
    mgr = _mgr(runner, master_with_equity, equities=["RELIANCE"])
    tokens = mgr.reconcile_once()
    assert 738561 in tokens
    meta = runner.token_meta[738561]
    assert meta.product == "scout_equity"
    assert meta.symbol == "RELIANCE"


def test_unresolved_equity_increments_unresolved_count(master_with_equity):
    runner = FakeRunner()
    mgr = _mgr(runner, master_with_equity, equities=["NOTINMASTER"])
    mgr.reconcile_once()
    assert mgr.status().last_unresolved_legs >= 1


def test_empty_equity_loader_skips_equity_tokens(master_with_equity):
    runner = FakeRunner()
    mgr = _mgr(runner, master_with_equity, equities=[])
    tokens = mgr.reconcile_once()
    assert 738561 not in tokens
    assert 256265 in tokens
