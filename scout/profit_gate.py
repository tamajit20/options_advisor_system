"""Scout entry profitability gates — expected net vs Zerodha intraday costs."""

from __future__ import annotations

from typing import Optional, Set, Tuple

from engine.equity_charges import estimate_equity_intraday_charges_for_target
from scout.settings_schema import default_scout_settings

VALID_AUTO_SIGNAL_TYPES = frozenset({
    "OR_BREAK_UP",
    "OR_BREAK_DOWN",
    "RANGE_BREAK_UP",
    "RANGE_BREAK_DOWN",
    "PULLBACK_UP",
    "PULLBACK_DOWN",
})


def signal_type_allowed(settings: dict, signal_type: Optional[str]) -> bool:
    allowed: Set[str] = {
        str(s).upper() for s in (settings.get("auto_enter_signal_types") or [])
    }
    if not allowed:
        return True
    return str(signal_type or "").upper() in allowed


def effective_min_net_profit(settings: dict, *, notional: float) -> float:
    """Floor in ₹ and optional % of capital deployed — use the higher bar."""
    floor = float(settings.get("min_net_profit_inr", 100.0))
    pct = float(settings.get("min_net_profit_pct", 0.0))
    if pct > 0 and notional > 0:
        return max(floor, round(notional * pct, 2))
    return floor


def _risk_per_share(entry: float, stop: float, action: str) -> float:
    if action == "BUY":
        return max(0.0, float(entry) - float(stop))
    return max(0.0, float(stop) - float(entry))


def _reward_per_share(entry: float, target: float, action: str) -> float:
    if action == "BUY":
        return max(0.0, float(target) - float(entry))
    return max(0.0, float(entry) - float(target))


def entry_profit_analysis(
    *,
    signal: dict,
    entry: float,
    qty: int,
    settings: dict,
) -> Tuple[Optional[str], dict]:
    """
    Return (block_reason, metrics). block_reason is None when entry passes all gates.
    """
    from scout.signal_enrichment import build_exit_plan

    plan = build_exit_plan(signal, entry_price=float(entry), settings=settings)
    target = plan.get("target_price")
    stop = plan.get("stop_price")
    metrics: dict = {"entry": float(entry), "qty": int(qty)}

    if target is None or stop is None:
        return "missing stop or target for profitability check", metrics

    action = str(signal.get("action") or "").upper()
    qty_i = max(int(qty), 1)
    entry_f = float(entry)
    target_f = float(target)
    stop_f = float(stop)
    risk = _risk_per_share(entry_f, stop_f, action)
    metrics.update({
        "target": target_f,
        "stop": stop_f,
        "risk_per_share": round(risk, 4),
        "target_r": plan.get("target_r", settings.get("min_target_r", 2.0)),
    })

    if entry_f <= 0:
        return "invalid entry price for profitability check", metrics

    min_risk_pct = float(settings.get("min_risk_pct", 0.0))
    if min_risk_pct > 0:
        risk_pct = risk / entry_f * 100.0
        metrics["risk_pct"] = round(risk_pct, 3)
        if risk_pct < min_risk_pct:
            return (
                f"stop too tight ({risk_pct:.2f}% risk < min {min_risk_pct:.2f}% — "
                "charges dominate small moves)",
                metrics,
            )

    min_target_r = float(settings.get("min_target_r", 2.0))
    if risk > 0:
        reward = _reward_per_share(entry_f, target_f, action)
        rr = reward / risk
        metrics["reward_risk"] = round(rr, 2)
        if rr + 1e-9 < min_target_r:
            return (
                f"reward/risk {rr:.2f} < min {min_target_r:.1f}R at entry",
                metrics,
            )

    slip_pct = float(settings.get("profit_slippage_pct", 0.0)) / 100.0
    if action == "BUY":
        expected_gross = (target_f - entry_f) * qty_i
        if slip_pct > 0:
            expected_gross -= (entry_f + target_f) * qty_i * slip_pct
    else:
        expected_gross = (entry_f - target_f) * qty_i
        if slip_pct > 0:
            expected_gross -= (entry_f + target_f) * qty_i * slip_pct

    charges = estimate_equity_intraday_charges_for_target(
        entry=entry_f, target=target_f, qty=qty_i,
    ).total
    charge_buffer = float(settings.get("profit_charge_buffer_inr", 0.0))
    total_cost = charges + charge_buffer

    notional = entry_f * qty_i
    min_net = effective_min_net_profit(settings, notional=notional)
    expected_net = expected_gross - total_cost

    metrics.update({
        "expected_gross": round(expected_gross, 2),
        "charges": round(charges, 2),
        "charge_buffer": charge_buffer,
        "expected_net": round(expected_net, 2),
        "min_net_required": min_net,
        "notional": round(notional, 2),
    })

    if expected_net < min_net:
        r_mult = metrics.get("target_r", min_target_r)
        return (
            f"expected net ₹{expected_net:.0f} at {target_f:.2f} ({r_mult}R target) "
            f"(gross ₹{expected_gross:.0f} − costs ₹{total_cost:.0f}) "
            f"< min ₹{min_net:.0f}",
            metrics,
        )
    return None, metrics


def entry_profit_block_reason(
    *,
    signal: dict,
    entry: float,
    qty: int,
    settings: dict,
) -> Optional[str]:
    """None if entry passes cost gate; else human-readable skip reason."""
    reason, _ = entry_profit_analysis(
        signal=signal, entry=entry, qty=qty, settings=settings,
    )
    return reason
