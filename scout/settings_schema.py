"""Scout UI settings — defaults, validation, and helpers."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, List, Optional, Set

from config import SCOUT_CONFIG

VALID_STRENGTHS = frozenset({"WEAK", "MEDIUM", "HIGH"})


def default_scout_settings() -> dict:
    """Persisted Scout settings (merged over SCOUT_CONFIG at runtime)."""
    return {
        # Automation
        "auto_execute_signals": bool(SCOUT_CONFIG.get("auto_execute_signals", False)),
        "auto_close_trades": bool(SCOUT_CONFIG.get("auto_close_trades", False)),
        # Position sizing
        "use_investment_sizing": True,
        "investment_per_trade_inr": 20_000,
        "auto_trade_quantity": max(1, int(SCOUT_CONFIG.get("auto_trade_quantity", 1))),
        # Trade limits
        "max_trades_per_day": 5,
        "one_trade_per_symbol_per_day": True,
        # Auto-enter strength filter (checkboxes in UI)
        "auto_enter_strengths": ["MEDIUM", "HIGH"],
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

    # Ensure window start <= end (same-day intraday)
    if _parse_hhmm(d["trade_window_start"]) > _parse_hhmm(d["trade_window_end"]):
        d["trade_window_start"], d["trade_window_end"] = "09:45", "14:30"

    return d


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
