"""Score closed trades against the gates and conditions saved at suggestion time.

Taken trades already cleared the hard gates, so a gate that passed on every
winner usually passed on losers too. The useful split is where a warning or a
condition shows up on one side more than the other.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

_ADVISORY_LABELS = frozenset({
    "No high-impact event this week",
    "Event inside the hold",
    "ATM IV trajectory benign",
    "OI PCR momentum neutral",
    "IV Rank vs IV/HV alignment",
    "Today's tape & nearby OI wall",
    "Mid-IV zone",
    "Range trade vs OI PCR drift",
    "FII options book",
    "Max pain vs short strikes",
    "Volume burst",
})

_EVENT_LABELS = frozenset({
    "No high-impact event this week",
    "Event inside the hold",
})

_STOP = frozenset({
    "LOSS_LIMIT_HIT", "SL_TRIGGER", "PRE_BREACH_WARNING",
})
_TARGET = frozenset({
    "TARGET_HIT", "TAKE_PROFIT", "PROFIT_MILESTONE_HIT", "PROFIT_PCT_HIT",
})


def build_closed_trade_report(trades: Iterable[dict]) -> dict:
    """Return gate, warning, condition, exit, and event slices."""
    rows = [t for t in trades if t.get("net_pnl") is not None]
    return {
        "trade_count": len(rows),
        "gates": _gate_rows(rows, advisory=False),
        "advisories": _gate_rows(rows, advisory=True),
        "by_underlying": _slice(rows, _underlying_name),
        "by_dte": _slice(rows, _dte_band),
        "by_iv_rank": _slice(rows, _iv_band),
        "by_credit_grade": _slice(rows, _credit_name),
        "by_entry_quality": _slice(rows, _quality_band),
        "by_exit": _slice(rows, _exit_name),
        "by_event": _slice(rows, _event_name),
        "note": (
            "Taken trades already passed the hard gates. "
            "A gate that passed on every winner often passed on the losers too. "
            "Read a row only when both sides have a few trades."
        ),
    }


def exit_bucket(exit_signal: Optional[str], daily_status: Optional[str]) -> str:
    """Map the last risk alert, or an expiry settlement, to a close type."""
    if str(daily_status or "").upper() == "AUTO_SETTLED":
        return "Expiry settlement"
    signal = str(exit_signal or "").upper()
    if signal in _STOP:
        return "Stop"
    if signal == "PROFIT_FLOOR_HIT":
        return "Profit floor"
    if signal in _TARGET:
        return "Target"
    return "Manual close"


def _gate_rows(trades: List[dict], *, advisory: bool) -> List[dict]:
    buckets: Dict[str, dict] = {}
    for trade in trades:
        win = float(trade["net_pnl"]) > 0
        pnl = float(trade["net_pnl"])
        seen = set()
        for check in _checks(trade.get("conditions_json")):
            label = str(check.get("label") or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            kind = _kind(check)
            if advisory != (kind == "ADVISORY"):
                continue
            slot = buckets.setdefault(label, {
                "label": label,
                "kind": kind,
                "win_pass": 0, "win_n": 0, "loss_pass": 0, "loss_n": 0,
                "pass_pnls": [], "warn_pnls": [],
            })
            passed = str(check.get("status") or "") not in ("FAIL", "SOFT_FAIL")
            if win:
                slot["win_n"] += 1
                if passed:
                    slot["win_pass"] += 1
            else:
                slot["loss_n"] += 1
                if passed:
                    slot["loss_pass"] += 1
            (slot["pass_pnls"] if passed else slot["warn_pnls"]).append(pnl)
    out = [_gate_summary(slot) for slot in buckets.values()]
    out.sort(key=lambda r: (-abs(r["pass_win_pct"] - r["pass_loss_pct"]), r["label"]))
    return out


def _gate_summary(slot: dict) -> dict:
    win_n = slot["win_n"]
    loss_n = slot["loss_n"]
    pass_win = _pct(slot["win_pass"], win_n)
    pass_loss = _pct(slot["loss_pass"], loss_n)
    return {
        "label": slot["label"],
        "kind": slot["kind"],
        "win_pass": slot["win_pass"],
        "win_n": win_n,
        "loss_pass": slot["loss_pass"],
        "loss_n": loss_n,
        "pass_win_pct": pass_win,
        "pass_loss_pct": pass_loss,
        "avg_pnl_passed": _avg(slot["pass_pnls"]),
        "avg_pnl_warned": _avg(slot["warn_pnls"]),
        "read": _gate_read(pass_win, pass_loss, win_n, loss_n),
    }


def _gate_read(pass_win: float, pass_loss: float, win_n: int, loss_n: int) -> str:
    total = win_n + loss_n
    if total < 4 or win_n == 0 or loss_n == 0:
        return "Too few trades to judge"
    gap = pass_win - pass_loss
    if abs(gap) < 10:
        return "Passed on winners and losers — not separating"
    if gap >= 15:
        return "Warning sits more often on losers"
    if gap <= -15:
        return "Warning sits more often on winners"
    return "No clear split yet"


def _slice(trades: List[dict], name_of) -> List[dict]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for trade in trades:
        groups[name_of(trade)].append(trade)
    rows = [_group_stats(name, items) for name, items in groups.items()]
    rows.sort(key=lambda r: (-r["total"], r["name"]))
    return rows


def _group_stats(name: str, items: List[dict]) -> dict:
    pnls = [float(t["net_pnl"]) for t in items]
    wins = [p for p in pnls if p > 0]
    return {
        "name": name,
        "total": len(pnls),
        "wins": len(wins),
        "losses": len(pnls) - len(wins),
        "win_rate": _pct(len(wins), len(pnls)),
        "avg_pnl": _avg(pnls),
        "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
    }


def _checks(raw: Any) -> List[dict]:
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def _kind(check: dict) -> str:
    kind = str(check.get("kind") or "").upper()
    if kind in ("HARD", "SOFT", "ADVISORY"):
        return kind
    if str(check.get("label") or "") in _ADVISORY_LABELS:
        return "ADVISORY"
    return "SOFT"


def _underlying_name(trade: dict) -> str:
    return str(trade.get("underlying") or "Unknown")


def _dte_band(trade: dict) -> str:
    raw = trade.get("dte")
    try:
        dte = int(raw)
    except (TypeError, ValueError):
        return "DTE unknown"
    if dte < 7:
        return "Under 7 DTE"
    if dte <= 10:
        return "7–10 DTE"
    if dte <= 14:
        return "11–14 DTE"
    if dte <= 21:
        return "15–21 DTE"
    return "Over 21 DTE"


def _iv_band(trade: dict) -> str:
    rank = _iv_rank(trade.get("conditions_json"))
    if rank is None:
        return "IV rank unknown"
    if rank < 30:
        return "IV rank under 30"
    if rank <= 50:
        return "IV rank 30–50"
    if rank <= 70:
        return "IV rank 50–70"
    return "IV rank over 70"


def _iv_rank(raw: Any) -> Optional[float]:
    for check in _checks(raw):
        if check.get("label") != "IV Rank in actionable zone":
            continue
        match = re.search(r"IV Rank\s+([\d.]+)", str(check.get("detail") or ""))
        if match:
            return float(match.group(1))
    return None


def _credit_name(trade: dict) -> str:
    grade = str(trade.get("credit_grade") or "").strip().lower()
    if grade in ("weak", "good", "strong"):
        return f"Credit grade {grade}"
    return "Credit grade not set"


def _quality_band(trade: dict) -> str:
    raw = trade.get("entry_quality_score")
    try:
        score = int(raw)
    except (TypeError, ValueError):
        return "Entry quality unknown"
    if score >= 80:
        return "Entry quality excellent"
    if score >= 65:
        return "Entry quality good"
    if score >= 50:
        return "Entry quality fair"
    if score >= 35:
        return "Entry quality weak"
    return "Entry quality poor"


def _exit_name(trade: dict) -> str:
    return exit_bucket(trade.get("exit_signal"), trade.get("daily_status"))


def _event_name(trade: dict) -> str:
    for check in _checks(trade.get("conditions_json")):
        if str(check.get("label") or "") not in _EVENT_LABELS:
            continue
        if str(check.get("status") or "") in ("FAIL", "SOFT_FAIL"):
            return "High-impact event in the hold"
    return "No high-impact event flagged"


def _pct(part: int, whole: int) -> float:
    if not whole:
        return 0.0
    return round(part / whole * 100, 1)


def _avg(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 2)
