"""
tests/conftest.py
=================

Shared fixtures for the unit + integration suites.

Adds the repo root to sys.path so `import engine`, `import config`, etc. work
without an installed package.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Mapping
from unittest.mock import MagicMock

import pytest

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Date / time fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def trade_date() -> date:
    return date(2026, 4, 30)


@pytest.fixture
def expiry_date() -> date:
    """Default expiry — 14 days after trade_date (mid-DTE band)."""
    return date(2026, 5, 14)


# ---------------------------------------------------------------------------
# Synthetic option chain
# ---------------------------------------------------------------------------
def _build_chain(
    spot: float = 23000.0,
    step: float = 50.0,
    width: int = 30,
    expiry: date = date(2026, 5, 14),
    base_iv: float = 0.18,
) -> List[dict]:
    """Build a deterministic synthetic NIFTY-style option chain.

    `width` strikes on each side of ATM. Prices are *not* arbitrage-free
    Black-Scholes — they're a smooth approximation that's good enough for
    testing strike-selection / pop / charges logic without scipy.
    """
    rows: List[dict] = []
    atm = round(spot / step) * step
    import math
    dte = max((expiry - date(2026, 4, 30)).days, 1)
    t = dte / 365.0
    for i in range(-width, width + 1):
        strike = atm + i * step
        moneyness = (strike - spot) / spot
        # Cheap pseudo-prices: intrinsic + time-value bell curve
        time_val = max(spot * base_iv * math.sqrt(t) * math.exp(-(moneyness * 8) ** 2), 0.5)
        ce_intrinsic = max(spot - strike, 0.0)
        pe_intrinsic = max(strike - spot, 0.0)
        ce_price = round(ce_intrinsic + time_val, 2)
        pe_price = round(pe_intrinsic + time_val, 2)
        # OI: Gaussian around ATM with bias on each side
        oi = int(max(50000 * math.exp(-(i / 8) ** 2), 100))
        rows.append({
            "strike": strike, "option_type": "CE",
            "settle_price": ce_price, "close_price": ce_price,
            "open_interest": oi, "contracts": oi // 10,
            "expiry_date": expiry,
        })
        rows.append({
            "strike": strike, "option_type": "PE",
            "settle_price": pe_price, "close_price": pe_price,
            "open_interest": oi + 5000, "contracts": (oi + 5000) // 10,
            "expiry_date": expiry,
        })
    return rows


@pytest.fixture
def sample_chain(expiry_date: date) -> List[dict]:
    return _build_chain(spot=23000.0, expiry=expiry_date)


@pytest.fixture
def sample_spot() -> float:
    return 23000.0


# ---------------------------------------------------------------------------
# Synthetic spot history (60 days) — long enough for SMA50 + ADX
# ---------------------------------------------------------------------------
@pytest.fixture
def spot_history_sideways(trade_date: date) -> List[dict]:
    """Sideways: oscillating around 23000."""
    import math
    out = []
    for i in range(60):
        d = trade_date - timedelta(days=60 - i)
        c = 23000 + 50 * math.sin(i / 3.0)
        out.append({
            "trade_date": d,
            "open_price": c, "high_price": c + 80,
            "low_price": c - 80, "close_price": c,
        })
    return out


@pytest.fixture
def spot_history_bullish(trade_date: date) -> List[dict]:
    """Bullish: linear uptrend 22000 → 23200 with noise."""
    import math
    out = []
    for i in range(60):
        d = trade_date - timedelta(days=60 - i)
        c = 22000 + i * 20 + 30 * math.sin(i / 2.0)
        out.append({
            "trade_date": d,
            "open_price": c, "high_price": c + 50,
            "low_price": c - 50, "close_price": c,
        })
    return out


@pytest.fixture
def spot_history_bearish(trade_date: date) -> List[dict]:
    """Bearish: linear downtrend 23200 → 22000."""
    import math
    out = []
    for i in range(60):
        d = trade_date - timedelta(days=60 - i)
        c = 23200 - i * 20 + 30 * math.sin(i / 2.0)
        out.append({
            "trade_date": d,
            "open_price": c, "high_price": c + 50,
            "low_price": c - 50, "close_price": c,
        })
    return out


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_indicators(trade_date: date):
    """A neutral, all-passing indicators bundle for confidence/strategy tests."""
    from contracts import MarketIndicators
    return MarketIndicators(
        symbol="NIFTY",
        as_of=trade_date,
        spot=23000.0,
        pcr=1.0,
        max_pain=23000.0,
        atr_14=200.0,
        trend="SIDEWAYS",
        vix_close=15.0,
        vix_regime="STABLE",
        oi_walls_call=[23200.0, 23300.0, 23400.0],
        oi_walls_put=[22800.0, 22700.0, 22600.0],
        expected_move=300.0,
        hv_20=0.16,
        iv_premium=1.10,
        fii_net_futures=10000.0,
        adx_14=25.0,
        sma20_slope_pct=0.10,
        sma_diff_pct=0.10,
    )


# ---------------------------------------------------------------------------
# DB mock — pyodbc-style
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_db():
    """A MagicMock SQLServerConnection. Configure return values per-test:

        mock_db.fetch_all.return_value = [{"id": 1, ...}, ...]
        mock_db.fetch_one.return_value = {"id": 1, ...}
    """
    db = MagicMock()
    # execute() returns a cursor-like mock (some repos call .close() on it)
    cursor = MagicMock()
    cursor.rowcount = 0
    cursor.close = MagicMock(return_value=None)
    db.execute = MagicMock(return_value=cursor)
    db.executemany = MagicMock(return_value=0)
    db.fetch_all = MagicMock(return_value=[])
    db.fetch_one = MagicMock(return_value=None)
    db.scalar = MagicMock(return_value=None)
    db.commit = MagicMock(return_value=None)
    db.rollback = MagicMock(return_value=None)
    db.close = MagicMock(return_value=None)
    db._cursor = cursor  # exposed for test inspection
    return db


# ---------------------------------------------------------------------------
# Helper to build a simple SuggestionLeg (used by economics tests)
# ---------------------------------------------------------------------------
@pytest.fixture
def make_leg():
    from contracts import SuggestionLeg

    def _make(
        leg_order: int, strike: float, option_type: str, action: str,
        price: float = 100.0, lots: int = 1, lot_size: int = 75,
        expiry: date = date(2026, 5, 14),
    ) -> SuggestionLeg:
        return SuggestionLeg(
            leg_order=leg_order,
            hedge_pair_leg=None,
            symbol="NIFTY",
            expiry_date=expiry,
            strike=strike,
            option_type=option_type,
            action=action,
            lots=lots,
            lot_size=lot_size,
            suggested_price=price,
            suggested_price_low=round(price * 0.98, 2),
            suggested_price_high=round(price * 1.02, 2),
            leg_purpose_note="test",
        )
    return _make
