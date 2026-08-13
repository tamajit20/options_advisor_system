"""Tests for scout History tab display classifiers and aggregation."""

from __future__ import annotations

import pytest

from scout.history_display import (
    aggregate_trades,
    pf_class,
    pnl_class,
    trade_net_pnl,
    win_pct_class,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (100, "pnl-profit"),
        (0, "pnl-profit"),
        (-50.5, "pnl-loss"),
        ("42.3", "pnl-profit"),
        ("-1", "pnl-loss"),
        (None, ""),
        ("", ""),
        ("abc", ""),
        (float("nan"), ""),
    ],
)
def test_pnl_class(value, expected):
    assert pnl_class(value) == expected


@pytest.mark.parametrize(
    "pct,expected",
    [
        (55, "pnl-winpct-good"),
        (80, "pnl-winpct-good"),
        (54, "pnl-winpct-neutral"),
        (45, "pnl-winpct-neutral"),
        (44, "pnl-winpct-bad"),
        (0, "pnl-winpct-bad"),
        (None, ""),
        ("bad", ""),
    ],
)
def test_win_pct_class(pct, expected):
    assert win_pct_class(pct) == expected


@pytest.mark.parametrize(
    "pf,expected",
    [
        (1.0, "pnl-profit"),
        (1.5, "pnl-profit"),
        (0.99, "pnl-loss"),
        (0, "pnl-loss"),
        (None, ""),
        ("x", ""),
    ],
)
def test_pf_class(pf, expected):
    assert pf_class(pf) == expected


def test_trade_net_pnl_prefers_net_pnl_field():
    assert trade_net_pnl({"net_pnl": 10, "pnl": 100}) == 10.0
    assert trade_net_pnl({"pnl": 100}) == 100.0
    assert trade_net_pnl({}) == 0.0


def test_aggregate_trades_mixed_win_loss():
    trades = [
        {"pnl": 100, "net_pnl": 80, "total_charges": 20},
        {"pnl": -50, "net_pnl": -60, "total_charges": 10},
        {"pnl": 30, "net_pnl": 25, "total_charges": 5},
    ]
    agg = aggregate_trades(trades)

    assert agg["count"] == 3
    assert agg["wins"] == 2
    assert agg["win_pct"] == 67
    assert agg["net_pnl"] == 45.0
    assert agg["total_charges"] == 35.0
    assert agg["avg_win"] == 52.5
    assert agg["avg_loss"] == -60.0
    assert agg["profit_factor"] == 1.75
    assert pnl_class(agg["net_pnl"]) == "pnl-profit"
    assert win_pct_class(agg["win_pct"]) == "pnl-winpct-good"


def test_aggregate_trades_all_losses():
    trades = [
        {"pnl": -40, "net_pnl": -45},
        {"pnl": -10, "net_pnl": -12},
    ]
    agg = aggregate_trades(trades)

    assert agg["wins"] == 0
    assert agg["win_pct"] == 0
    assert agg["net_pnl"] == -57.0
    assert agg["profit_factor"] == 0.0
    assert pnl_class(agg["net_pnl"]) == "pnl-loss"
    assert win_pct_class(agg["win_pct"]) == "pnl-winpct-bad"


def test_aggregate_trades_empty():
    agg = aggregate_trades([])
    assert agg["count"] == 0
    assert agg["win_pct"] == 0
    assert agg["net_pnl"] == 0
    assert agg["profit_factor"] is None


def test_summary_color_contract():
    """Document expected CSS classes for History summary rows."""
    agg = aggregate_trades([
        {"pnl": 50, "net_pnl": 40, "total_charges": 10},
        {"pnl": -30, "net_pnl": -35, "total_charges": 5},
    ])
    net_cls = pnl_class(agg["net_pnl"])
    win_cls = win_pct_class(agg["win_pct"])
    pf = pf_class(agg["profit_factor"])

    assert net_cls in ("pnl-profit", "pnl-loss")
    assert win_cls in ("pnl-winpct-good", "pnl-winpct-bad", "pnl-winpct-neutral")
    assert pf in ("pnl-profit", "pnl-loss", "")
    assert pnl_class(agg["avg_win"]) == "pnl-profit"
    assert pnl_class(agg["avg_loss"]) == "pnl-loss"
