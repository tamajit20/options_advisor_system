"""providers/nse_eod — adapter that serves settled bhavcopy data from the
existing SQL Server tables (`options_fo_eod`, `options_spot_eod`,
`options_vix_history`) via the existing repos in `database/models.py`.

This is the always-available baseline provider. It is also used as the
fallback when a live provider (Zerodha) is unavailable or out of market
hours."""

from __future__ import annotations

from .provider import NseEodProvider

__all__ = ["NseEodProvider"]
