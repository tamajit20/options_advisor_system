"""Advisory data-edge notes must warn without hiding a suggestion."""
from datetime import date

from contracts import MarketIndicators, SuggestionLeg
from engine.data_edge import build_data_edge_checks


def _ind(**kwargs) -> MarketIndicators:
    base = dict(
        symbol="NIFTY",
        as_of=date(2026, 5, 4),
        spot=24000.0,
        pcr=1.0,
        max_pain=24000.0,
        atr_14=100.0,
        trend="SIDEWAYS",
        vix_close=14.0,
        vix_regime="STABLE",
        oi_walls_call=[],
        oi_walls_put=[],
        expected_move=200.0,
        hv_20=0.12,
        iv_premium=1.0,
        fii_net_futures=0.0,
    )
    base.update(kwargs)
    return MarketIndicators(**base)


def _leg(action, opt, strike) -> SuggestionLeg:
    return SuggestionLeg(
        leg_order=1,
        hedge_pair_leg=None,
        symbol="NIFTY",
        expiry_date=date(2026, 5, 28),
        strike=strike,
        option_type=opt,
        action=action,
        lots=1,
        lot_size=75,
        suggested_price=10.0,
        suggested_price_low=9.0,
        suggested_price_high=11.0,
        leg_purpose_note="",
    )


def test_quiet_signals_add_no_notes():
    checks = build_data_edge_checks(
        strategy="IRON_CONDOR",
        iv_rank=70.0,
        indicators=_ind(),
        legs=[_leg("SELL", "CE", 24500), _leg("SELL", "PE", 23500)],
    )
    assert checks == []


def test_warnings_are_advisory_and_do_not_use_hard_fail():
    checks = build_data_edge_checks(
        strategy="IRON_CONDOR",
        iv_rank=40.0,
        indicators=_ind(
            max_pain=25000.0,
            oi_pcr_slope_5min=2.0,
            oi_pcr_persistence=0.9,
        ),
        legs=[_leg("SELL", "CE", 24200)],
    )
    assert checks
    assert all(c.kind == "ADVISORY" for c in checks)
    assert all(c.status != "FAIL" for c in checks)
    labels = {c.label for c in checks}
    assert "Mid-IV zone" in labels
    assert "Range trade vs OI PCR drift" in labels
    assert "Max pain vs short strikes" in labels


def test_fii_conflict_and_quiet_volume_warn_only():
    fii = build_data_edge_checks(
        strategy="BULL_CALL_SPREAD",
        iv_rank=20.0,
        indicators=_ind(fii_net_calls=10_000, fii_net_puts=200_000),
        legs=[_leg("BUY", "CE", 24000)],
    )
    assert any(c.label == "FII options book" and c.status == "SOFT_FAIL" for c in fii)

    aligned = build_data_edge_checks(
        strategy="BULL_CALL_SPREAD",
        iv_rank=20.0,
        indicators=_ind(fii_net_calls=200_000, fii_net_puts=10_000),
        legs=[_leg("BUY", "CE", 24000)],
    )
    assert not any(c.label == "FII options book" for c in aligned)

    vol = build_data_edge_checks(
        strategy="LONG_STRADDLE",
        iv_rank=15.0,
        indicators=_ind(volume_burst_z=0.1),
        legs=[_leg("BUY", "CE", 24000)],
    )
    assert any(c.label == "Volume burst" and c.kind == "ADVISORY" for c in vol)
