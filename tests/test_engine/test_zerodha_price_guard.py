"""Tests for engine/zerodha_price_guard.py"""

from engine.zerodha_price_guard import validate_live_prices


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
    from engine.zerodha_price_guard import validate_limit_prices
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
    from engine.zerodha_price_guard import leg_limit_in_band
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
