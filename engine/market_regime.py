"""Market regime labels for sit-out transparency (no gate changes)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from config import STRATEGY_CONFIG


def _parse_iv_rank_from_checks(checks: List[dict]) -> Optional[float]:
    for c in checks:
        if c.get("label") == "IV Rank in actionable zone":
            m = re.search(r"IV Rank\s+([\d.]+)", c.get("detail") or "")
            if m:
                return float(m.group(1))
    return None


def _parse_iv_premium_from_checks(checks: List[dict]) -> Optional[float]:
    for c in checks:
        if c.get("label") == "IV premium vs realised vol (HV-20)":
            m = re.search(r"ratio\s+([\d.]+)", c.get("detail") or "", re.I)
            if m:
                return float(m.group(1))
            m = re.search(r"([\d.]+)\s*×", c.get("detail") or "")
            if m:
                return float(m.group(1))
    return None


def parse_conditions_metrics(conditions_json: Any) -> Dict[str, Optional[float]]:
    """Extract IV rank and IV/HV from persisted confidence checks."""
    checks: List[dict] = []
    if isinstance(conditions_json, str) and conditions_json.strip():
        try:
            checks = json.loads(conditions_json)
        except json.JSONDecodeError:
            checks = []
    elif isinstance(conditions_json, list):
        checks = conditions_json
    return {
        "iv_rank": _parse_iv_rank_from_checks(checks),
        "iv_premium": _parse_iv_premium_from_checks(checks),
    }


def classify_market_regime(
    iv_rank: Optional[float],
    iv_premium: Optional[float],
) -> Dict[str, str]:
    """Classify IV regime for dashboard copy — does not affect engine gates."""
    write_min = float(STRATEGY_CONFIG["iv_rank_writing_min"])
    buy_max = float(STRATEGY_CONFIG["iv_rank_buying_max"])

    if iv_rank is None:
        return {
            "id": "unknown",
            "title": "Regime unclear",
            "summary": "IV rank data is not available yet.",
            "profit_note": (
                "The engine waits for a clear IV regime before suggesting trades."
            ),
        }

    if iv_rank > write_min:
        return {
            "id": "writing",
            "title": "High IV — premium selling favoured",
            "summary": (
                f"IV rank {iv_rank:.1f}% is above {write_min:.0f}% "
                f"(credit structures have edge when other gates pass)."
            ),
            "profit_note": (
                "Best expectancy: iron condor / spreads when trend and liquidity align."
            ),
        }

    if iv_rank < buy_max:
        prem = f" IV/HV {iv_premium:.2f}×." if iv_premium is not None else ""
        return {
            "id": "buying",
            "title": "Low IV — long premium favoured",
            "summary": (
                f"IV rank {iv_rank:.1f}% is below {buy_max:.0f}%.{prem}"
            ),
            "profit_note": (
                "Best expectancy: debit spreads / strangles when IV is not overpriced vs HV."
            ),
        }

    prem_txt = (
        f" IV/HV {iv_premium:.2f}× — options still expensive vs realised vol."
        if iv_premium is not None and iv_premium > 1.0
        else (
            f" IV/HV {iv_premium:.2f}×."
            if iv_premium is not None
            else ""
        )
    )
    return {
        "id": "dead_zone",
        "title": "Mid IV — sitting out (capital preservation)",
        "summary": (
            f"IV rank {iv_rank:.1f}% is between {buy_max:.0f}% and {write_min:.0f}% "
            f"(not high enough to sell, not low enough to buy).{prem_txt}"
        ),
        "profit_note": (
            "Waiting for IV rank > 50% (credit) or < 30% with cheap IV/HV (debit). "
            "No trade is better than a marginal one."
        ),
    }


def regime_from_sit_out_row(row: Dict[str, Any]) -> Dict[str, str]:
    metrics = parse_conditions_metrics(row.get("conditions_json"))
    return classify_market_regime(metrics["iv_rank"], metrics["iv_premium"])


def summarize_market_sit_out(rows: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Aggregate sit-out rows into one dashboard headline."""
    if not rows:
        return None

    regimes = [regime_from_sit_out_row(r) for r in rows]
    ids = {g["id"] for g in regimes}
    if ids == {"dead_zone"}:
        return {
            "title": "Engine sitting out — mid-IV dead zone",
            "summary": (
                "All underlyings are in the 30–50% IV rank band where neither "
                "premium selling nor cheap long-premium buys have a clear edge."
            ),
            "profit_note": regimes[0]["profit_note"],
        }
    if "dead_zone" in ids:
        return {
            "title": "Engine sitting out — mixed / weak edge",
            "summary": (
                "Some underlyings are in the mid-IV dead zone; others failed "
                "strategy or liquidity vetoes after confidence passed."
            ),
            "profit_note": (
                "Capital preserved until a high-conviction credit or cheap-IV setup appears."
            ),
        }
    return {
        "title": "Engine sitting out — vetoes active",
        "summary": (
            "Confidence or strategy checks blocked all underlyings today. "
            "See per-symbol cards for detail."
        ),
        "profit_note": (
            "Tight vetoes are intentional — they filter marginal setups that tend to lose."
        ),
    }
