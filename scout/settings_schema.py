"""Scout UI settings — defaults, validation, and helpers."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, List, Optional, Set

from config import SCOUT_CONFIG

VALID_STRENGTHS = frozenset({"WEAK", "MEDIUM", "HIGH"})


def _default_square_off_hhmm() -> str:
    return "15:10"


def default_scout_settings() -> dict:
    """Persisted Scout settings (merged over SCOUT_CONFIG at runtime)."""
    return {
        # Automation
        "auto_execute_signals": bool(SCOUT_CONFIG.get("auto_execute_signals", False)),
        "auto_close_trades": bool(SCOUT_CONFIG.get("auto_close_trades", False)),
        "auto_close_poll_seconds": int(SCOUT_CONFIG.get("auto_close_poll_seconds", 10)),
        # Position sizing
        "use_investment_sizing": True,
        "investment_per_trade_inr": 20_000,
        "auto_trade_quantity": max(1, int(SCOUT_CONFIG.get("auto_trade_quantity", 1))),
        # Trade limits
        "max_trades_per_day": 5,
        "one_trade_per_symbol_per_day": True,
        # Auto-enter strength filter (checkboxes in UI)
        "auto_enter_strengths": ["HIGH"],
        # Auto-enter pattern filter (OR breaks recommended for cost coverage)
        "auto_enter_signal_types": ["OR_BREAK_UP", "OR_BREAK_DOWN"],
        # Profitability gates (Zerodha intraday costs)
        "min_net_profit_inr": 150.0,
        "min_net_profit_pct": 0.0,
        "min_target_r": 2.5,
        "min_risk_pct": 0.35,
        "profit_slippage_pct": 0.15,
        "profit_charge_buffer_inr": 25.0,
        "breakeven_at_r": 1.0,
        "trail_stop_r_fraction": 0.5,
        # Wallet / capital (persisted — survives deploy)
        "wallet_utilization_pct": 90.0,
        "wallet_reserve_inr": 2000.0,
        # Zerodha live execution (persisted in scout_config — survives deploy)
        "zerodha_execute_orders": False,
        "square_off_time": _default_square_off_hhmm(),
        "square_off_warn_minutes": 5,
        # Trading window (IST, HH:MM)
        "trade_window_start": "09:45",
        "trade_window_end": "14:30",
        # Signal generation
        "push_dedupe_minutes": int(SCOUT_CONFIG.get("push_dedupe_minutes", 60)),
        "dedupe_per_symbol": True,
        "signal_valid_minutes": int(SCOUT_CONFIG.get("signal_valid_minutes", 30)),
        # Pattern / filter tuning
        "max_move_from_open_pct": float(SCOUT_CONFIG.get("max_move_from_open_pct", 1.0)),
        "late_spike_from_extreme_pct": float(SCOUT_CONFIG.get("late_spike_from_extreme_pct", 0.8)),
        "rs_margin_pct": float(SCOUT_CONFIG.get("rs_margin_pct", 0.25)),
        "compression_range_pct": float(SCOUT_CONFIG.get("compression_range_pct", 0.30)),
        "entry_slippage_pct": float(SCOUT_CONFIG.get("entry_slippage_pct", 0.20)),
        "min_candles": int(SCOUT_CONFIG.get("min_candles", 12)),
    }


def merge_scout_settings(saved: Optional[dict]) -> dict:
    base = default_scout_settings()
    if not saved:
        return base
    out = {**base}
    for k in base:
        if k in saved and saved[k] is not None:
            out[k] = saved[k]
    return validate_scout_settings(out)


def _parse_hhmm(raw: str) -> time:
    parts = str(raw or "09:15").strip().split(":")
    h = int(parts[0]) if parts else 9
    m = int(parts[1]) if len(parts) > 1 else 0
    return time(max(0, min(h, 23)), max(0, min(m, 59)))


def validate_scout_settings(raw: dict) -> dict:
    d = default_scout_settings()
    src = raw if isinstance(raw, dict) else {}

    d["auto_execute_signals"] = bool(src.get("auto_execute_signals", d["auto_execute_signals"]))
    d["auto_close_trades"] = bool(src.get("auto_close_trades", d["auto_close_trades"]))
    d["auto_close_poll_seconds"] = max(
        5, min(int(src.get("auto_close_poll_seconds", d["auto_close_poll_seconds"])), 120),
    )
    d["use_investment_sizing"] = bool(src.get("use_investment_sizing", d["use_investment_sizing"]))

    inv = float(src.get("investment_per_trade_inr", d["investment_per_trade_inr"]))
    d["investment_per_trade_inr"] = max(1000.0, min(inv, 10_000_000.0))

    d["auto_trade_quantity"] = max(1, int(src.get("auto_trade_quantity", d["auto_trade_quantity"])))
    d["max_trades_per_day"] = max(0, min(int(src.get("max_trades_per_day", d["max_trades_per_day"])), 100))
    d["one_trade_per_symbol_per_day"] = bool(
        src.get("one_trade_per_symbol_per_day", d["one_trade_per_symbol_per_day"])
    )

    strengths = src.get("auto_enter_strengths", d["auto_enter_strengths"])
    if isinstance(strengths, str):
        strengths = [s.strip() for s in strengths.split(",") if s.strip()]
    if not isinstance(strengths, list):
        strengths = list(d["auto_enter_strengths"])
    cleaned: List[str] = []
    for s in strengths:
        u = str(s).upper()
        if u in VALID_STRENGTHS and u not in cleaned:
            cleaned.append(u)
    d["auto_enter_strengths"] = cleaned or list(d["auto_enter_strengths"])

    d["trade_window_start"] = str(src.get("trade_window_start", d["trade_window_start"]))[:5]
    d["trade_window_end"] = str(src.get("trade_window_end", d["trade_window_end"]))[:5]

    d["push_dedupe_minutes"] = max(5, min(int(src.get("push_dedupe_minutes", d["push_dedupe_minutes"])), 240))
    d["dedupe_per_symbol"] = bool(src.get("dedupe_per_symbol", d["dedupe_per_symbol"]))
    d["signal_valid_minutes"] = max(5, min(int(src.get("signal_valid_minutes", d["signal_valid_minutes"])), 120))

    d["max_move_from_open_pct"] = max(0.3, min(float(src.get("max_move_from_open_pct", d["max_move_from_open_pct"])), 5.0))
    d["late_spike_from_extreme_pct"] = max(0.2, min(float(src.get("late_spike_from_extreme_pct", d["late_spike_from_extreme_pct"])), 3.0))
    d["rs_margin_pct"] = max(0.0, min(float(src.get("rs_margin_pct", d["rs_margin_pct"])), 2.0))
    d["compression_range_pct"] = max(0.1, min(float(src.get("compression_range_pct", d["compression_range_pct"])), 2.0))
    d["entry_slippage_pct"] = max(0.05, min(float(src.get("entry_slippage_pct", d["entry_slippage_pct"])), 1.0))
    d["min_candles"] = max(5, min(int(src.get("min_candles", d["min_candles"])), 60))

    types = src.get("auto_enter_signal_types", d["auto_enter_signal_types"])
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    if not isinstance(types, list):
        types = list(d["auto_enter_signal_types"])
    from scout.profit_gate import VALID_AUTO_SIGNAL_TYPES
    cleaned_types: List[str] = []
    for t in types:
        u = str(t).upper()
        if u in VALID_AUTO_SIGNAL_TYPES and u not in cleaned_types:
            cleaned_types.append(u)
    d["auto_enter_signal_types"] = cleaned_types or list(d["auto_enter_signal_types"])

    d["min_net_profit_inr"] = max(0.0, min(float(src.get("min_net_profit_inr", d["min_net_profit_inr"])), 50_000.0))
    d["min_net_profit_pct"] = max(0.0, min(float(src.get("min_net_profit_pct", d["min_net_profit_pct"])), 0.05))
    d["min_target_r"] = max(1.0, min(float(src.get("min_target_r", d["min_target_r"])), 5.0))
    d["min_risk_pct"] = max(0.0, min(float(src.get("min_risk_pct", d["min_risk_pct"])), 2.0))
    d["profit_slippage_pct"] = max(0.0, min(float(src.get("profit_slippage_pct", d["profit_slippage_pct"])), 1.0))
    d["profit_charge_buffer_inr"] = max(0.0, min(float(src.get("profit_charge_buffer_inr", d["profit_charge_buffer_inr"])), 500.0))
    d["breakeven_at_r"] = max(0.5, min(float(src.get("breakeven_at_r", d["breakeven_at_r"])), 3.0))
    d["trail_stop_r_fraction"] = max(0.0, min(float(src.get("trail_stop_r_fraction", d["trail_stop_r_fraction"])), 2.0))

    d["zerodha_execute_orders"] = bool(
        src.get("zerodha_execute_orders", d["zerodha_execute_orders"])
    )
    d["square_off_time"] = str(src.get("square_off_time", d["square_off_time"]))[:5]
    d["square_off_warn_minutes"] = max(
        1, min(int(src.get("square_off_warn_minutes", d["square_off_warn_minutes"])), 30),
    )
    d["wallet_utilization_pct"] = max(
        50.0, min(float(src.get("wallet_utilization_pct", d["wallet_utilization_pct"])), 100.0),
    )
    d["wallet_reserve_inr"] = max(
        0.0, min(float(src.get("wallet_reserve_inr", d["wallet_reserve_inr"])), 1_000_000.0),
    )

    # Ensure window start <= end (same-day intraday)
    if _parse_hhmm(d["trade_window_start"]) > _parse_hhmm(d["trade_window_end"]):
        d["trade_window_start"], d["trade_window_end"] = "09:45", "14:30"

    return d


def format_square_off_time(settings: Optional[dict] = None) -> str:
    s = settings or default_scout_settings()
    raw = str(s.get("square_off_time") or "15:10")[:5]
    return f"{raw} IST"


def square_off_datetime(day: datetime, settings: Optional[dict] = None) -> datetime:
    """IST square-off moment on `day` from persisted settings."""
    s = settings or default_scout_settings()
    t = _parse_hhmm(str(s.get("square_off_time") or "15:10"))
    return day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def effective_pattern_config(settings: Optional[dict] = None) -> dict:
    """SCOUT_CONFIG merged with persisted settings for pattern/filter code."""
    merged = dict(SCOUT_CONFIG)
    s = settings or default_scout_settings()
    for key in (
        "max_move_from_open_pct",
        "late_spike_from_extreme_pct",
        "rs_margin_pct",
        "compression_range_pct",
        "compression_bars",
        "or_minutes",
        "min_candles",
        "signal_valid_minutes",
        "entry_slippage_pct",
        "push_dedupe_minutes",
    ):
        if key in s:
            merged[key] = s[key]
    return merged


def in_trading_window(settings: dict, now: Optional[datetime] = None) -> bool:
    from utils import now_ist

    cur = (now or now_ist()).time()
    start = _parse_hhmm(settings.get("trade_window_start", "09:45"))
    end = _parse_hhmm(settings.get("trade_window_end", "14:30"))
    return start <= cur <= end


def strength_allowed(settings: dict, strength: Optional[str]) -> bool:
    allowed: Set[str] = {str(s).upper() for s in settings.get("auto_enter_strengths") or []}
    return str(strength or "WEAK").upper() in allowed


def compute_trade_quantity(settings: dict, ltp: float) -> int:
    if settings.get("use_investment_sizing", True):
        inv = float(settings.get("investment_per_trade_inr", 20_000))
        px = float(ltp or 0)
        if px > 0:
            return max(1, int(inv // px))
    return max(1, int(settings.get("auto_trade_quantity", 1)))


def suggested_quantity(settings: dict, ltp: Optional[float]) -> int:
    px = float(ltp or 0)
    if px <= 0:
        return max(1, int(settings.get("auto_trade_quantity", 1)))
    return compute_trade_quantity(settings, px)
