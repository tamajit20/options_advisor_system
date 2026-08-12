"""Execution audit for Scout trades — entry/exit mode and conditions met."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from scout.signal_enrichment import build_exit_plan, enrich_signal, evaluate_exit_alerts

logger = logging.getLogger(__name__)

AUTO_ENTRY_SOURCES = frozenset({"auto_execute", "auto_enter"})
AUTO_EXIT_CODES = frozenset({
    "target_hit", "stop_hit", "square_off_due", "auto_close",
    "TARGET_HIT", "STOP_HIT", "SQUARE_OFF_DUE", "AUTO_CLOSE",
})

EXIT_LABELS = {
    "target_hit": "Target hit",
    "stop_hit": "Stop hit",
    "square_off_due": "Square-off (EOD)",
    "auto_close": "Auto-close rule",
    "manual": "Manual close",
    "TARGET_HIT": "Target hit",
    "STOP_HIT": "Stop hit",
    "SQUARE_OFF_DUE": "Square-off (EOD)",
    "AUTO_CLOSE": "Auto-close rule",
}


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_notes_audit(notes: Optional[str]) -> Optional[dict]:
    raw = (notes or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            logger.debug("scout trade notes JSON parse failed")
    if raw.lower() in AUTO_ENTRY_SOURCES:
        return {"mode": "auto", "source": raw.lower()}
    return None


def _norm_exit_code(reason: Optional[str]) -> str:
    code = str(reason or "manual").strip().lower()
    if code.startswith("auto:"):
        code = code.split(":", 1)[1].strip().lower()
    return code or "manual"


def _exit_mode(code: str) -> str:
    return "auto" if code in {c.lower() for c in AUTO_EXIT_CODES} else "manual"


def _signal_from_trade(trade: dict) -> dict:
    meta = None
    raw_meta = trade.get("meta_json") or trade.get("signal_meta_json")
    if raw_meta:
        try:
            meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": trade.get("signal_id"),
        "symbol": trade.get("symbol"),
        "action": trade.get("signal_action") or trade.get("action"),
        "signal_type": trade.get("signal_type"),
        "reason": trade.get("signal_reason"),
        "strength": trade.get("signal_strength"),
        "ltp": trade.get("signal_ltp"),
        "invalidation": trade.get("invalidation"),
        "triggered_at": trade.get("signal_triggered_at"),
        "meta": meta,
    }


def build_entry_audit(
    signal: dict,
    *,
    entry_price: float,
    executed_at: datetime,
    mode: str,
    source: str,
) -> str:
    """Compact JSON for scout_trades.notes (max 512 chars)."""
    enriched = enrich_signal(
        signal,
        live_ltp=float(entry_price),
        now=_parse_dt(executed_at) or executed_at,
    )
    gates = dict((enriched.get("dashboard") or {}).get("gates") or {})
    payload = {
        "mode": mode,
        "source": source,
        "fill": round(float(entry_price), 2),
        "trig": round(float(signal.get("ltp") or entry_price), 2),
        "validity": enriched.get("validity_status"),
        "setup": str(signal.get("signal_type") or ""),
        "strength": str(signal.get("strength") or ""),
        "gates": {
            "band": bool(gates.get("band_ok")),
            "time": bool(gates.get("time_ok")),
            "stop": bool(gates.get("stop_ok")),
        },
    }
    text = json.dumps(payload, separators=(",", ":"))
    return text[:512]


def build_entry_execution(trade: dict, signal: Optional[dict] = None) -> dict:
    sig = signal or _signal_from_trade(trade)
    audit = _parse_notes_audit(trade.get("notes"))
    notes_raw = (trade.get("notes") or "").strip().lower()

    if audit and audit.get("mode") in ("auto", "manual"):
        mode = str(audit["mode"])
        source = str(audit.get("source") or ("auto_execute" if mode == "auto" else "manual"))
    elif notes_raw in AUTO_ENTRY_SOURCES:
        mode, source = "auto", notes_raw
    else:
        mode, source = "manual", "manual"

    entry_px = float(trade.get("entry_price") or audit.get("fill") or 0)
    exec_at = _parse_dt(trade.get("executed_at"))
    conditions: List[dict] = []

    if audit:
        setup = audit.get("setup") or sig.get("signal_type")
        if setup:
            conditions.append({"label": "Setup", "value": str(setup).replace("_", " ")})
        if audit.get("strength"):
            conditions.append({"label": "Strength", "value": str(audit["strength"])})
        if audit.get("validity"):
            conditions.append({
                "label": "Signal validity",
                "value": str(audit["validity"]),
                "ok": str(audit["validity"]).upper() == "ACTIVE",
            })
        gates = audit.get("gates") or {}
        if gates:
            gate_vals = (
                gates.get("band", gates.get("band_ok")),
                gates.get("time", gates.get("time_ok")),
                gates.get("stop", gates.get("stop_ok")),
            )
            conditions.append({
                "label": "Entry gates",
                "value": ", ".join(
                    f"{k}={'✓' if v else '✗'}"
                    for k, v in zip(("band", "time", "stop"), gate_vals)
                ),
                "ok": all(bool(x) for x in gate_vals),
            })
        trig = audit.get("trig")
        if trig is not None and entry_px:
            conditions.append({
                "label": "Fill vs trigger",
                "value": f"₹{entry_px:.2f} vs ₹{float(trig):.2f}",
            })

    if not conditions and sig.get("signal_type"):
        enriched = enrich_signal(
            sig,
            live_ltp=entry_px or None,
            now=exec_at,
        )
        conditions.append({
            "label": "Setup",
            "value": str(sig.get("signal_type") or "").replace("_", " "),
        })
        if sig.get("strength"):
            conditions.append({"label": "Strength", "value": str(sig["strength"])})
        validity = enriched.get("validity_status")
        if validity:
            conditions.append({
                "label": "Signal validity (est.)",
                "value": validity,
                "ok": validity == "ACTIVE",
            })
        dash = enriched.get("dashboard") or {}
        gates = dash.get("gates") or {}
        if gates:
            conditions.append({
                "label": "Entry gates (est.)",
                "value": f"band={'✓' if gates.get('band_ok') else '✗'}, "
                         f"time={'✓' if gates.get('time_ok') else '✗'}, "
                         f"stop={'✓' if gates.get('stop_ok') else '✗'}",
            })

    if sig.get("reason"):
        conditions.append({"label": "Signal reason", "value": str(sig["reason"])})

    return {
        "mode": mode,
        "mode_label": "Auto-enter" if mode == "auto" else "Manual enter",
        "source": source,
        "conditions": conditions,
    }


def build_exit_execution(trade: dict, signal: Optional[dict] = None) -> dict:
    sig = signal or _signal_from_trade(trade)
    code = _norm_exit_code(trade.get("exit_reason"))
    mode = _exit_mode(code)
    exit_px = float(trade.get("exit_price") or 0)
    entry_px = float(trade.get("entry_price") or 0)
    closed_at = _parse_dt(trade.get("closed_at"))
    conditions: List[dict] = []

    label = EXIT_LABELS.get(code) or EXIT_LABELS.get(code.upper()) or code.replace("_", " ").title()

    plan = build_exit_plan(
        sig,
        entry_price=entry_px,
        executed_at=trade.get("executed_at"),
        live_ltp=exit_px if exit_px > 0 else None,
        now=closed_at,
    )
    prices = dict((plan.get("dashboard") or {}).get("prices") or {})
    target = prices.get("target")
    stop = prices.get("stop")

    if code == "target_hit" and target is not None and exit_px:
        conditions.append({
            "label": "Target",
            "value": f"Exit ₹{exit_px:.2f} — target was ₹{float(target):.2f}",
            "ok": True,
        })
    elif code == "stop_hit" and stop is not None and exit_px:
        conditions.append({
            "label": "Stop",
            "value": f"Exit ₹{exit_px:.2f} — stop was ₹{float(stop):.2f}",
            "ok": False,
        })
    elif code == "square_off_due":
        conditions.append({
            "label": "Square-off",
            "value": f"Closed at {plan.get('square_off_by') or 'EOD'} — target not met",
            "ok": None,
        })
    elif code == "manual":
        conditions.append({"label": "Close type", "value": "Closed manually in dashboard"})
    else:
        conditions.append({"label": "Trigger", "value": label})

    if exit_px and entry_px:
        action = str(trade.get("action") or "").upper()
        if action == "BUY":
            move = exit_px - entry_px
        else:
            move = entry_px - exit_px
        conditions.append({
            "label": "Move",
            "value": f"{'+' if move >= 0 else ''}₹{move:.2f}/share",
            "ok": move > 0,
        })

    alerts = evaluate_exit_alerts(
        action=str(trade.get("action") or ""),
        live_ltp=exit_px if exit_px > 0 else None,
        exit_plan=plan,
    )
    for alert in alerts.get("alerts") or []:
        if str(alert.get("code") or "").lower() == code or code == "manual":
            conditions.insert(0, {
                "label": "Alert",
                "value": str(alert.get("label") or label),
                "ok": alert.get("level") == "now" and code == "target_hit",
            })
            break

    return {
        "mode": mode,
        "mode_label": "Auto-close" if mode == "auto" else "Manual close",
        "trigger_code": code,
        "trigger_label": label,
        "conditions": conditions,
    }


def enrich_history_trade(trade: dict) -> dict:
    """Add execution audit block for history API / UI."""
    sig = _signal_from_trade(trade)
    out = dict(trade)
    out["execution"] = {
        "entry": build_entry_execution(trade, sig),
        "exit": build_exit_execution(trade, sig),
    }
    return out
