"""Scout dashboard template and static asset tests."""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def dashboard_html(client):
    return client.get("/").data.decode("utf-8")


def test_scout_execution_sidebar_matches_panel_title(dashboard_html):
    assert 'data-tab="scout-trades"' in dashboard_html
    idx = dashboard_html.index('data-tab="scout-trades"')
    snippet = dashboard_html[idx:idx + 400]
    assert "Execution</span>" in snippet
    assert "My Trades</span>" not in snippet
    assert "Intraday Scout — Execution</h2>" in dashboard_html


@pytest.mark.parametrize(
    "tab,label",
    [
        ("scout-signals", "Signals"),
        ("scout-trades", "Execution"),
        ("scout-history", "History"),
        ("scout-errors", "Errors"),
        ("scout-watchlist", "Watchlist"),
        ("scout-config", "Config"),
    ],
)
def test_scout_subtabs_present(dashboard_html, tab, label):
    assert f'data-stab="{tab}">{label}</button>' in dashboard_html


def test_scout_history_pnl_css_rules():
    css = (ROOT / "dashboard" / "static" / "scout.css").read_text(encoding="utf-8")
    required = [
        ".scout-hist-sum-meta strong.pnl-profit",
        ".scout-hist-sum-meta strong.pnl-loss",
        ".scout-hist-sum-meta strong.pnl-charge",
        ".scout-hist-sum-meta strong.pnl-winpct-good",
        ".scout-hist-sum-meta strong.pnl-winpct-bad",
        ".scout-hist-filter-result .pnl-profit",
    ]
    for rule in required:
        assert rule in css, f"missing CSS rule: {rule}"


def test_scout_js_exports_flow_loader():
    js = (ROOT / "dashboard" / "static" / "scout.js").read_text(encoding="utf-8")
    assert "function filterExecutionItems" in js
    assert "function renderExecutionFlow(items, marketOpen)" in js
    assert "Market is closed" in js
    assert "function pnlClass" in js
    assert "function winPctClass" in js
    assert "function pfClass" in js
