"""Future-scope placeholders for lifecycle/suggestion_engine.py.

Suggestion engine is the central orchestrator (~480 lines) that wires
downloader data → indicators → strategy selector → leg builder → confidence
→ DB persistence. Comprehensive integration tests require either a real DB
or a complete fake-DB harness (every repo method, all branching paths).

The engine modules underneath (strategy_selector, leg_builder, confidence,
indicators) all have unit-test coverage in tests/test_engine/. The
orchestration glue here is intentionally deferred to a future phase.
"""
from __future__ import annotations

import pytest


@pytest.mark.future
@pytest.mark.skip(reason="future: suggestion_engine integration tests "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Code Quality)")
def test_run_suggestion_engine_happy_path():
    """Full pipeline: with mocked DB returning realistic chain/indicators data,
    verify a SUG-* row is inserted with legs."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: suggestion_engine vetoes when confidence < threshold "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Code Quality)")
def test_inserts_no_suggestion_when_confidence_below_threshold():
    """When confidence soft-fails enough gates, expect a NO_SUGGESTION row
    instead of a real suggestion."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: suggestion_engine deduplication via has_suggestion_for "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Code Quality)")
def test_skips_when_existing_suggestion_for_same_entry_date():
    """If a PENDING suggestion already exists for the same underlying +
    entry_date + strategy, the engine must not insert a duplicate."""
    pass


@pytest.mark.future
@pytest.mark.skip(reason="future: trade_executor unit tests "
                  "(FUTURE_ENHANCEMENT_SCOPES.md → Code Quality)")
def test_trade_executor_records_fill_prices():
    """When the user records actual fills, trade_executor must compute
    actual_max_profit/loss based on real fill prices and persist a TRD-* row."""
    pass
