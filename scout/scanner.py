"""Scan watchlist symbols and persist signals."""

from __future__ import annotations

import logging
from typing import List, Tuple

from scout.filters import min_candles_ok
from scout.market_data import ScoutMarketData, ScoutMarketError
from scout.patterns import detect_signals
from scout.utils import pct_change

logger = logging.getLogger(__name__)


def scan_watchlist(mkt: ScoutMarketData) -> Tuple[List[dict], int]:
    """Return (signal dicts with symbol, symbols_scanned count)."""
    bench_pct = mkt.benchmark_pct_from_open()
    out: List[dict] = []
    symbols = mkt.watchlist()
    for symbol in symbols:
        try:
            candles, stats = mkt.minute_bars(symbol)
            if not min_candles_ok(candles):
                continue
            open_px = stats["open"]
            ltp = stats["ltp"]
            stock_pct = pct_change(open_px, ltp)
            sigs = detect_signals(
                candles,
                open_px=open_px,
                day_high=stats["high"],
                day_low=stats["low"],
                stock_pct=stock_pct,
                bench_pct=bench_pct,
            )
            for sig in sigs:
                row = sig.to_dict()
                row["symbol"] = symbol
                row["stock_pct_from_open"] = round(stock_pct, 3)
                row["nifty_pct_from_open"] = round(bench_pct, 3)
                out.append(row)
        except ScoutMarketError as exc:
            logger.warning("Scout skip %s: %s", symbol, exc)
        except Exception:
            logger.exception("Scout error scanning %s", symbol)
    return out, len(symbols)
