"""Tests for Nifty 50 index constituent sync."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from scout import index_constituents as ic


@pytest.fixture(autouse=True)
def _reset_index_constituents_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "_memory", None)
    monkeypatch.setattr(ic, "_cache_path", lambda: tmp_path / ic._CACHE_FILENAME)
    yield
    ic._memory = None


def test_parse_nse_index_payload_eq_only_and_priority():
    payload = {
        "data": [
            {"symbol": "TCS", "series": "EQ", "priority": 2},
            {"symbol": "RELIANCE", "series": "EQ", "priority": 1},
            {"symbol": "NIFTYBEES", "series": "BE", "priority": 1},
        ]
    }
    assert ic.parse_nse_index_payload(payload) == ["RELIANCE", "TCS"]


def test_refresh_writes_cache_and_memory(monkeypatch):
    monkeypatch.setattr(
        ic,
        "fetch_nifty50_from_nse",
        lambda: ["RELIANCE", "TCS", "INFY"],
    )
    out = ic.refresh_nifty50_constituents(force=True)
    assert out == ["RELIANCE", "TCS", "INFY"]
    assert ic.get_nifty50_symbols() == ["RELIANCE", "TCS", "INFY"]
    cached = json.loads(ic._cache_path().read_text(encoding="utf-8"))
    assert cached["source"] == "nse"
    assert cached["symbols"] == ["RELIANCE", "TCS", "INFY"]


def test_refresh_falls_back_to_config_when_nse_fails(monkeypatch):
    monkeypatch.setattr(ic, "fetch_nifty50_from_nse", lambda: (_ for _ in ()).throw(RuntimeError("nse down")))
    monkeypatch.setattr(ic, "default_nifty50_symbols", lambda: ["AAA", "BBB"])
    out = ic.refresh_nifty50_constituents(force=True)
    assert out == ["AAA", "BBB"]


def test_refresh_uses_stale_cache_when_nse_fails(monkeypatch):
    path = ic._cache_path()
    path.write_text(
        json.dumps(
            {
                "updated_at": (ic.now_ist() - timedelta(days=2)).isoformat(),
                "source": "nse",
                "index": "NIFTY 50",
                "symbols": ["OLD1", "OLD2"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ic, "fetch_nifty50_from_nse", lambda: (_ for _ in ()).throw(RuntimeError("nse down")))
    out = ic.refresh_nifty50_constituents(force=True)
    assert out == ["OLD1", "OLD2"]


def test_get_nifty50_symbols_uses_fresh_cache_without_nse(monkeypatch):
    path = ic._cache_path()
    path.write_text(
        json.dumps(
            {
                "updated_at": ic.now_ist().isoformat(),
                "source": "nse",
                "index": "NIFTY 50",
                "symbols": ["CACHED"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ic,
        "fetch_nifty50_from_nse",
        lambda: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    assert ic.get_nifty50_symbols() == ["CACHED"]


def test_fetch_nifty50_from_nse_parses_response(monkeypatch):
    resp = MagicMock()
    resp.json.return_value = {
        "data": [
            {"symbol": f"SYM{i}", "series": "EQ", "priority": i}
            for i in range(50)
        ]
    }
    monkeypatch.setattr(ic, "make_session", lambda: MagicMock())
    monkeypatch.setattr(ic, "fetch_with_retry", lambda *_a, **_k: resp)
    syms = ic.fetch_nifty50_from_nse()
    assert len(syms) == 50
    assert syms[0] == "SYM0"
