"""Future-scope: unused suggestion signals must be validated before wiring.

See FUTURE_ENHANCEMENT_SCOPES.md → Strategy & Regime Coverage
→ Unused suggestion signals — validate before any implementation.
"""
from __future__ import annotations

import pytest


@pytest.mark.future
@pytest.mark.skip(
    reason="future: unused suggestion signals — validate vs closed P&L "
           "one-by-one before any gate "
           "(FUTURE_ENHANCEMENT_SCOPES.md → Strategy)",
)
def test_unused_signals_not_wired_until_validated():
    """IV percentile, FII option OI, max-pain distance, volume burst, and
    smile/risk-reversal stay off confidence and strategy_selector until each
    is validated on a larger closed-trade sample (credit + debit, FII writing
    and buying days). Never ship as a hard FAIL. One signal at a time; display
    or SOFT_FAIL first."""
    pass
