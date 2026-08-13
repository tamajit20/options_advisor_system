"""Auto-enter readiness — all checks exposed for UI and polling."""

from __future__ import annotations

from typing import List, Optional

from config import SCOUT_CONFIG
from scout.entry_pricing import entry_limit_price
from scout.profit_gate import effective_min_net_profit, entry_profit_block_reason, signal_type_allowed
from scout.settings_schema import (
    compute_trade_quantity,
    in_trading_window,
    strength_allowed,
)
from scout.utils import is_market_open


def _check(
    check_id: str,
    label: str,
    ok: bool,
    *,
    detail: str = "",
) -> dict:
    return {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "detail": detail or "",
    }


def evaluate_auto_enter_status(
    *,
    signal: dict,
    enriched: dict,
    settings: dict,
    trade_repo,
    market_open: Optional[bool] = None,
    has_open_trade: bool = False,
    symbol_trade_blocked: bool = False,
) -> dict:
    """Full auto-enter checklist for one signal (API + UI)."""
    from scout.auto_trader import _auto_enter_block_reason, _symbol_has_other_open_trade

    sid = signal.get("id")
    try:
        signal_id = int(sid) if sid is not None else 0
    except (TypeError, ValueError):
        signal_id = 0

    sym = str(signal.get("symbol") or "").upper()
    strength = str(signal.get("strength") or "WEAK")
    signal_type = str(signal.get("signal_type") or "")
    automation_on = bool(settings.get("auto_execute_signals"))
    scout_enabled = bool(SCOUT_CONFIG.get("enabled", True))
    mkt_open = is_market_open() if market_open is None else bool(market_open)

    live_ltp = enriched.get("live_ltp")
    if live_ltp is None or float(live_ltp or 0) <= 0:
        live_ltp = float(signal.get("ltp") or 0)
    else:
        live_ltp = float(live_ltp)

    validity = str(enriched.get("validity_status") or "")
    entry_min = float(enriched.get("entry_min") or 0)
    entry_max = float(enriched.get("entry_max") or 0)
    inv = signal.get("invalidation")
    action = str(signal.get("action") or "").upper()

    band_ok = None
    if live_ltp > 0 and entry_min > 0 and entry_max > 0:
        band_ok = entry_min <= live_ltp <= entry_max

    stop_ok = None
    if inv is not None and live_ltp > 0:
        inv_f = float(inv)
        stop_ok = live_ltp >= inv_f if action == "BUY" else live_ltp <= inv_f

    max_day = int(settings.get("max_trades_per_day") or 0)
    try:
        trades_today = int(trade_repo.count_trades_opened_today())
    except (TypeError, ValueError):
        trades_today = 0
    daily_ok = max_day <= 0 or trades_today < max_day

    symbol_day_ok = True
    if settings.get("one_trade_per_symbol_per_day"):
        symbol_day_ok = not trade_repo.symbol_has_trade_today(sym)

    symbol_open_ok = True
    if signal_id > 0:
        symbol_open_ok = not _symbol_has_other_open_trade(trade_repo, sym, signal_id)

    strength_ok = strength_allowed(settings, strength)
    allowed_strengths = ", ".join(settings.get("auto_enter_strengths") or [])
    pattern_ok = signal_type_allowed(settings, signal_type)
    allowed_patterns = ", ".join(settings.get("auto_enter_signal_types") or []) or "any"

    window_ok = in_trading_window(settings)
    window_detail = (
        f"{settings.get('trade_window_start', '09:45')}–"
        f"{settings.get('trade_window_end', '14:30')} IST"
    )

    entry_px = entry_limit_price(enriched, signal, live_ltp if live_ltp > 0 else None)
    qty = compute_trade_quantity(settings, entry_px if entry_px > 0 else live_ltp) if live_ltp > 0 else 0
    profit_detail = ""
    profit_ok = True
    if entry_px > 0 and qty > 0:
        profit_block = entry_profit_block_reason(
            signal=signal,
            entry=entry_px,
            qty=qty,
            settings=settings,
        )
        if profit_block:
            profit_ok = False
            profit_detail = profit_block
        else:
            from scout.signal_enrichment import build_exit_plan

            plan = build_exit_plan(signal, entry_price=entry_px, settings=settings)
            target = plan.get("target_price")
            min_net = effective_min_net_profit(settings, notional=entry_px * qty)
            profit_detail = (
                f"≥ ₹{min_net:.0f} net at ₹{float(target or 0):.2f} "
                f"({plan.get('target_r', 2)}R) · entry ₹{entry_px:.2f}"
            )
    elif live_ltp <= 0:
        profit_ok = False
        profit_detail = "no live price for profit estimate"

    wallet_ok = True
    wallet_detail = ""
    if settings.get("zerodha_execute_orders") and live_ltp > 0 and qty > 0:
        from scout.wallet import entry_wallet_block_reason, wallet_summary

        entry_est = entry_max if action == "BUY" else entry_min
        if entry_est <= 0:
            entry_est = live_ltp
        wblock = entry_wallet_block_reason(
            trade_repo.db,
            entry_price=float(entry_est),
            quantity=qty,
            settings=settings,
        )
        if wblock:
            wallet_ok = False
            wallet_detail = wblock
        else:
            wsum = wallet_summary(trade_repo.db, settings, fetch=True)
            free = wsum.get("free_inr")
            cap = wsum.get("max_deployable_inr")
            if free is not None and cap is not None:
                wallet_detail = f"₹{float(free):,.0f} free of ₹{float(cap):,.0f} deployable"

    block = None
    if signal_id > 0 and automation_on and scout_enabled and mkt_open and not has_open_trade:
        block = _auto_enter_block_reason(
            trade_repo,
            settings,
            signal_id=signal_id,
            symbol=sym,
            strength=strength,
            signal_type=signal_type,
        )

    checks: List[dict] = [
        _check("automation", "Auto-enter enabled", automation_on),
        _check("scout_on", "Scout module on", scout_enabled),
        _check("market", "Market open", mkt_open),
        _check("validity", "Signal ACTIVE", validity == "ACTIVE", detail=validity or "—"),
        _check("band", "In entry band", band_ok is True, detail=(
            f"₹{live_ltp:.2f} in ₹{entry_min:.2f}–₹{entry_max:.2f}" if band_ok else (
                f"₹{live_ltp:.2f} outside ₹{entry_min:.2f}–₹{entry_max:.2f}" if band_ok is False else "—"
            )
        )),
        _check("stop", "Stop not hit", stop_ok is True, detail=(
            f"invalidation ₹{float(inv):.2f}" if inv is not None else "no stop set"
        )),
        _check("trade_window", "Trading window", window_ok, detail=window_detail),
        _check("strength", f"Strength ({strength})", strength_ok, detail=(
            f"allowed: {allowed_strengths}" if allowed_strengths else "none configured"
        )),
        _check("pattern", "Pattern allowed", pattern_ok, detail=(
            f"{signal_type} · allowed: {allowed_patterns}"
        )),
        _check("profit", "Min net profit", profit_ok, detail=profit_detail),
        _check(
            "wallet",
            "Deployable capital",
            wallet_ok,
            detail=wallet_detail or ("paper mode" if not settings.get("zerodha_execute_orders") else ""),
        ),
        _check(
            "daily_cap",
            "Daily trade cap",
            daily_ok,
            detail=f"{trades_today}/{max_day} today" if max_day > 0 else "unlimited",
        ),
        _check(
            "symbol_day",
            "Symbol not traded today",
            symbol_day_ok,
            detail=sym if not symbol_day_ok else "",
        ),
        _check(
            "symbol_open",
            "No other open trade",
            symbol_open_ok and not symbol_trade_blocked,
            detail=sym if not symbol_open_ok else "",
        ),
        _check("not_taken", "Signal not taken", not has_open_trade),
    ]

    ready = (
        automation_on
        and scout_enabled
        and mkt_open
        and not has_open_trade
        and validity == "ACTIVE"
        and band_ok is True
        and stop_ok is True
        and window_ok
        and strength_ok
        and pattern_ok
        and profit_ok
        and wallet_ok
        and daily_ok
        and symbol_day_ok
        and symbol_open_ok
        and not symbol_trade_blocked
        and live_ltp > 0
    )

    block_reason = block
    if ready:
        block_reason = None
    elif not block_reason:
        for c in checks:
            if not c["ok"]:
                block_reason = c["detail"] or c["label"]
                break

    return {
        "enabled": automation_on,
        "ready": ready,
        "block_reason": block_reason,
        "checks": checks,
        "quantity": qty,
        "live_ltp": live_ltp if live_ltp > 0 else None,
    }
