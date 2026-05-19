"""Helpers to merge stock + index spot rows without clobbering real OHLC."""

from __future__ import annotations

from typing import Dict, List

from contracts import SpotBhavRow


def _row_has_real_ohlc(r: SpotBhavRow) -> bool:
    if r.close_price <= 0:
        return False
    return (r.high_price - r.low_price) > max(r.close_price * 1e-6, 0.01)


def merge_spot_bhav_rows(
    stock_rows: List[SpotBhavRow],
    index_rows: List[SpotBhavRow],
    fo_settle: Dict[str, float],
    trade_date,
) -> List[SpotBhavRow]:
    """Priority: NSE index OHLC > cash stock row > FO settle-only fallback."""
    by_sym: Dict[str, SpotBhavRow] = {r.symbol: r for r in stock_rows}
    for r in index_rows:
        by_sym[r.symbol] = r
    for sym, px in fo_settle.items():
        existing = by_sym.get(sym)
        if existing is not None and _row_has_real_ohlc(existing):
            continue
        by_sym[sym] = SpotBhavRow(
            trade_date=trade_date,
            symbol=sym,
            open_price=px,
            high_price=px,
            low_price=px,
            close_price=px,
            volume=0,
        )
    return list(by_sym.values())
