"""Tests for engine/zerodha_price_guard.py"""

from engine.zerodha_price_guard import (
    leg_limit_in_band,
    validate_limit_prices,
    validate_live_prices,
)


def _credit_spread_legs():
    # SELL band 95–105, BUY band 40–50 → net envelope 45–65
    return [
        {
            "leg_order": 1,
            "action": "SELL",
            "suggested_price": 100,
            "suggested_price_low": 95,
            "suggested_price_high": 105,
        },
        {
            "leg_order": 2,
            "action": "BUY",
            "suggested_price": 45,
            "suggested_price_low": 40,
            "suggested_price_high": 50,
        },
    ]


def test_band_veto_when_ltp_below_low():
    legs = [{
        "leg_order": 1,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }]
    res = validate_live_prices(legs, {1: 90.0}, require_band=True)
    assert not res.ok
    assert "below band" in res.reason()


def test_band_ok_when_inside():
    legs = [{
        "leg_order": 1,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }]
    res = validate_live_prices(legs, {1: 100.0}, require_band=True)
    assert res.ok


def test_limit_above_band_veto():
    legs = [{
        "leg_order": 1,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }]
    res = validate_limit_prices(legs, {1: 106.0}, require_band=True)
    assert not res.ok
    assert "above band" in res.reason()


def test_leg_limit_in_band_helper():
    leg = {
        "leg_order": 1,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }
    assert leg_limit_in_band(leg, 100.0)
    assert not leg_limit_in_band(leg, 110.0)


def test_drift_veto_when_no_band():
    legs = [{"leg_order": 1, "suggested_price": 100}]
    res = validate_live_prices(legs, {1: 120.0}, require_band=False, max_drift_pct=10)
    assert not res.ok
    assert "drift" in res.reason().lower() or "%" in res.reason()


def test_net_override_allows_per_leg_oob_when_structure_in_range():
    # SELL 90 (below 95) + BUY 35 (below 40) → net 55, still in 45–65
    legs = _credit_spread_legs()
    res = validate_live_prices(legs, {1: 90.0, 2: 35.0}, require_band=True)
    assert res.ok
    assert res.details.get("net_override") is True
    assert res.details["net_structure"]["ok"] is True


def test_net_override_blocks_when_structure_worse_than_floor():
    # SELL 90 + BUY 50 → net 40 < 45 floor
    legs = _credit_spread_legs()
    res = validate_live_prices(legs, {1: 90.0, 2: 50.0}, require_band=True)
    assert not res.ok
    assert "below minimum" in res.reason()


def test_net_override_allows_better_than_band_high():
    # SELL 110 (above) + BUY 40 → net 70 > 65 high — still allowed
    legs = _credit_spread_legs()
    res = validate_limit_prices(legs, {1: 110.0, 2: 40.0}, require_band=True)
    assert res.ok
    assert res.details.get("net_override") is True


def test_missing_ltp_not_overridden_by_net():
    legs = _credit_spread_legs()
    res = validate_live_prices(legs, {1: 100.0}, require_band=True)
    assert not res.ok
    assert "unavailable" in res.reason()
