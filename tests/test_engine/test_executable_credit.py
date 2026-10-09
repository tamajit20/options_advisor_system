"""Sell-at-bid / buy-at-ask credit must clear the width floor or the card is not offered."""
from datetime import date

import pytest

from contracts import ConfidenceCheck, ConfidenceResult, SuggestionLeg
from engine import strategy_selector as ss
from engine.leg_builder import executable_net_premium
from exceptions import StrategyVeto


def _leg(action, opt, strike, price=10.0) -> SuggestionLeg:
    return SuggestionLeg(
        leg_order=1,
        hedge_pair_leg=None,
        symbol="NIFTY",
        expiry_date=date(2026, 5, 14),
        strike=strike,
        option_type=opt,
        action=action,
        lots=1,
        lot_size=75,
        suggested_price=price,
        suggested_price_low=price,
        suggested_price_high=price,
        leg_purpose_note="",
    )


def _pass():
    checks = [ConfidenceCheck(label=f"c{i}", status="PASS", detail="") for i in range(9)]
    return ConfidenceResult(checks=checks, failed_reasons=[], score=9, total=9, all_passed=True)


def test_executable_net_uses_bid_for_sells_and_ask_for_buys():
    legs = [_leg("SELL", "PE", 22800), _leg("BUY", "PE", 22600)]
    chain = [
        {"strike": 22800, "option_type": "PE", "bid": 40.0, "ask": 42.0},
        {"strike": 22600, "option_type": "PE", "bid": 10.0, "ask": 12.0},
    ]
    # Sell 40, buy 12 → credit 28. Mid is not used.
    assert executable_net_premium(legs, chain) == pytest.approx(28.0)


def test_missing_book_skips_executable_credit():
    legs = [_leg("SELL", "PE", 22800)]
    chain = [{"strike": 22800, "option_type": "PE", "settle_price": 40.0}]
    assert executable_net_premium(legs, chain) is None


def _with_book(chain, bid_mult, ask_mult):
    out = []
    for row in chain:
        copied = dict(row)
        mid = float(copied["settle_price"])
        copied["bid"] = round(max(mid * bid_mult, 0.05), 2)
        copied["ask"] = round(max(mid * ask_mult, 0.05), 2)
        out.append(copied)
    return out


def test_thin_executable_credit_is_not_offered(sample_chain, sample_indicators, mocker):
    from config import STRATEGY_CONFIG
    mocker.patch.dict(STRATEGY_CONFIG, {
        "min_credit_to_width_ratio": 0.0,
        "strategy_min_credit_to_width_ratio": {},
    })
    thin = _with_book(sample_chain, bid_mult=0.05, ask_mult=4.0)
    with pytest.raises(StrategyVeto, match="Executable credit too thin"):
        ss.assemble_suggestion(
            suggestion_id="S-1", underlying="NIFTY",
            expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
            spot=23000.0, chain=thin,
            indicators=sample_indicators,
            confidence=_pass(),
            iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
        )


def test_book_that_clears_the_floor_stays_offered_and_event_is_a_warning(
    sample_chain, sample_indicators, mocker,
):
    from config import STRATEGY_CONFIG
    mocker.patch.dict(STRATEGY_CONFIG, {
        "min_credit_to_width_ratio": 0.0,
        "strategy_min_credit_to_width_ratio": {},
    })
    # Bid above settle and ask below it: the touch still pays a real credit
    # after charges, so the card is kept and the event stays a warning.
    rich = []
    for row in sample_chain:
        copied = dict(row)
        mid = float(copied["settle_price"])
        copied["bid"] = round(mid + 50.0, 2)
        copied["ask"] = round(max(mid - 50.0, 0.05), 2)
        rich.append(copied)
    sug = ss.assemble_suggestion(
        suggestion_id="S-1", underlying="NIFTY",
        expiry=date(2026, 5, 14), expiry_type="Weekly", dte=14,
        spot=23000.0, chain=rich,
        indicators=sample_indicators,
        confidence=_pass(),
        iv_rank=60.0, atm_iv=0.18, lots=1, lot_size=75,
        has_event_in_hold=True,
        event_in_hold_description="RBI policy on 2026-05-10",
    )
    assert sug.strategy == "IRON_CONDOR"
    assert sug.confidence.all_passed is True
    note = next(c for c in sug.confidence.checks if c.label == "Event inside the hold")
    assert note.kind == "ADVISORY"
    assert note.status == "SOFT_FAIL"
