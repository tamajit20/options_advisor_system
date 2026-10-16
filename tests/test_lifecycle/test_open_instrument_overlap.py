"""Open-trade contracts that a new suggestion would touch again."""

from datetime import date

from lifecycle.open_instrument_overlap import (
    contract_key,
    load_open_contracts,
    overlaps_for_legs,
)


def _open(trade_id="TRD-1", suggestion_id="SUG-OLD", action="SELL", strike=25000):
    return {
        "trade_id": trade_id,
        "trade_name": "Nifty put spread",
        "suggestion_id": suggestion_id,
        "symbol": "NIFTY",
        "expiry_date": date(2026, 10, 27),
        "strike": strike,
        "option_type": "PE",
        "action": action,
    }


def _leg(action="BUY", strike=25000):
    return {
        "symbol": "nifty",
        "expiry_date": "2026-10-27",
        "strike": strike,
        "option_type": "pe",
        "action": action,
    }


def test_matching_contract_is_flagged_even_when_the_side_differs():
    hits = overlaps_for_legs([_open()], [_leg()])
    assert len(hits) == 1
    assert hits[0]["trade_id"] == "TRD-1"
    assert hits[0]["label"] == "NIFTY 2026-10-27 25000 PE"
    assert hits[0]["open_action"] == "SELL"
    assert hits[0]["suggestion_action"] == "BUY"
    assert hits[0]["strike"] == "25000"


def test_different_strike_is_not_an_overlap():
    assert overlaps_for_legs([_open(strike=25000)], [_leg(strike=25100)]) == []


def test_same_suggestion_does_not_warn_about_its_own_trade():
    assert overlaps_for_legs(
        [_open(suggestion_id="SUG-1")],
        [_leg()],
        exclude_suggestion_id="SUG-1",
    ) == []


def test_duplicate_legs_on_one_trade_are_listed_once():
    hits = overlaps_for_legs([_open(), _open()], [_leg(), _leg(action="SELL")])
    assert len(hits) == 1


def test_contract_key_normalises_strike_and_case():
    assert contract_key("nifty", date(2026, 10, 27), 25000.0, "ce") == (
        "NIFTY", "2026-10-27", "25000", "CE",
    )


def test_load_open_contracts_ignores_a_non_list():
    class _Db:
        def fetch_all(self, sql):
            return None

    assert load_open_contracts(_Db()) == []
