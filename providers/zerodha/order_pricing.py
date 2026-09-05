"""
Limit-price helpers for Zerodha leg orders — bid/ask reference and retry walk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import ZERODHA_EXECUTION_CONFIG
from providers.zerodha.instruments import Instrument


@dataclass(frozen=True)
class ExecutionProfile:
    """Pricing / retry / validity profile for a leg placement context."""

    name: str
    slippage_pct: float
    slip_walk_per_retry: float
    max_retries: int
    validity: str
    order_type: str = "LIMIT"
    allow_market_fallback: bool = False


def profile_for(mode: str) -> ExecutionProfile:
    cfg = ZERODHA_EXECUTION_CONFIG
    m = str(mode or "entry").lower()
    base_slip = float(cfg.get("limit_slippage_pct", 0.5))
    walk = float(cfg.get("limit_slip_walk_per_retry", 0.25))
    if m == "rollback":
        return ExecutionProfile(
            name="rollback",
            slippage_pct=float(cfg.get("rollback_limit_slippage_pct", 1.0)),
            slip_walk_per_retry=walk * 2,
            max_retries=int(cfg.get("rollback_max_retries", 2)),
            validity=str(cfg.get("order_validity_rollback", "IOC")),
            allow_market_fallback=bool(cfg.get("rollback_allow_market", True)),
        )
    if m in ("close", "exit"):
        return ExecutionProfile(
            name="close",
            slippage_pct=base_slip,
            slip_walk_per_retry=walk,
            max_retries=int(cfg.get("order_max_retries", 3)),
            validity=str(cfg.get("order_validity_close", "DAY")),
            allow_market_fallback=bool(cfg.get("close_allow_market", False)),
        )
    return ExecutionProfile(
        name="entry",
        slippage_pct=base_slip,
        slip_walk_per_retry=walk,
        max_retries=int(cfg.get("order_max_retries", 3)),
        validity=str(cfg.get("order_validity_entry", "DAY")),
        allow_market_fallback=False,
    )


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    steps = round(price / tick)
    return round(steps * tick, 2)


def _depth_price(quote_row: dict, txn: str) -> Optional[float]:
    depth = quote_row.get("depth") or {}
    txn = txn.upper()
    if txn == "BUY":
        sells = depth.get("sell") or []
        if sells and sells[0].get("price"):
            return float(sells[0]["price"])
        ask = quote_row.get("ask") or quote_row.get("sell_price")
        return float(ask) if ask is not None else None
    buys = depth.get("buy") or []
    if buys and buys[0].get("price"):
        return float(buys[0]["price"])
    bid = quote_row.get("bid") or quote_row.get("buy_price")
    return float(bid) if bid is not None else None


def reference_price(
    *,
    ltp: float,
    quote_row: Optional[dict],
    transaction_type: str,
    use_bid_ask: bool,
) -> float:
    txn = transaction_type.upper()
    if use_bid_ask and quote_row:
        dp = _depth_price(quote_row, txn)
        if dp is not None and dp > 0:
            return dp
    return ltp


def limit_from_reference(
    ref: float,
    transaction_type: str,
    inst: Instrument,
    *,
    slippage_pct: float,
    attempt: int = 0,
    slip_walk_per_retry: float = 0.0,
    user_limit: Optional[float] = None,
) -> float:
    if user_limit is not None:
        px = float(user_limit)
        if px <= 0:
            raise ValueError(f"leg limit price must be positive (got {user_limit})")
        return round_to_tick(px, inst.tick_size)
    slip = (slippage_pct + slip_walk_per_retry * attempt) / 100.0
    txn = transaction_type.upper()
    if txn == "BUY":
        px = ref * (1.0 + slip)
    else:
        px = ref * (1.0 - slip)
    return round_to_tick(px, inst.tick_size)


def fetch_quote_map(facade: Any, keys: list) -> Dict[str, dict]:
    if not keys:
        return {}
    try:
        raw = facade.quote(list(keys))
    except Exception:
        return {}
    out: Dict[str, dict] = {}
    for key in keys:
        row = raw.get(key)
        if isinstance(row, dict):
            out[key] = row
    return out
