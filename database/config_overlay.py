"""
database/config_overlay.py
=========================

Apply ``options_config`` rows onto the live config dicts so the Config page
edits actually drive the engine, Exit Plan, charges, scheduler, and alerts.

``config.py`` remains the file default. Strategy keys are stored unprefixed
(``take_profit_fraction``). Other namespaces use a prefix
(``scheduler.timezone``, ``zerodha_charges.gst_pct``, ``events.calendar``).
Secrets (passwords, API tokens) are never listed or writable.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import (
    ALERTS_CONFIG,
    ALERTS_CONFIG_DEFAULTS,
    DASHBOARD_CONFIG,
    DASHBOARD_CONFIG_DEFAULTS,
    EVENTS_CONFIG,
    EVENTS_CONFIG_DEFAULTS,
    LOGGING_CONFIG,
    LOGGING_CONFIG_DEFAULTS,
    PROVIDERS_CONFIG,
    PROVIDERS_CONFIG_DEFAULTS,
    RETENTION_CONFIG,
    RETENTION_CONFIG_DEFAULTS,
    SCHEDULER_CONFIG,
    SCHEDULER_CONFIG_DEFAULTS,
    SIMULATION_CONFIG,
    SIMULATION_CONFIG_DEFAULTS,
    STRATEGY_CONFIG,
    STRATEGY_CONFIG_DEFAULTS,
    ZERODHA_API_CONFIG,
    ZERODHA_API_CONFIG_DEFAULTS,
    ZERODHA_CONFIG,
    ZERODHA_CONFIG_DEFAULTS,
    ZERODHA_EQUITY_INTRADAY_CONFIG,
    ZERODHA_EQUITY_INTRADAY_CONFIG_DEFAULTS,
    ZERODHA_EXECUTION_CONFIG,
    ZERODHA_EXECUTION_CONFIG_DEFAULTS,
)


logger = logging.getLogger(__name__)

_SECRET_KEYS = frozenset({
    "api_key", "api_secret", "access_token", "password", "smtp_password",
    "telegram_bot_token",
})
_DASHBOARD_SKIP = frozenset({
    "host", "port", "debug", "api_key", "public_base_url",
})

_PNL_KEYS = frozenset({
    "long_premium_target_base", "long_premium_target_dte_scale",
    "long_premium_target_max", "long_premium_target_strategies",
    "debit_spread_target_fraction", "debit_spread_target_strategies",
    "take_profit_fraction", "strategy_take_profit_fraction",
    "strategy_sl_defaults", "strategy_sl_limits", "stop_loss_fraction",
    "time_decay_exit_dte", "time_decay_exit_strategies",
    "long_premium_thesis_exit", "intraday_sl_multiplier",
    "live_risk_monitor", "adverse_move_warning_pct",
    "loss_milestone_alert",
})

_GROUP_META: Sequence[Tuple[str, str]] = (
    ("zerodha", "Zerodha broker execution"),
    ("pnl", "Profit targets & stop-loss"),
    ("sizing", "Capital & sizing"),
    ("gates", "Entry gates & IV"),
    ("trend", "Trend & trajectory"),
    ("monitor", "Live risk & alerts"),
    ("charges", "Brokerage & charges"),
    ("scheduler", "Scheduler & jobs"),
    ("providers", "Market-data providers"),
    ("dashboard", "Dashboard"),
    ("alerts", "Email / Telegram"),
    ("retention", "Data retention"),
    ("events", "Event calendar"),
    ("simulation", "Simulation"),
    ("logging", "Logging"),
    ("other", "Other strategy knobs"),
)

_KEY_GROUP: Dict[str, str] = {k: "pnl" for k in _PNL_KEYS}
_KEY_GROUP.update({
    "trading_capital_rs": "sizing",
    "risk_per_trade_pct": "sizing",
    "max_lots_cap": "sizing",
    "max_loss_pct_of_capital": "sizing",
    "default_lot_sizes": "sizing",
    "daily_pnl_circuit_breaker_capital_rs": "sizing",
    "daily_pnl_circuit_breaker_pct": "sizing",
    "underlyings": "sizing",
    "live_risk_monitor": "monitor",
    "intraday_sl_multiplier": "monitor",
    "ws_watchdog": "monitor",
    "suggestion_freshness_minutes": "monitor",
    "execution_validator_enabled": "monitor",
    "execution_validator_max_data_age_minutes": "monitor",
    "dte_min": "gates",
    "dte_max": "gates",
    "calendar_near_dte_max": "gates",
    "calendar_far_dte_min": "gates",
    "short_premium_strategies": "gates",
    "long_strangle_em_multiplier": "gates",
    "fii_net_futures_threshold": "gates",
})

_DESCRIPTIONS: Dict[str, str] = {
    "long_premium_target_base":
        "Long premium / calendar: profit target starts at this multiple of debit paid.",
    "long_premium_target_dte_scale":
        "Added as DTE / this number. Default 0.50 + 7/14 = 100% of debit at 7 DTE.",
    "long_premium_target_max":
        "Cap on the DTE-aware debit multiple (default 1.50 = 150%).",
    "strategy_take_profit_fraction":
        "Per-strategy take-profit (fraction of max profit ≈ credit captured).",
    "strategy_sl_limits":
        "Per-strategy MTM stop (loss_fraction + ₹ cap). Exit Plan and LOSS_LIMIT_HIT.",
    "loss_milestone_alert":
        "Optional early exit when MTM loss hits pct_of_premium % of entry premium "
        "(paid for debits, received for credits — same as P&L % brackets). "
        "Separate from strategy SL. JSON: {enabled, pct_of_premium, cooldown_minutes}. "
        "Legacy key pct_of_max_loss is still read if pct_of_premium is omitted.",
    "live_risk_monitor":
        "Live alert engine (session, cooldown, trailing floor, pre-breach). Nested JSON.",
    "trading_capital_rs": "Notional capital for circuit-breaker and sizing.",
    "scheduler.jobs":
        "Per-job cron / enable flags. Job times take effect after a scheduler restart.",
    "scheduler.timezone":
        "APScheduler timezone. Takes effect after a scheduler restart.",
    "events.calendar":
        "HIGH-impact events used by the event gate. JSON list of {date, event_type, description, impact}.",
    "alerts.telegram_enabled":
        "Telegram channel. Channel objects are built at process start — restart after changing.",
    "alerts.email_enabled":
        "Email channel. Channel objects are built at process start — restart after changing.",
    "zerodha_api.enabled":
        "Hard kill switch for the Kite adapter. Restart the WS runner after changing.",
    "zerodha_execution.enabled":
        "Enable Execute/Close in Zerodha from the dashboard. Also turn on the "
        "trade_execution_enabled runtime switch below.",
    "zerodha_execution.require_price_band":
        "Reject LIMIT prices outside the suggestion band (preview warns; ack required).",
    "zerodha_execution.max_price_drift_pct":
        "When band columns are empty, max % drift from suggested mid before blocking.",
    "zerodha_execution.order_poll_interval_sec":
        "Seconds between Kite order-status polls while waiting for fills.",
    "zerodha_execution.order_max_wait_sec":
        "Give up polling a single leg after this many seconds (then retry).",
    "zerodha_execution.order_max_retries":
        "Re-place a leg up to this many times before failing the trade.",
    "zerodha_execution.limit_slippage_pct":
        "Auto LIMIT offset from LTP: BUY pays up, SELL accepts less (%).",
    "zerodha_execution.product":
        "Kite product code for option orders (e.g. NRML, MIS).",
    "zerodha_execution.variety":
        "Kite order variety (usually regular).",
}


@dataclass(frozen=True)
class _Spec:
    key: str
    local_key: str
    group: str
    target: Any
    defaults: Any
    needs_restart: bool
    is_list_root: bool = False


def _strategy_group(local_key: str) -> str:
    if local_key in _KEY_GROUP:
        return _KEY_GROUP[local_key]
    if local_key.startswith(("iv_", "pcr_", "oi_", "vix_", "long_vol", "strategy_min",
                             "strategy_iv", "soft_gate", "confidence", "em_", "min_")):
        return "gates"
    if local_key.startswith("trend_") or "traj" in local_key:
        return "trend"
    if local_key.startswith(("intraday_", "regen_")):
        return "monitor"
    return "other"


def _specs() -> List[_Spec]:
    out: List[_Spec] = []
    for local in STRATEGY_CONFIG_DEFAULTS:
        out.append(_Spec(
            key=local, local_key=local, group=_strategy_group(local),
            target=STRATEGY_CONFIG, defaults=STRATEGY_CONFIG_DEFAULTS,
            needs_restart=False,
        ))

    def _add_ns(prefix: str, group: str, target: dict, defaults: dict, *,
                skip: frozenset = frozenset(), needs_restart: bool = False,
                restart_keys: frozenset = frozenset()) -> None:
        for local, _val in defaults.items():
            if local in skip or local in _SECRET_KEYS:
                continue
            out.append(_Spec(
                key=f"{prefix}.{local}" if prefix else local,
                local_key=local, group=group,
                target=target, defaults=defaults,
                needs_restart=needs_restart or local in restart_keys,
            ))

    _add_ns("scheduler", "scheduler", SCHEDULER_CONFIG, SCHEDULER_CONFIG_DEFAULTS,
            restart_keys=frozenset({"jobs", "timezone"}))
    _add_ns("zerodha_charges", "charges", ZERODHA_CONFIG, ZERODHA_CONFIG_DEFAULTS)
    _add_ns("zerodha_equity", "charges", ZERODHA_EQUITY_INTRADAY_CONFIG,
            ZERODHA_EQUITY_INTRADAY_CONFIG_DEFAULTS)
    _add_ns("simulation", "simulation", SIMULATION_CONFIG, SIMULATION_CONFIG_DEFAULTS)
    _add_ns("dashboard", "dashboard", DASHBOARD_CONFIG, DASHBOARD_CONFIG_DEFAULTS,
            skip=_DASHBOARD_SKIP)
    _add_ns("alerts", "alerts", ALERTS_CONFIG, ALERTS_CONFIG_DEFAULTS,
            skip=_SECRET_KEYS, needs_restart=True)
    _add_ns("retention", "retention", RETENTION_CONFIG, RETENTION_CONFIG_DEFAULTS)
    _add_ns("logging", "logging", LOGGING_CONFIG, LOGGING_CONFIG_DEFAULTS,
            skip=frozenset({"log_dir", "log_file_name"}), needs_restart=True)
    _add_ns("providers", "providers", PROVIDERS_CONFIG, PROVIDERS_CONFIG_DEFAULTS,
            needs_restart=True)
    _add_ns("zerodha_execution", "zerodha", ZERODHA_EXECUTION_CONFIG,
            ZERODHA_EXECUTION_CONFIG_DEFAULTS)
    out.append(_Spec(
        key="zerodha_api.enabled", local_key="enabled", group="providers",
        target=ZERODHA_API_CONFIG, defaults=ZERODHA_API_CONFIG_DEFAULTS,
        needs_restart=True,
    ))
    out.append(_Spec(
        key="events.calendar", local_key="calendar", group="events",
        target=EVENTS_CONFIG, defaults=EVENTS_CONFIG_DEFAULTS,
        needs_restart=False, is_list_root=True,
    ))
    return out


def _spec_map() -> Dict[str, _Spec]:
    return {s.key: s for s in _specs()}


def resolve_spec(key: str) -> _Spec:
    spec = _spec_map().get(key)
    if spec is None:
        raise KeyError(f"unknown config key: {key}")
    return spec


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (list, tuple, dict)) or value is None:
        return "json"
    return "text"


def json_safe(value: Any) -> Any:
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return value


def _parse_stored(raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _default_for(spec: _Spec) -> Any:
    if spec.is_list_root:
        return spec.defaults
    return spec.defaults[spec.local_key]


def coerce_value(key: str, value: Any) -> Any:
    spec = resolve_spec(key)
    default = _default_for(spec)
    if isinstance(value, str) and _value_type(default) == "json":
        value = json.loads(value)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, tuple):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{key} must be a list")
        return list(value)
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a JSON array")
        return value
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a JSON object")
        return value
    return value


def restore_file_defaults() -> None:
    STRATEGY_CONFIG.clear()
    STRATEGY_CONFIG.update(copy.deepcopy(STRATEGY_CONFIG_DEFAULTS))
    SCHEDULER_CONFIG.clear()
    SCHEDULER_CONFIG.update(copy.deepcopy(SCHEDULER_CONFIG_DEFAULTS))
    ZERODHA_CONFIG.clear()
    ZERODHA_CONFIG.update(copy.deepcopy(ZERODHA_CONFIG_DEFAULTS))
    ZERODHA_EQUITY_INTRADAY_CONFIG.clear()
    ZERODHA_EQUITY_INTRADAY_CONFIG.update(
        copy.deepcopy(ZERODHA_EQUITY_INTRADAY_CONFIG_DEFAULTS)
    )
    SIMULATION_CONFIG.clear()
    SIMULATION_CONFIG.update(copy.deepcopy(SIMULATION_CONFIG_DEFAULTS))
    DASHBOARD_CONFIG.clear()
    DASHBOARD_CONFIG.update(copy.deepcopy(DASHBOARD_CONFIG_DEFAULTS))
    ALERTS_CONFIG.clear()
    ALERTS_CONFIG.update(copy.deepcopy(ALERTS_CONFIG_DEFAULTS))
    RETENTION_CONFIG.clear()
    RETENTION_CONFIG.update(copy.deepcopy(RETENTION_CONFIG_DEFAULTS))
    LOGGING_CONFIG.clear()
    LOGGING_CONFIG.update(copy.deepcopy(LOGGING_CONFIG_DEFAULTS))
    PROVIDERS_CONFIG.clear()
    PROVIDERS_CONFIG.update(copy.deepcopy(PROVIDERS_CONFIG_DEFAULTS))
    ZERODHA_API_CONFIG.clear()
    ZERODHA_API_CONFIG.update(copy.deepcopy(ZERODHA_API_CONFIG_DEFAULTS))
    ZERODHA_EXECUTION_CONFIG.clear()
    ZERODHA_EXECUTION_CONFIG.update(copy.deepcopy(ZERODHA_EXECUTION_CONFIG_DEFAULTS))
    EVENTS_CONFIG.clear()
    EVENTS_CONFIG.extend(copy.deepcopy(EVENTS_CONFIG_DEFAULTS))


def _assign(spec: _Spec, value: Any) -> None:
    if spec.is_list_root:
        spec.target.clear()
        spec.target.extend(value)
        return
    spec.target[spec.local_key] = value


def apply_config_overrides(db=None) -> int:
    """Copy DB overrides onto live config dicts. Returns override count."""
    close = False
    if db is None:
        from database.connection import SQLServerConnection
        db = SQLServerConnection()
        db.connect()
        close = True
    try:
        from database.models import ConfigRepo
        rows = ConfigRepo(db).get_all()
        if not isinstance(rows, (list, tuple)):
            logger.debug("config overlay skipped: get_all returned %s", type(rows))
            return 0
        restore_file_defaults()
        specs = _spec_map()
        applied = 0
        for row in rows:
            key = row.get("config_key")
            spec = specs.get(key)
            if spec is None:
                continue
            try:
                parsed = _parse_stored(row.get("config_value"))
                _assign(spec, coerce_value(key, parsed))
                applied += 1
            except Exception:
                logger.warning("config overlay skipped %s", key, exc_info=True)
        if applied:
            logger.info("config overlay: applied %d options_config override(s)", applied)
        return applied
    finally:
        if close:
            try:
                db.close()
            except Exception:
                pass


def apply_strategy_overrides(db=None) -> int:
    """Backward-compatible alias."""
    return apply_config_overrides(db)


def catalog_items(db) -> List[Dict[str, Any]]:
    from database.models import ConfigRepo
    raw = ConfigRepo(db).get_all()
    if not isinstance(raw, (list, tuple)):
        raw = []
    rows = {r["config_key"]: r for r in raw}
    items: List[Dict[str, Any]] = []
    for spec in sorted(_specs(), key=lambda s: (s.group, s.key)):
        default = json_safe(_default_for(spec))
        row = rows.get(spec.key)
        overridden = row is not None and row.get("config_value") is not None
        current = (
            coerce_value(spec.key, _parse_stored(row["config_value"]))
            if overridden else default
        )
        items.append({
            "key": spec.key,
            "group": spec.group,
            "type": _value_type(_default_for(spec)),
            "value": json_safe(current),
            "default": default,
            "overridden": overridden,
            "description": _DESCRIPTIONS.get(spec.key) or spec.local_key.replace("_", " "),
            "modified_at": str(row.get("last_modified") or "") if row else "",
            "modified_by": (row.get("modified_by") or "") if row else "",
            "locked": bool(row.get("is_locked")) if row else False,
            "needs_restart": spec.needs_restart,
        })
    return items


def catalog_payload(db) -> Dict[str, Any]:
    items = catalog_items(db)
    by_group: Dict[str, List[Dict[str, Any]]] = {gid: [] for gid, _ in _GROUP_META}
    for item in items:
        by_group.setdefault(item["group"], []).append(item)
    groups = [
        {"id": gid, "label": label, "items": by_group.get(gid) or []}
        for gid, label in _GROUP_META
    ]
    flags: List[Dict[str, Any]] = []
    try:
        from database.runtime_flags import RuntimeFlagsRepo
        repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
        for f in repo.all():
            flags.append({
                "key": f.key,
                "value": f.value,
                "type": f.type,
                "description": f.description,
                "last_modified": str(f.last_modified) if f.last_modified else None,
                "modified_by": f.modified_by,
            })
    except Exception:
        logger.debug("catalog flags read failed", exc_info=True)
    from database.models import ConfigRepo
    raw_cfg = ConfigRepo(db).get_all()
    if not isinstance(raw_cfg, (list, tuple)):
        raw_cfg = []
    config_rows = []
    for r in raw_cfg:
        try:
            config_rows.append(dict(r))
        except Exception:
            continue
    return {
        "groups": groups,
        "flags": flags,
        "config": config_rows,
    }


def default_value_for(key: str) -> Any:
    return json_safe(_default_for(resolve_spec(key)))
