"""
tests/test_lifecycle/test_intraday_validator.py
================================================

09:35 IST opening-bell validator (lifecycle/intraday_validator.py).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

import lifecycle.intraday_validator as iv


_TODAY = date(2026, 5, 4)
_EXPIRY = date(2026, 5, 14)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pending_suggestion(
    sid: str = "SUG-1",
    underlying: str = "NIFTY",
    net_credit: float = 28.0,
    trade_name: str = "NIFTY Bull Put 23200/23100",
) -> dict:
    return {
        "suggestion_id": sid,
        "underlying": underlying,
        "expiry_date": _EXPIRY,
        "entry_date": _TODAY,
        "status": "PENDING",
        "net_credit_suggested": net_credit,
        "trade_name": trade_name,
    }


def _legs_short_strangle() -> list[dict]:
    """Two SELL legs → live credit = sum of mids (positive)."""
    return [
        {"leg_order": 1, "symbol": "NIFTY", "expiry_date": _EXPIRY,
         "strike": 23200.0, "option_type": "PE", "action": "SELL"},
        {"leg_order": 2, "symbol": "NIFTY", "expiry_date": _EXPIRY,
         "strike": 23800.0, "option_type": "CE", "action": "SELL"},
    ]


def _chain_at(prices: dict[tuple[float, str], float]) -> list[dict]:
    return [
        {"strike": k[0], "option_type": k[1], "last_price": v,
         "_source": "LIVE", "_provider": "zerodha", "_freshness_ms": 200}
        for k, v in prices.items()
    ]


@pytest.fixture
def patched(mocker, mock_db):
    """Patch SuggestionRepo.legs and capture the notifier."""
    legs = _legs_short_strangle()
    mocker.patch.object(iv.SuggestionRepo, "legs", return_value=legs)
    update_status = mocker.patch.object(iv.SuggestionRepo, "update_status")
    notifier = MagicMock()
    notifier.notify = MagicMock()
    return {
        "db": mock_db,
        "legs": legs,
        "update_status": update_status,
        "notifier": notifier,
    }


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------
class TestEmpty:
    def test_no_pending_returns_zero(self, mock_db, patched):
        mock_db.fetch_all.return_value = []
        provider = MagicMock()
        n = iv.run_intraday_validator(
            mock_db, _TODAY, provider=provider, notifier=patched["notifier"],
        )
        assert n == 0
        provider.get_chain.assert_not_called()
        patched["notifier"].notify.assert_not_called()


# ---------------------------------------------------------------------------
# STILL_GOOD path
# ---------------------------------------------------------------------------
class TestStillGood:
    def test_credit_within_tolerance_marks_still_good(self, mock_db, patched):
        # Suggested credit 28.0; live credit 28.5 → 1.8% change → within 15%.
        mock_db.fetch_all.return_value = [_pending_suggestion(net_credit=28.0)]
        provider = MagicMock()
        provider.get_chain.return_value = _chain_at({
            (23200.0, "PE"): 12.0,
            (23800.0, "CE"): 16.5,
        })

        n = iv.run_intraday_validator(
            mock_db, _TODAY, provider=provider, notifier=patched["notifier"],
        )
        assert n == 1

        # Status flipped to STILL_GOOD_0935 via UPDATE
        sql_calls = [c.args[0] for c in mock_db.execute.call_args_list]
        params  = [c.args[1] for c in mock_db.execute.call_args_list]
        assert any("validator_status" in s for s in sql_calls)
        assert any("STILL_GOOD_0935" in p for p in params)

        # Suggestion stays PENDING — update_status NOT called
        patched["update_status"].assert_not_called()

        # Notification: SUGGESTION_STILL_GOOD INFO
        patched["notifier"].notify.assert_called_once()
        kwargs = patched["notifier"].notify.call_args
        assert kwargs.args[0] == "SUGGESTION_STILL_GOOD"
        assert kwargs.args[1] == "INFO"
        assert kwargs.kwargs["related_suggestion_id"] == "SUG-1"


# ---------------------------------------------------------------------------
# STALE path
# ---------------------------------------------------------------------------
class TestStale:
    def test_credit_outside_tolerance_marks_stale_and_ignored(self, mock_db, patched):
        # Suggested 28.0; live 18.0 → 35.7% drop → outside 15% band.
        mock_db.fetch_all.return_value = [_pending_suggestion(net_credit=28.0)]
        provider = MagicMock()
        provider.get_chain.return_value = _chain_at({
            (23200.0, "PE"): 8.0,
            (23800.0, "CE"): 10.0,
        })

        n = iv.run_intraday_validator(
            mock_db, _TODAY, provider=provider, notifier=patched["notifier"],
        )
        assert n == 1

        params = [c.args[1] for c in mock_db.execute.call_args_list]
        assert any("STALE_0935" in p for p in params)

        # Status flipped to IGNORED
        patched["update_status"].assert_called_once_with("SUG-1", "IGNORED")

        # Notification: SUGGESTION_STALE WARNING
        patched["notifier"].notify.assert_called_once()
        call = patched["notifier"].notify.call_args
        assert call.args[0] == "SUGGESTION_STALE"
        assert call.args[1] == "WARNING"


# ---------------------------------------------------------------------------
# NOT_VALIDATED path (provider failure / missing data)
# ---------------------------------------------------------------------------
class TestNotValidated:
    def test_provider_raises_marks_not_validated_no_status_flip(
        self, mock_db, patched
    ):
        mock_db.fetch_all.return_value = [_pending_suggestion()]
        provider = MagicMock()
        provider.get_chain.side_effect = RuntimeError("kite session expired")

        n = iv.run_intraday_validator(
            mock_db, _TODAY, provider=provider, notifier=patched["notifier"],
        )
        assert n == 1

        params = [c.args[1] for c in mock_db.execute.call_args_list]
        assert any("NOT_VALIDATED" in p for p in params)

        # Fail-open: do NOT flip the suggestion to IGNORED
        patched["update_status"].assert_not_called()
        # Fail-open: no user-facing notification on infra failure
        patched["notifier"].notify.assert_not_called()

    def test_empty_chain_marks_not_validated(self, mock_db, patched):
        mock_db.fetch_all.return_value = [_pending_suggestion()]
        provider = MagicMock()
        provider.get_chain.return_value = []

        n = iv.run_intraday_validator(
            mock_db, _TODAY, provider=provider, notifier=patched["notifier"],
        )
        assert n == 1
        params = [c.args[1] for c in mock_db.execute.call_args_list]
        assert any("NOT_VALIDATED" in p for p in params)
        patched["update_status"].assert_not_called()

    def test_missing_leg_in_chain_marks_not_validated(self, mock_db, patched):
        # Only one of the two legs returned in the chain
        mock_db.fetch_all.return_value = [_pending_suggestion()]
        provider = MagicMock()
        provider.get_chain.return_value = _chain_at({
            (23200.0, "PE"): 12.0,
            # 23800 CE missing
        })

        n = iv.run_intraday_validator(
            mock_db, _TODAY, provider=provider, notifier=patched["notifier"],
        )
        assert n == 1
        params = [c.args[1] for c in mock_db.execute.call_args_list]
        assert any("NOT_VALIDATED" in p for p in params)
        patched["update_status"].assert_not_called()


# ---------------------------------------------------------------------------
# Per-suggestion isolation
# ---------------------------------------------------------------------------
class TestMultipleSuggestions:
    def test_one_stale_one_still_good(self, mock_db, patched):
        mock_db.fetch_all.return_value = [
            _pending_suggestion(sid="SUG-A", net_credit=28.0),
            _pending_suggestion(sid="SUG-B", net_credit=28.0),
        ]
        provider = MagicMock()
        # First call: STILL_GOOD chain. Second: STALE chain.
        provider.get_chain.side_effect = [
            _chain_at({(23200.0, "PE"): 12.0, (23800.0, "CE"): 16.5}),
            _chain_at({(23200.0, "PE"): 5.0,  (23800.0, "CE"): 6.0}),
        ]

        n = iv.run_intraday_validator(
            mock_db, _TODAY, provider=provider, notifier=patched["notifier"],
        )
        assert n == 2

        notify_types = [c.args[0] for c in patched["notifier"].notify.call_args_list]
        assert notify_types == ["SUGGESTION_STILL_GOOD", "SUGGESTION_STALE"]
        patched["update_status"].assert_called_once_with("SUG-B", "IGNORED")


# ---------------------------------------------------------------------------
# Internal helper — _evaluate_one
# ---------------------------------------------------------------------------
class TestEvaluateOne:
    def test_zero_suggested_credit_returns_not_validated(self, mock_db):
        sug = _pending_suggestion(net_credit=0.0)
        legs = _legs_short_strangle()
        provider = MagicMock()
        provider.get_chain.return_value = _chain_at({
            (23200.0, "PE"): 12.0,
            (23800.0, "CE"): 16.5,
        })
        status, _ = iv._evaluate_one(
            mock_db, sug, legs,
            trade_date=_TODAY, provider=provider, tolerance_pct=15.0,
        )
        assert status == "NOT_VALIDATED"

    def test_exactly_at_band_edge_is_still_good(self, mock_db):
        # Suggested 100; tolerance 15% → 115 is exactly the upper edge.
        sug = _pending_suggestion(net_credit=100.0)
        legs = [
            {"leg_order": 1, "symbol": "NIFTY", "expiry_date": _EXPIRY,
             "strike": 100.0, "option_type": "PE", "action": "SELL"},
        ]
        provider = MagicMock()
        provider.get_chain.return_value = [
            {"strike": 100.0, "option_type": "PE", "last_price": 115.0},
        ]
        status, summary = iv._evaluate_one(
            mock_db, sug, legs,
            trade_date=_TODAY, provider=provider, tolerance_pct=15.0,
        )
        assert status == "STILL_GOOD_0935"
        assert "+15.00%" in summary
