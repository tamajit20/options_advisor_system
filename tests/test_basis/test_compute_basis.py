"""Unit tests for compute_basis."""

from __future__ import annotations

from datetime import date

import pytest

from basis.basis_engine import compute_basis


def test_compute_basis_formula():
    spot, fut = 2500.0, 2512.5
    expiry = date(2026, 8, 28)
    as_of = date(2026, 8, 17)
    basis_abs, basis_pct, ann_pct, direction = compute_basis(spot, fut, expiry, as_of=as_of)

    assert basis_abs == pytest.approx(12.5)
    assert basis_pct == pytest.approx(0.5)
    assert direction == "CONTANGO"
    dte = max((expiry - as_of).days, 1)
    assert ann_pct == pytest.approx(basis_pct * 365 / dte)
