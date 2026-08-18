"""Tests for engine.exit_pricing — close prices, quote keys, multi-leg P&L."""
from __future__ import annotations

from datetime import date

import pytest

from engine.exit_pricing import (
    aligned_current_chain,
    build_close_suggestion,
    format_leg_quote_key,
    leg_close_pnl,
    lookup_leg_ltp,
    unique_leg_expiries,
)


def test_quote_key_includes_expiry():
    near = format_leg_quote_key("BANKNIFTY", date(2026, 8, 25), 57600, "CE")
    far = format_leg_quote_key("banknifty", "2026-09-29", 57600.0, "ce")
    assert near == "BANKNIFTY|2026-08-25|57600.0|CE"
    assert far == "BANKNIFTY|2026-09-29|57600.0|CE"
    assert near != far


def test_calendar_close_suggestion_uses_distinct_expiry_mids():
    """TRD-20260817-005 shape: same strike CE, two expiries, must not collapse."""
    near, far = date(2026, 8, 25), date(2026, 9, 29)
    legs = [
        {
            "leg_order": 1, "action": "SELL", "symbol": "BANKNIFTY",
            "expiry_date": near, "strike": 57600.0, "option_type": "CE",
            "fill_price": 535.6, "lots_actual": 1, "lots": 1, "lot_size": 35,
        },
        {
            "leg_order": 2, "action": "BUY", "symbol": "BANKNIFTY",
            "expiry_date": far, "strike": 57600.0, "option_type": "CE",
            "fill_price": 1177.2, "lots_actual": 1, "lots": 1, "lot_size": 35,
        },
    ]
    chains = {
        near: [{"strike": 57600.0, "option_type": "CE", "settle_price": 500.0}],
        far: [{"strike": 57600.0, "option_type": "CE", "settle_price": 1100.0}],
    }
    payload = build_close_suggestion(legs, chains, spot=57000.0)
    by_order = {l["leg_order"]: l for l in payload["legs"]}
    assert by_order[1]["suggested_close"] == pytest.approx(500.0)
    assert by_order[2]["suggested_close"] == pytest.approx(1100.0)
    # (535.6-500)*35 + (1100-1177.2)*35 = 1246 - 2702 = -1456
    assert payload["est_gross_pnl"] == pytest.approx(-1456.0)
    # Collapsing both legs onto one mid would always equal -premium paid.
    premium = (1177.2 - 535.6) * 35
    assert payload["est_gross_pnl"] != pytest.approx(-premium)


def test_iron_condor_same_expiry_still_resolves_by_strike():
    exp = date(2026, 8, 27)
    legs = [
        {"leg_order": 1, "action": "BUY", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 22800.0, "option_type": "PE", "fill_price": 40.0,
         "lots": 1, "lot_size": 75},
        {"leg_order": 2, "action": "SELL", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23000.0, "option_type": "PE", "fill_price": 80.0,
         "lots": 1, "lot_size": 75},
        {"leg_order": 3, "action": "SELL", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23600.0, "option_type": "CE", "fill_price": 70.0,
         "lots": 1, "lot_size": 75},
        {"leg_order": 4, "action": "BUY", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23800.0, "option_type": "CE", "fill_price": 35.0,
         "lots": 1, "lot_size": 75},
    ]
    chain = [
        {"strike": 22800.0, "option_type": "PE", "settle_price": 30.0},
        {"strike": 23000.0, "option_type": "PE", "settle_price": 55.0},
        {"strike": 23600.0, "option_type": "CE", "settle_price": 50.0},
        {"strike": 23800.0, "option_type": "CE", "settle_price": 22.0},
    ]
    payload = build_close_suggestion(legs, {exp: chain}, spot=23300.0)
    closes = [l["suggested_close"] for l in payload["legs"]]
    assert closes == [30.0, 55.0, 50.0, 22.0]
    expected = (
        leg_close_pnl(action="BUY", fill_price=40, close_price=30, lots=1, lot_size=75)
        + leg_close_pnl(action="SELL", fill_price=80, close_price=55, lots=1, lot_size=75)
        + leg_close_pnl(action="SELL", fill_price=70, close_price=50, lots=1, lot_size=75)
        + leg_close_pnl(action="BUY", fill_price=35, close_price=22, lots=1, lot_size=75)
    )
    assert payload["est_gross_pnl"] == pytest.approx(expected)


def test_vertical_and_straddle_same_expiry_resolve_by_strike_or_type():
    """Same-expiry verticals (strike) and straddles (CE/PE) stay unique with expiry in the key."""
    exp = date(2026, 8, 27)
    vertical = [
        {"leg_order": 1, "action": "SELL", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23000.0, "option_type": "CE", "fill_price": 120.0,
         "lots": 1, "lot_size": 75},
        {"leg_order": 2, "action": "BUY", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23200.0, "option_type": "CE", "fill_price": 40.0,
         "lots": 1, "lot_size": 75},
    ]
    v_chain = [
        {"strike": 23000.0, "option_type": "CE", "settle_price": 90.0},
        {"strike": 23200.0, "option_type": "CE", "settle_price": 25.0},
    ]
    v = build_close_suggestion(vertical, {exp: v_chain}, spot=22950.0)
    assert [l["suggested_close"] for l in v["legs"]] == [90.0, 25.0]
    assert v["est_gross_pnl"] == pytest.approx((120 - 90) * 75 + (25 - 40) * 75)

    straddle = [
        {"leg_order": 1, "action": "BUY", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23000.0, "option_type": "CE", "fill_price": 80.0,
         "lots": 1, "lot_size": 75},
        {"leg_order": 2, "action": "BUY", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23000.0, "option_type": "PE", "fill_price": 70.0,
         "lots": 1, "lot_size": 75},
    ]
    s_chain = [
        {"strike": 23000.0, "option_type": "CE", "settle_price": 110.0},
        {"strike": 23000.0, "option_type": "PE", "settle_price": 55.0},
    ]
    s = build_close_suggestion(straddle, {exp: s_chain}, spot=23050.0)
    assert [l["suggested_close"] for l in s["legs"]] == [110.0, 55.0]
    assert s["est_gross_pnl"] == pytest.approx((110 - 80) * 75 + (55 - 70) * 75)


def test_aligned_chain_keeps_calendar_mids_positional():
    near, far = date(2026, 8, 25), date(2026, 9, 29)
    legs = [
        {"strike": 57600.0, "option_type": "CE", "expiry_date": near},
        {"strike": 57600.0, "option_type": "CE", "expiry_date": far},
    ]
    chain = aligned_current_chain(legs, {
        near: [{"strike": 57600.0, "option_type": "CE", "settle_price": 500.0}],
        far: [{"strike": 57600.0, "option_type": "CE", "settle_price": 1100.0}],
    })
    assert [r["mid_price"] for r in chain] == [500.0, 1100.0]
    assert chain[0]["expiry_date"] == "2026-08-25"
    assert chain[1]["expiry_date"] == "2026-09-29"


def test_lookup_prefers_4part_calendar_keys():
    near = format_leg_quote_key("BANKNIFTY", date(2026, 8, 25), 57600, "CE")
    far = format_leg_quote_key("BANKNIFTY", date(2026, 9, 29), 57600, "CE")
    marks = {near: 303.0, far: 982.5}
    assert lookup_leg_ltp(
        marks, symbol="BANKNIFTY", strike=57600, option_type="CE",
        expiry=date(2026, 8, 25),
    ) == pytest.approx(303.0)
    assert lookup_leg_ltp(
        marks, symbol="banknifty", strike="57600.0000", option_type="ce",
        expiry="2026-09-29",
    ) == pytest.approx(982.5)


def test_lookup_ambiguous_3part_returns_none():
    marks = {
        "BANKNIFTY|57600.0|CE": 303.0,
        "NIFTY|57600.0|CE": 10.0,  # different symbol — not a collision
    }
    # Two 3-part keys for the SAME symbol+strike+CE cannot live in one dict.
    # Simulate the map the old exporter built when it overwrote: one key.
    # Ambiguity is two 3-part keys that both match want3 — use duplicate
    # formatting variants that JS would treat as the same after parseFloat.
    # Python dict can't hold two identical keys, so pass a synthetic pair
    # via a map that lookup iterates: CE vs ce stored before uppercasing.
    # lookup uppercases on compare, so "BANKNIFTY|57600.0|CE" and
    # "banknifty|57600.0|ce" both match want3.
    colliding = {
        "BANKNIFTY|57600.0|CE": 303.0,
        "banknifty|57600.0|ce": 982.5,
    }
    assert lookup_leg_ltp(
        colliding, symbol="BANKNIFTY", strike=57600, option_type="CE",
        expiry=date(2026, 8, 25),
    ) is None


def test_lookup_unique_legacy_3part_still_works_for_verticals():
    marks = {"NIFTY|23000.0|CE": 90.0, "NIFTY|23200.0|CE": 25.0}
    assert lookup_leg_ltp(
        marks, symbol="NIFTY", strike=23000, option_type="CE",
        expiry=date(2026, 8, 27),
    ) == pytest.approx(90.0)
    assert lookup_leg_ltp(
        marks, symbol="NIFTY", strike=23200, option_type="CE",
        expiry=date(2026, 8, 27),
    ) == pytest.approx(25.0)


def test_lookup_4part_wins_over_legacy_same_strike():
    marks = {
        "BANKNIFTY|57600.0|CE": 977.55,
        format_leg_quote_key("BANKNIFTY", date(2026, 8, 25), 57600, "CE"): 303.0,
        format_leg_quote_key("BANKNIFTY", date(2026, 9, 29), 57600, "CE"): 982.5,
    }
    assert lookup_leg_ltp(
        marks, symbol="BANKNIFTY", strike=57600, option_type="CE",
        expiry=date(2026, 8, 25),
    ) == pytest.approx(303.0)


def test_calendar_collapsed_marks_equal_minus_premium():
    """If both legs are marked at the same mid, gross P&L == −premium paid."""
    near, far = date(2026, 8, 25), date(2026, 9, 29)
    legs = [
        {
            "leg_order": 1, "action": "SELL", "symbol": "BANKNIFTY",
            "expiry_date": near, "strike": 57600.0, "option_type": "CE",
            "fill_price": 535.6, "lots": 1, "lot_size": 35,
        },
        {
            "leg_order": 2, "action": "BUY", "symbol": "BANKNIFTY",
            "expiry_date": far, "strike": 57600.0, "option_type": "CE",
            "fill_price": 1177.2, "lots": 1, "lot_size": 35,
        },
    ]
    collapsed = {
        near: [{"strike": 57600.0, "option_type": "CE", "settle_price": 977.55}],
        far: [{"strike": 57600.0, "option_type": "CE", "settle_price": 977.55}],
    }
    payload = build_close_suggestion(legs, collapsed, spot=57000.0)
    premium = (1177.2 - 535.6) * 35
    assert payload["est_gross_pnl"] == pytest.approx(-premium)


def test_strangle_same_expiry_resolves_by_strike():
    exp = date(2026, 8, 27)
    legs = [
        {"leg_order": 1, "action": "BUY", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 22800.0, "option_type": "PE", "fill_price": 70.0,
         "lots": 1, "lot_size": 75},
        {"leg_order": 2, "action": "BUY", "symbol": "NIFTY", "expiry_date": exp,
         "strike": 23200.0, "option_type": "CE", "fill_price": 80.0,
         "lots": 1, "lot_size": 75},
    ]
    chain = [
        {"strike": 22800.0, "option_type": "PE", "settle_price": 55.0},
        {"strike": 23200.0, "option_type": "CE", "settle_price": 110.0},
    ]
    payload = build_close_suggestion(legs, {exp: chain}, spot=23000.0)
    assert [l["suggested_close"] for l in payload["legs"]] == [55.0, 110.0]
    assert payload["est_gross_pnl"] == pytest.approx((55 - 70) * 75 + (110 - 80) * 75)


def test_unique_leg_expiries_preserves_order():
    near, far = date(2026, 8, 25), date(2026, 9, 29)
    legs = [
        {"expiry_date": near},
        {"expiry_date": far},
        {"expiry_date": near},
    ]
    assert unique_leg_expiries(legs) == [near, far]


def test_aligned_chain_accepts_iso_string_map_keys():
    near, far = date(2026, 8, 25), date(2026, 9, 29)
    legs = [
        {"strike": 57600.0, "option_type": "CE", "expiry_date": near},
        {"strike": 57600.0, "option_type": "CE", "expiry_date": far},
    ]
    chain = aligned_current_chain(legs, {
        "2026-08-25": [{"strike": 57600.0, "option_type": "CE", "settle_price": 303.0}],
        "2026-09-29": [{"strike": 57600.0, "option_type": "CE", "settle_price": 982.5}],
    })
    assert [r["mid_price"] for r in chain] == [303.0, 982.5]


def test_dashboard_js_lookup_keeps_calendar_contract():
    """UI lookup must keep the same 4-part / unique-3-part rules as Python."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "dashboard" / "static" / "dashboard.js"
    text = src.read_text(encoding="utf-8")
    assert "parts.length === 4" in text
    assert "matches.length === 1" in text
    assert "lookup_leg_ltp" in text
