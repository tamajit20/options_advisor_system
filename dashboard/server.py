"""
dashboard/server.py
===================

Flask dashboard server. Mobile-first responsive UI with 5 tabs:
    1. Suggestion (today's suggestion + mark-executed flow)
    2. My Trades (open trades, daily exit instructions, broken-trade advisor)
    3. History (past suggestions, executed trades, simulations)
    4. Logs (system logs filterable by level/module/job)
    5. Config (runtime overrides via options_config)

Boundary: imports from database + lifecycle. No engine internals.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

from flask import Flask, has_request_context, jsonify, redirect, render_template, request

# Datetimes stored in the DB are naive IST (the runtime TZ is Asia/Kolkata).
# Format them as plain readable strings — no UTC offset needed.


def _ist_iso(dt) -> str | None:
    """Format a datetime/date for API output as a plain IST string."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)

from config import DASHBOARD_CONFIG, STRATEGY_CONFIG, SCHEDULER_CONFIG
from contracts import TradeLegFill
from engine.execution_validator import validate_execution
from database.connection import SQLServerConnection
from database.log_repo import JobLogRepo, LogRepo
from database.models import (
    ConfigRepo,
    FoEodRepo,
    NotificationRepo,
    SimulationRepo,
    SpotEodRepo,
    SuggestionRepo,
    TradeRepo,
    VixRepo,
)
from engine.exit_pricing import sanitized_close_price
from lifecycle.eod_gap_replay import replay_gap_for_trade
from lifecycle.resuggestion_engine import generate_resuggestion
from lifecycle.trade_executor import close_trade_with_fills, mark_executed, supplement_trade
from utils import market_state_at, now_ist, today_ist

logger = logging.getLogger(__name__)


def public_base_url() -> str:
    """External base URL for Zerodha OAuth redirect registration and operator docs."""
    configured = (DASHBOARD_CONFIG.get("public_base_url") or "").strip().rstrip("/")
    if configured:
        return configured
    if has_request_context():
        scheme = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip()
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
        if host:
            return f"{scheme}://{host}".rstrip("/")
    port = DASHBOARD_CONFIG["port"]
    return f"http://127.0.0.1:{port}"


def zerodha_callback_url() -> str:
    """Kite Developer Console redirect URL for this deployment."""
    return f"{public_base_url()}/zerodha/callback"


def kite_redirect_https_required(redirect_url: str | None = None) -> bool:
    """True when Kite will reject this redirect URL (HTTP on non-localhost)."""
    url = redirect_url or zerodha_callback_url()
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return False
    host = (parsed.hostname or "").lower()
    return host not in ("127.0.0.1", "localhost")


def kite_console_redirect_url() -> str:
    """Redirect URL to register in Kite Developer Console for this deployment."""
    if kite_redirect_https_required(zerodha_callback_url()):
        port = DASHBOARD_CONFIG["port"]
        return f"http://127.0.0.1:{port}/zerodha/callback"
    return zerodha_callback_url()


def _zerodha_status_payload() -> dict:
    from providers.zerodha.session import is_token_valid, load_session

    callback = zerodha_callback_url()
    manual_paste = kite_redirect_https_required(callback)
    console_redirect = kite_console_redirect_url()
    base = {
        "public_base_url": public_base_url(),
        "redirect_url": callback,
        "kite_console_redirect_url": console_redirect,
        "login_path": "/zerodha/login",
        "kite_https_required": manual_paste,
        "kite_manual_paste_flow": manual_paste,
    }
    s = load_session()
    if s is None:
        return {
            **base,
            "has_session": False,
            "valid": False,
            "user_id": None,
            "generated_at": None,
        }
    return {
        **base,
        "has_session": True,
        "valid": bool(is_token_valid(s)),
        "user_id": s.user_id,
        "generated_at": s.generated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Connection helper — each request gets its own short-lived DB connection
# ---------------------------------------------------------------------------

def _with_db(fn):
    """Wrap a view function to provide an open DB connection."""
    def wrapper(*args, **kwargs):
        db = SQLServerConnection()
        try:
            db.connect()
            return fn(db, *args, **kwargs)
        finally:
            db.close()
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _json_default(o: Any):
    if isinstance(o, datetime):
        return _ist_iso(o)
    if isinstance(o, date):
        return o.isoformat()
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


def _row(d: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a DB row dict to JSON-safe dict (datetimes tagged as IST)."""
    out = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = _ist_iso(v)
        elif isinstance(v, date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


_CONFIDENCE_LEGACY_TOTAL = 7
_CONFIDENCE_EXPANDED_TOTAL = 14


def _confidence_counts(row: Dict[str, Any]) -> Optional[tuple[int, int]]:
    """Return (passed, total) confidence checks for a suggestion row."""
    cond = row.get("conditions_json")
    if cond:
        try:
            parsed = json.loads(cond) if isinstance(cond, str) else cond
            if isinstance(parsed, list) and parsed:
                fails = sum(
                    1 for c in parsed
                    if c.get("status") in ("FAIL", "SOFT_FAIL")
                )
                total = len(parsed)
                return total - fails, total
        except (json.JSONDecodeError, TypeError):
            pass
    score = row.get("confidence_score")
    if score is None:
        return None
    passed = int(score)
    total = (
        _CONFIDENCE_LEGACY_TOTAL
        if passed <= _CONFIDENCE_LEGACY_TOTAL
        else max(_CONFIDENCE_EXPANDED_TOTAL, passed)
    )
    return passed, total


def _confidence_display(row: Dict[str, Any]) -> Optional[str]:
    counts = _confidence_counts(row)
    if not counts:
        return None
    passed, total = counts
    return f"{passed}/{total}"


def _execution_gate_label(gate, row: Dict[str, Any]) -> Optional[str]:
    """Short UI badge for a blocked suggestion card."""
    if gate.ok:
        return None
    status = (row.get("status") or "").upper()
    if status == "IGNORED":
        return "Retired"
    if status == "EXECUTED":
        return "Executed"
    vstatus = (row.get("validator_status") or "").upper()
    if vstatus == "STALE_0935":
        return "Stale at open"
    for v in gate.vetoes:
        vl = v.lower()
        if "generated" in vl and "ago" in vl:
            return "Stale"
        if "strike too close" in vl:
            return "Strike too close"
        if "circuit breaker" in vl:
            return "Circuit breaker"
    if status != "PENDING":
        return status.title()
    return "Cannot execute"


def _append_quality_band_filter(
    sql: str,
    params: list,
    band: str,
    column: str = "entry_quality_score",
) -> str:
    """Filter entry_quality_score to one tier (matches dashboard badge bands)."""
    band = (band or "").strip().lower()
    if band == "excellent":
        params.append(80)
        return sql + f" AND {column} >= ? "
    if band == "good":
        params.extend([65, 79])
        return sql + f" AND {column} >= ? AND {column} <= ? "
    if band == "fair":
        params.extend([50, 64])
        return sql + f" AND {column} >= ? AND {column} <= ? "
    if band == "weak":
        params.extend([35, 49])
        return sql + f" AND {column} >= ? AND {column} <= ? "
    if band == "poor":
        params.append(35)
        return sql + f" AND {column} IS NOT NULL AND {column} < ? "
    return sql


def _append_trade_pnl_filter(sql: str, pnl: str) -> str:
    pnl = (pnl or "").strip().lower()
    if pnl == "profit":
        return sql + " AND t.net_pnl > 0 "
    if pnl == "loss":
        return sql + " AND t.net_pnl < 0 "
    if pnl == "breakeven":
        return sql + " AND t.net_pnl = 0 "
    if pnl == "unknown":
        return sql + " AND t.net_pnl IS NULL "
    return sql


def _normalize_quality_band(raw_band: str, raw_min: str = "") -> str:
    """Resolve quality_band; accept legacy ?quality_min= numeric tiers from stale UI."""
    band = (raw_band or "").strip().lower()
    if band in ("excellent", "good", "fair", "weak", "poor"):
        return band
    legacy = {"80": "excellent", "65": "good", "50": "fair", "35": "weak"}
    if band in legacy:
        return legacy[band]
    qmin = (raw_min or "").strip()
    return legacy.get(qmin, "")


def _parse_history_date_window(
    from_date_str: str,
    to_date_str: str,
    *,
    days_default: int = 30,
) -> tuple[str, str]:
    """Return inclusive YYYY-MM-DD window; invalid input falls back to last N days (IST)."""
    from_date_str = (from_date_str or "").strip()
    to_date_str = (to_date_str or "").strip()
    if from_date_str and to_date_str:
        try:
            datetime.strptime(from_date_str, "%Y-%m-%d")
            datetime.strptime(to_date_str, "%Y-%m-%d")
            return from_date_str, to_date_str
        except ValueError:
            pass
    to_d = today_ist()
    from_d = to_d - timedelta(days=days_default)
    return from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d")


# Closed-trade history filters on close date (fallback to executed_on when unset).
_CLOSED_TRADE_DATE_EXPR = "COALESCE(t.closed_on, t.executed_on)"

_INDEX_LABELS: Dict[str, str] = {
    "NIFTY": "Nifty",
    "BANKNIFTY": "Bank Nifty",
    "FINNIFTY": "Fin Nifty",
    "VIX": "India VIX",
}


def _index_ticker_symbols() -> list[str]:
    """All index symbols shown in the header strip (underlyings + VIX)."""
    seen: set[str] = set()
    out: list[str] = []
    for sym in list(STRATEGY_CONFIG.get("underlyings") or []) + ["VIX"]:
        key = str(sym).strip().upper()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _load_ws_status_snapshot() -> Optional[dict]:
    from providers.ws_monitor import default_snapshot_path

    path = default_snapshot_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _latest_live_index_quotes(
    snap: dict,
    symbols: set[str],
) -> dict[str, dict[str, Any]]:
    """Most recent index tick per symbol from ws_status recent_events."""
    out: dict[str, dict[str, Any]] = {}
    for ev in reversed(snap.get("recent_events") or []):
        if ev.get("option_type"):
            continue
        sym = str(ev.get("symbol") or "").upper()
        if sym not in symbols or sym in out:
            continue
        lp = ev.get("last_price")
        if lp is None:
            continue
        out[sym] = {
            "price": float(lp),
            "as_of": ev.get("ts"),
        }
    return out


def _ws_tick_age_seconds(snap: dict, now: datetime) -> Optional[float]:
    last_tick = snap.get("last_tick_at")
    if not last_tick:
        return None
    try:
        last_dt = datetime.fromisoformat(str(last_tick))
    except ValueError:
        return None
    if last_dt.tzinfo is not None:
        from zoneinfo import ZoneInfo

        last_dt = last_dt.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    return (now - last_dt).total_seconds()


# ---------------------------------------------------------------------------
# Job metadata (display only) — order matches the daily pipeline
# ---------------------------------------------------------------------------
_JOB_META: Dict[str, Dict[str, str]] = {
    "fo_bhav_download":   {"icon": "📥", "name": "F&O Bhavcopy Download",
                            "description": "Downloads NSE F&O EOD bhavcopy (option chain settle prices)."},
    "spot_bhav_download": {"icon": "📈", "name": "Spot Bhavcopy Download",
                            "description": "Downloads NSE cash-segment EOD spot prices for the underlying."},
    "vix_download":       {"icon": "📊", "name": "India VIX Download",
                            "description": "Downloads India VIX EOD level for volatility regime detection."},
    "fii_download":       {"icon": "🏦", "name": "FII OI Download",
                            "description": "Downloads FII derivative OI data for sentiment analysis."},
    "iv_calculation":     {"icon": "🧮", "name": "IV Calculation",
                            "description": "Computes IV / IV-rank / IV-percentile from F&O + spot data."},
    "suggestion_engine":  {"icon": "💡", "name": "Suggestion Engine",
                            "description": "Generates today's options trade suggestion across all enabled strategies."},
    "live_suggestion_engine": {"icon": "💡", "name": "Live Suggestion Engine",
                            "description": "Re-runs the suggestion engine against the live Zerodha chain at 09:45, 11:00, 13:00, and 14:30 IST (Mon–Fri)."},
    "simulation_update":  {"icon": "🎯", "name": "Simulation Update",
                            "description": "Updates daily P/L simulation for past suggestions."},
    "exit_engine":        {"icon": "🚪", "name": "Exit Engine",
                            "description": "Evaluates open trades and emits exit instructions."},
    "eod_nightly_pipeline": {"icon": "🌙", "name": "EOD Nightly Pipeline",
                            "description": "Sequential EOD chain: bhav → IV → suggestion → exit (Mon–Fri 20:35 IST)."},
    "weekly_cleanup":     {"icon": "🧹", "name": "Weekly Cleanup",
                            "description": "Applies retention policy and trims historical data."},
}

_DOW_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
               "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def _summarize_cron(cfg: Dict[str, Any]) -> str:
    """Render a SCHEDULER_CONFIG entry as a human-readable schedule string."""
    if not cfg:
        return ""
    schedules = cfg.get("schedules")
    if schedules:
        slots = [
            s for s in schedules
            if s.get("enabled", True) and s.get("hour") is not None and s.get("minute") is not None
        ]
        if not slots:
            return "—"
        times = ", ".join(
            f"{int(s['hour']):02d}:{int(s['minute']):02d}" for s in slots
        )
        dows = {str(s.get("day_of_week") or "").strip().lower() for s in slots}
        if len(dows) == 1 and dows != {""}:
            raw = next(iter(dows))
            days = ", ".join(
                _DOW_LABELS.get(d.strip().lower(), d) for d in raw.split(",")
            )
            return f"{days} @ {times} IST"
        parts = []
        for s in slots:
            h, m = int(s["hour"]), int(s["minute"])
            dow = s.get("day_of_week")
            t = f"{h:02d}:{m:02d}"
            if dow:
                days = ", ".join(
                    _DOW_LABELS.get(d.strip().lower(), d) for d in str(dow).split(",")
                )
                parts.append(f"{days} {t}")
            else:
                parts.append(t)
        return "; ".join(parts) + " IST"
    h = cfg.get("hour")
    m = cfg.get("minute")
    dow = cfg.get("day_of_week")
    time_part = ""
    if h is not None and m is not None:
        time_part = f"{int(h):02d}:{int(m):02d} IST"
    if dow:
        days = ", ".join(_DOW_LABELS.get(d.strip().lower(), d) for d in str(dow).split(","))
        return f"{days} @ {time_part}".strip(" @")
    return f"Daily @ {time_part}" if time_part else "—"


def _next_run_for_job(sch, job_name: str):
    """Earliest next_run_time across all APScheduler triggers for this logical job."""
    candidates = []
    for aps_job in sch.get_jobs():
        jid = aps_job.id or ""
        if jid == job_name or jid.startswith(f"{job_name}@"):
            if aps_job.next_run_time:
                candidates.append(aps_job.next_run_time)
    return min(candidates) if candidates else None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["JSON_SORT_KEYS"] = False
    # Never cache static assets in the browser — the cache_bust timestamp in
    # the HTML template ensures a fresh fetch on every page load anyway.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    # ---------- HTML ----------
    @app.route("/")
    def index():
        import time
        return render_template("dashboard.html",
                               theme=DASHBOARD_CONFIG["theme"],
                               port=DASHBOARD_CONFIG["port"],
                               zerodha_callback_url=zerodha_callback_url(),
                               cache_bust=int(time.time()))

    # ---------- Theme tokens ----------
    @app.route("/api/theme")
    def api_theme():
        return jsonify(DASHBOARD_CONFIG["theme"])

    # ---------- Zerodha daily login flow ----------
    # Two endpoints work together so the operator can re-mint a Kite
    # access_token without dropping into a shell:
    #   GET  /zerodha/login        — 302 redirect to Kite's OAuth login URL
    #   GET  /zerodha/callback     — Kite redirect target; exchanges request_token
    #   POST /api/zerodha/exchange — JSON {request_token}; manual fallback
    #   GET  /api/zerodha/status   — JSON snapshot of current session validity.
    #   POST /api/zerodha/logout   — clear persisted session (ws_runner exits).
    #
    # Kite Developer Console redirect URL must match zerodha_callback_url() for this host.
    @app.route("/zerodha/login")
    def zerodha_login_redirect():
        try:
            from providers.zerodha.session import build_login_url
            url = build_login_url()
        except (RuntimeError, ImportError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return redirect(url, code=302)

    @app.route("/zerodha/callback")
    def zerodha_callback():
        """OAuth return URL — exchange request_token server-side, then redirect."""
        rt = (request.args.get("request_token") or "").strip()
        status = (request.args.get("status") or "").strip().lower()
        kite_err = (request.args.get("error") or request.args.get("error_message") or "").strip()
        if kite_err or (status and status != "success"):
            msg = kite_err or f"Kite login status={status or 'unknown'}"
            return redirect(f"/?tab=wsmon&zerodha_error={quote(msg[:200])}")
        if not rt:
            return redirect("/?tab=wsmon&zerodha_error=missing_request_token")
        try:
            from providers.zerodha.session import exchange_request_token
            exchange_request_token(rt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("zerodha callback exchange failed")
            return redirect(f"/?tab=wsmon&zerodha_error={quote(str(exc)[:200])}")
        return redirect("/?tab=wsmon&zerodha=ok")

    @app.route("/api/zerodha/exchange", methods=["POST"])
    def api_zerodha_exchange():
        body = request.get_json(silent=True) or {}
        rt = (body.get("request_token") or "").strip()
        if not rt:
            return jsonify({"ok": False, "error": "request_token is required"}), 400
        try:
            from providers.zerodha.session import exchange_request_token
            session = exchange_request_token(rt)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            logger.exception("zerodha exchange failed")
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({
            "ok": True,
            "user_id": session.user_id,
            "generated_at": session.generated_at.isoformat(),
        })

    @app.route("/api/zerodha/status")
    def api_zerodha_status():
        return jsonify(_zerodha_status_payload())

    @app.route("/api/zerodha/logout", methods=["POST"])
    def api_zerodha_logout():
        """Clear the persisted Zerodha session.

            The ws_runner container watches the session file and will exit
            cleanly within ~5 seconds of the file being removed. Restart
            policy `on-failure:5` does not relaunch it (exit 0 = clean stop).
            """
        from providers.zerodha.session import clear_session
        removed = clear_session()
        return jsonify({
            "ok": True,
            "removed": bool(removed),
            "message": (
                "Session cleared. WS runner will disconnect within ~5s." if removed
                else "No persisted session found."
            ),
        })

    # ---------- Tab 1: Suggestion ----------
    @app.route("/api/suggestion/today")
    @_with_db
    def api_suggestion_today(db: SQLServerConnection):
        sug = SuggestionRepo(db)
        # Return suggestions whose execution window hasn't closed yet:
        #   - entry_date > today: execute in the future (e.g. Friday→Monday)
        #   - entry_date = today: shown until 21:30 IST (see active_pending)
        # After that, same-day PENDING is hidden; next-day EOD rows still show.
        # Falls back to legacy PENDING rows that pre-date the entry_date column.
        rows = sug.active_pending()
        # Phase 3 — #2: surface staleness so the UI can grey out / badge rows
        # whose `generated_on` is older than `suggestion_freshness_minutes`.
        from utils import now_ist as _now
        fresh_min = float(
            STRATEGY_CONFIG.get("suggestion_freshness_minutes", 30)
        )
        now = _now()
        cb_active = False
        try:
            from database.runtime_flags import (
                FLAG_CIRCUIT_BREAKER_ACTIVE,
                RuntimeFlagsRepo,
            )
            cb_active = RuntimeFlagsRepo(db, cache_ttl_seconds=0).get_bool(
                FLAG_CIRCUIT_BREAKER_ACTIVE, default=False,
            )
        except Exception:
            pass
        out = []
        for r in rows:
            r_out = _row(r)
            if "net_credit_suggested" in r_out:
                r_out["net_credit"] = r_out.pop("net_credit_suggested")
            legs_out = [_row(l) for l in sug.legs(r["suggestion_id"])]
            r_out["legs"] = legs_out
            # Add data_as_of from provenance if available
            prov = db.fetch_one(
                "SELECT data_as_of FROM options_suggestions WHERE suggestion_id = ?",
                [r["suggestion_id"]],
            )
            r_out["data_as_of"] = (
                prov["data_as_of"] if prov and prov.get("data_as_of") else None
            )
            gen_on = r.get("generated_on")
            if isinstance(gen_on, datetime) and fresh_min > 0:
                age_min = (now - gen_on).total_seconds() / 60.0
                r_out["age_minutes"] = round(age_min, 1)
                r_out["is_stale"] = age_min > fresh_min
            else:
                r_out["age_minutes"] = None
                r_out["is_stale"] = False

            gate = validate_execution(
                r, legs_out, now=now, circuit_breaker_active=cb_active,
            )
            r_out["execution_gate"] = {
                "ok": gate.ok,
                "vetoes": list(gate.vetoes),
                "warnings": list(gate.warnings),
                "reason": gate.reason(),
                "label": _execution_gate_label(gate, r),
            }
            out.append(r_out)
        from engine.market_regime import regime_from_sit_out_row, summarize_market_sit_out

        pending_underlyings = {r.get("underlying") for r in out if r.get("underlying")}
        sit_out_raw = sug.active_sit_out_today()
        sit_out: list = []
        for r in sit_out_raw:
            if r.get("underlying") in pending_underlyings:
                continue
            r_out = _row(r)
            r_out["confidence_display"] = _confidence_display(r)
            r_out["market_regime"] = regime_from_sit_out_row(r_out)
            sit_out.append(r_out)

        market_summary = summarize_market_sit_out(sit_out)

        return jsonify({
            "suggestions": out,
            "sit_out": sit_out,
            "market_summary": market_summary,
            "freshness_minutes": fresh_min,
        })

    @app.route("/api/suggestion/<sid>/mark-executed", methods=["POST"])
    @_with_db
    def api_mark_executed(db: SQLServerConnection, sid: str):
        payload = request.get_json(silent=True) or {}
        fills_in = payload.get("fills") or []
        fills = []
        for f in fills_in:
            fills.append(TradeLegFill(
                leg_order=int(f["leg_order"]),
                executed=bool(f.get("executed")),
                fill_price=float(f["fill_price"]) if f.get("fill_price") is not None else None,
                fill_time=datetime.fromisoformat(f["fill_time"]) if f.get("fill_time") else None,
                not_filled_reason=f.get("not_filled_reason"),
                lots_override=int(f["lots_override"]) if f.get("lots_override") else None,
            ))
        spot_at_exec = payload.get("spot_at_execution")
        adj_sl = payload.get("actual_stop_loss_level")
        try:
            trade_id = mark_executed(
                db, sid, fills,
                spot_at_execution=float(spot_at_exec) if spot_at_exec is not None else None,
                actual_stop_loss_level=float(adj_sl) if adj_sl is not None else None,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"trade_id": trade_id})

    # ---------- Tab 2: My Trades ----------
    @app.route("/api/trades/open")
    @_with_db
    def api_trades_open(db: SQLServerConnection):
        trd = TradeRepo(db)
        sug = SuggestionRepo(db)
        notif = NotificationRepo(db)
        rows = trd.open_trades()
        out = []
        for r in rows:
            r_out = _row(r)
            r_out["legs"] = [_row(l) for l in trd.legs_with_suggestion_info(r["trade_id"])]
            # Live risk alert (TARGET_HIT / LOSS_LIMIT_HIT / PROFIT_FLOOR_HIT /
            # PRE_BREACH_WARNING / PROFIT_FLOOR_SET) so the card can render a prominent badge instead
            # of relying solely on the notification bar.
            ra = notif.latest_risk_alert_for_trade(r["trade_id"])
            r_out["risk_alert"] = _row(ra) if ra else None
            # Attach the original suggestion so the UI can show its rationale
            if r.get("suggestion_id"):
                sug_row = sug.get(r["suggestion_id"])
                if sug_row:
                    sug_out = _row(sug_row)
                    if "net_credit_suggested" in sug_out:
                        sug_out["net_credit"] = sug_out.pop("net_credit_suggested")
                    sug_out["legs"] = [_row(l) for l in sug.legs(r["suggestion_id"])]
                    r_out["suggestion"] = sug_out
                    r_out["entry_quality_score"] = sug_out.get("entry_quality_score")
                else:
                    r_out["suggestion"] = None
                    r_out["entry_quality_score"] = None
            else:
                r_out["suggestion"] = None
                r_out["entry_quality_score"] = None
            out.append(r_out)
        return jsonify({"trades": out})

    @app.route("/api/trades/<trade_id>")
    @_with_db
    def api_trade_detail(db: SQLServerConnection, trade_id: str):
        trd = TradeRepo(db)
        r = trd.get(trade_id)
        if r is None:
            return jsonify({"error": "Not found"}), 404
        r_out = _row(r)
        r_out["legs"] = [_row(l) for l in trd.legs(trade_id)]
        return jsonify({"trade": r_out})

    @app.route("/api/trades/<trade_id>/resuggest", methods=["POST"])
    @_with_db
    def api_resuggest(db: SQLServerConnection, trade_id: str):
        try:
            inserted = generate_resuggestion(db, trade_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"inserted": inserted})

    @app.route("/api/trades/<trade_id>/remaining-legs")
    @_with_db
    def api_remaining_legs(db: SQLServerConnection, trade_id: str):
        trd = TradeRepo(db)
        legs = trd.legs_with_suggestion_info(trade_id)
        remaining = [_row(l) for l in legs if not l.get("executed")]
        return jsonify({"legs": remaining})

    @app.route("/api/trades/<trade_id>/executed-legs")
    @_with_db
    def api_executed_legs(db: SQLServerConnection, trade_id: str):
        trd = TradeRepo(db)
        legs = trd.legs_with_suggestion_info(trade_id)
        executed = [_row(l) for l in legs if l.get("executed")]
        return jsonify({"legs": executed})

    @app.route("/api/trades/<trade_id>/supplement", methods=["POST"])
    @_with_db
    def api_supplement_trade(db: SQLServerConnection, trade_id: str):
        payload = request.get_json(silent=True) or {}
        fills_in = payload.get("fills") or []
        fills = []
        for f in fills_in:
            fills.append(TradeLegFill(
                leg_order=int(f["leg_order"]),
                executed=bool(f.get("executed")),
                fill_price=float(f["fill_price"]) if f.get("fill_price") is not None else None,
                fill_time=datetime.fromisoformat(f["fill_time"]) if f.get("fill_time") else None,
                not_filled_reason=f.get("not_filled_reason"),
                lots_override=int(f["lots_override"]) if f.get("lots_override") else None,
            ))
        try:
            supplement_trade(db, trade_id, fills)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.route("/api/trades/<trade_id>/close", methods=["POST"])
    @_with_db
    def api_close_trade(db: SQLServerConnection, trade_id: str):
        payload = request.get_json(silent=True) or {}
        exits_in = payload.get("exits") or []
        exits = []
        for e in exits_in:
            exits.append({
                "leg_order":  int(e["leg_order"]),
                "exit_price": float(e["exit_price"]) if e.get("exit_price") is not None else None,
                "exit_time":  datetime.fromisoformat(e["exit_time"]) if e.get("exit_time") else None,
            })
        try:
            close_trade_with_fills(db, trade_id, exits)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.route("/api/trades/<trade_id>", methods=["DELETE"])
    @_with_db
    def api_void_trade(db: SQLServerConnection, trade_id: str):
        trd = TradeRepo(db)
        if trd.get(trade_id) is None:
            return jsonify({"error": "Not found"}), 404
        trd.void_trade(trade_id)
        db.commit()
        return jsonify({"ok": True})

    @app.route("/api/trades/<trade_id>/monitor", methods=["PATCH"])
    @_with_db
    def api_update_monitor(db: SQLServerConnection, trade_id: str):
        """Update the Nifty SL level and/or spot price at execution for a trade."""
        payload = request.get_json(silent=True) or {}
        sl = payload.get("actual_stop_loss_level")
        spot = payload.get("spot_at_execution")
        trd = TradeRepo(db)
        trd.update_monitor(
            trade_id,
            float(sl) if sl is not None else None,
            float(spot) if spot is not None else None,
        )
        db.commit()
        return jsonify({"ok": True})

    @app.route("/api/trades/<trade_id>/gap-replay")
    @_with_db
    def api_trade_gap_replay(db: SQLServerConnection, trade_id: str):
        """EOD MTM replay for weekdays after the last live monitor snapshot."""
        payload = replay_gap_for_trade(db, trade_id)
        err = payload.get("error")
        if err == "not_found":
            return jsonify({"error": "Not found"}), 404
        if err:
            return jsonify({"error": err.replace("_", " ")}), 400
        return jsonify(payload)

    @app.route("/api/trades/<trade_id>/close-suggestion")
    @_with_db
    def api_close_suggestion(db: SQLServerConnection, trade_id: str):
        """Suggest per-leg closing prices using the latest chain mid prices.
        Returns: {legs: [{leg_order, suggested_price, action, ...}], est_gross_pnl}
        """
        trd = TradeRepo(db)
        trade = trd.get(trade_id)
        if trade is None:
            return jsonify({"error": "unknown trade"}), 404
        legs = trd.legs_with_suggestion_info(trade_id)
        executed = [l for l in legs if l.get("executed")]
        if not executed:
            return jsonify({"legs": [], "est_gross_pnl": 0.0})
        underlying = executed[0]["symbol"]
        expiry = executed[0]["expiry_date"]
        today = today_ist()
        fo = FoEodRepo(db)
        # During live market hours today's bhavcopy hasn't been published yet.
        # Fall back to the most recent available date so we don't return all-zeros.
        chain = fo.get_chain(underlying, today, expiry)
        if not chain:
            from datetime import timedelta
            for delta in (1, 2, 3, 4, 5):
                chain = fo.get_chain(underlying, today - timedelta(days=delta), expiry)
                if chain:
                    break
        as_of = today
        chain_mid = {
            (float(c["strike"]), c["option_type"]):
                float(c.get("settle_price") or c.get("close_price") or 0.0)
            for c in chain
        }
        # Look up underlying spot so sanitized_close_price can sanity-check
        # each leg's mid against intrinsic. Without this the close-suggestion
        # endpoint used to pre-fill the Close Trade modal with absurd values
        # whenever options_fo_eod had a row with settle_price ≈ spot (seen
        # on expired contracts), making the dashboard report ₹35-lakh of
        # phantom profit on simple straddles.
        spot_row = SpotEodRepo(db).for_date(underlying, today)
        if spot_row is None:
            from datetime import timedelta
            for delta in (1, 2, 3, 4, 5):
                spot_row = SpotEodRepo(db).for_date(underlying, today - timedelta(days=delta))
                if spot_row:
                    break
        spot_close = float(spot_row["close_price"]) if spot_row else None
        out = []
        est = 0.0
        for l in executed:
            raw_mid = chain_mid.get((float(l["strike"]), l["option_type"]), 0.0)
            mid, src = sanitized_close_price(
                option_type=l["option_type"],
                strike=float(l["strike"]),
                raw_mid=raw_mid,
                spot=spot_close,
            )
            if src == "intrinsic_fallback":
                logger.warning(
                    "close-suggestion: bogus settle for %s %s%s (raw=%.2f) — using intrinsic %.2f",
                    underlying, l["strike"], l["option_type"], raw_mid, mid,
                )
            lots = int(l.get("lots_actual") or l.get("lots") or 0)
            qty = lots * int(l.get("lot_size") or 0)
            fill = float(l.get("fill_price") or 0.0)
            if l["action"] == "SELL":
                est += (fill - mid) * qty
            else:
                est += (mid - fill) * qty
            out.append({
                "leg_order":       l["leg_order"],
                "action":          l["action"],
                "symbol":          l["symbol"],
                "strike":          float(l["strike"]),
                "option_type":     l["option_type"],
                "fill_price":      fill,
                "lots":            lots,
                "suggested_close": round(mid, 2),
                "price_source":    src,  # "mid" | "intrinsic_fallback"
            })
        return jsonify({"legs": out, "est_gross_pnl": round(est, 2)})

    # ---------- Tab 3: History ----------
    @app.route("/api/history/suggestions")
    @_with_db
    def api_history_suggestions(db: SQLServerConnection):
        from_date  = request.args.get("from_date")
        to_date    = request.args.get("to_date")
        underlying = request.args.get("underlying", "").strip()
        status_f   = request.args.get("status", "").strip().upper()

        # Fallback: legacy ?days= support
        if not from_date:
            days = int(request.args.get("days", 30))
            from_date = (today_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = today_ist().strftime("%Y-%m-%d")

        valid_statuses = {"PENDING", "EXECUTED", "IGNORED", "NO_SUGGESTION"}
        if status_f and status_f not in valid_statuses:
            status_f = ""

        filters: list[str] = ["CONVERT(date, generated_on) >= ?", "CONVERT(date, generated_on) <= ?"]
        params: list = [from_date, to_date]
        if underlying:
            filters.append("underlying = ?")
            params.append(underlying)
        if status_f:
            filters.append("status = ?")
            params.append(status_f)
        else:
            # Default: show all non-trivial statuses
            filters.append("status IN ('PENDING','EXECUTED','IGNORED')")

        strategy_f = request.args.get("strategy", "").strip()
        if strategy_f:
            filters.append("strategy = ?")
            params.append(strategy_f)

        quality_band = _normalize_quality_band(
            request.args.get("quality_band", ""),
            request.args.get("quality_min", ""),
        )
        where = " AND ".join(filters)
        sql = f"SELECT TOP 300 * FROM options_suggestions WHERE {where} "
        qparams = list(params)
        sql = _append_quality_band_filter(sql, qparams, quality_band)
        rows = db.fetch_all(sql + " ORDER BY generated_on DESC", qparams)
        suggestions = []
        for r in rows:
            r_out = _row(r)
            r_out["confidence_display"] = _confidence_display(r)
            suggestions.append(r_out)
        # Facet lists for the date window (ignore outcome/quality filters)
        facet_filters: list[str] = [
            "CONVERT(date, generated_on) >= ?",
            "CONVERT(date, generated_on) <= ?",
        ]
        facet_params: list = [from_date, to_date]
        if status_f:
            facet_filters.append("status = ?")
            facet_params.append(status_f)
        elif not request.args.get("status"):
            facet_filters.append("status IN ('PENDING','EXECUTED','IGNORED')")
        facet_where = " AND ".join(facet_filters)
        facet_rows = db.fetch_all(
            f"SELECT underlying, strategy FROM options_suggestions WHERE {facet_where}",
            facet_params,
        )
        underlyings = sorted({r["underlying"] for r in facet_rows if r.get("underlying")})
        strategies = sorted({r["strategy"] for r in facet_rows if r.get("strategy")})
        return jsonify({
            "suggestions": suggestions,
            "underlyings": underlyings,
            "strategies": strategies,
            "count": len(suggestions),
        })

    @app.route("/api/history/trades")
    @_with_db
    def api_history_trades(db: SQLServerConnection):
        days = int(request.args.get("days", 30))
        since = (today_ist() - timedelta(days=days))
        rows = db.fetch_all(
            "SELECT TOP 200 * FROM options_trades "
            "WHERE executed_on >= ? AND status IN ('CLOSED', 'EXPIRED') "
            "ORDER BY executed_on DESC",
            [since],
        )
        return jsonify({"trades": [_row(r) for r in rows]})

    @app.route("/api/stats/pnl-timeline")
    @_with_db
    def api_pnl_timeline(db: SQLServerConnection):
        """Per-trade P&L timeline for charts.

        Returns trades sorted by close date with cumulative P&L columns
        pre-computed so the frontend only needs to render, not aggregate.
        Optional query params: from_date, to_date (YYYY-MM-DD).
        """
        from_date = request.args.get("from_date") or None
        to_date   = request.args.get("to_date")   or None

        filters = ["t.status IN ('CLOSED', 'EXPIRED')", "t.net_pnl IS NOT NULL",
                   "t.closed_on IS NOT NULL"]
        params: list = []
        if from_date:
            filters.append("CONVERT(date, t.closed_on) >= ?")
            params.append(from_date)
        if to_date:
            filters.append("CONVERT(date, t.closed_on) <= ?")
            params.append(to_date)

        where = " AND ".join(filters)
        rows = db.fetch_all(
            f"SELECT t.trade_id, t.trade_name, t.closed_on, t.executed_on, "
            f"       t.net_pnl, t.gross_pnl, t.total_charges, t.net_credit_actual, "
            f"       COALESCE(s.strategy, 'UNKNOWN') AS strategy, "
            f"       COALESCE(s.underlying, '') AS underlying "
            f"FROM options_trades t "
            f"LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id "
            f"WHERE {where} "
            f"ORDER BY t.closed_on ASC",
            params,
        )

        trades = []
        cum_overall = 0.0
        cum_by_strategy: dict = {}
        total_invested = 0.0

        for r in rows:
            pnl    = float(r["net_pnl"])
            credit = float(r["net_credit_actual"] or 0)
            strat  = r["strategy"]
            cum_overall += pnl
            cum_by_strategy[strat] = cum_by_strategy.get(strat, 0.0) + pnl
            # "Invested" = absolute premium collected/paid (capital at risk per trade)
            total_invested += abs(credit)
            trades.append({
                "trade_id":        r["trade_id"],
                "trade_name":      r["trade_name"],
                "closed_on":       _row(r)["closed_on"],
                "executed_on":     _row(r)["executed_on"],
                "strategy":        strat,
                "underlying":      r["underlying"],
                "net_pnl":         round(pnl, 2),
                "gross_pnl":       round(float(r["gross_pnl"] or 0), 2),
                "total_charges":   round(float(r["total_charges"] or 0), 2),
                "net_credit_actual": round(credit, 2),
                "cum_pnl_overall": round(cum_overall, 2),
                "cum_pnl_strategy": round(cum_by_strategy[strat], 2),
            })

        strategies = sorted(cum_by_strategy.keys())
        return jsonify({
            "trades":          trades,
            "strategies":      strategies,
            "total_pnl":       round(cum_overall, 2),
            "total_invested":  round(total_invested, 2),
            "total_charges":   round(sum(float(r["total_charges"] or 0) for r in rows), 2),
        })

    @app.route("/api/stats/strategy-performance")
    @_with_db
    def api_strategy_performance(db: SQLServerConnection):
        """Aggregate closed trade stats grouped by strategy.

        Returns per-strategy: trade count, wins, losses, win rate, avg/total
        net P&L, avg hold days, best/worst trade, and an overall summary row.
        """
        rows = db.fetch_all(
            "SELECT t.trade_id, t.net_pnl, t.gross_pnl, t.total_charges, "
            "       t.executed_on, t.closed_on, t.net_credit_actual, "
            "       t.actual_max_profit, "
            "       COALESCE(s.strategy, 'UNKNOWN') AS strategy, "
            "       COALESCE(s.underlying, t.trade_name) AS underlying "
            "FROM options_trades t "
            "LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id "
            "WHERE t.status IN ('CLOSED', 'EXPIRED') "
            "  AND t.net_pnl IS NOT NULL "
            "ORDER BY t.executed_on DESC"
        )

        from collections import defaultdict
        buckets: dict = defaultdict(list)
        for r in rows:
            buckets[r["strategy"]].append(r)

        def _hold_days(r):
            ex = r.get("executed_on")
            cl = r.get("closed_on")
            if ex and cl:
                try:
                    delta = (cl - ex) if hasattr(cl, "days") else None
                    if delta is None:
                        from datetime import datetime as _dt
                        delta = _dt.fromisoformat(str(cl)) - _dt.fromisoformat(str(ex))
                    return max(0, delta.days)
                except Exception:
                    pass
            return None

        strategy_stats = []
        all_pnls = []
        for strategy, trades in sorted(buckets.items()):
            pnls = [float(t["net_pnl"]) for t in trades]
            all_pnls.extend(pnls)
            wins   = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            hold_days = [d for d in (_hold_days(t) for t in trades) if d is not None]
            best  = max(pnls)
            worst = min(pnls)
            avg_max_profit = None
            mps = [float(t["actual_max_profit"]) for t in trades if t.get("actual_max_profit")]
            if mps:
                avg_max_profit = round(sum(mps) / len(mps), 2)
            strategy_stats.append({
                "strategy":       strategy,
                "total":          len(trades),
                "wins":           len(wins),
                "losses":         len(losses),
                "win_rate":       round(len(wins) / len(trades) * 100, 1) if trades else 0,
                "total_pnl":      round(sum(pnls), 2),
                "avg_pnl":        round(sum(pnls) / len(pnls), 2),
                "avg_win":        round(sum(wins)   / len(wins),   2) if wins   else 0,
                "avg_loss":       round(sum(losses) / len(losses), 2) if losses else 0,
                "best_trade":     round(best,  2),
                "worst_trade":    round(worst, 2),
                "avg_hold_days":  round(sum(hold_days) / len(hold_days), 1) if hold_days else None,
                "avg_max_profit": avg_max_profit,
                "profit_factor":  round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
            })

        # Overall summary across all strategies
        overall_wins   = [p for p in all_pnls if p > 0]
        overall_losses = [p for p in all_pnls if p <= 0]
        overall = {
            "total":      len(all_pnls),
            "wins":       len(overall_wins),
            "losses":     len(overall_losses),
            "win_rate":   round(len(overall_wins) / len(all_pnls) * 100, 1) if all_pnls else 0,
            "total_pnl":  round(sum(all_pnls), 2),
            "avg_pnl":    round(sum(all_pnls) / len(all_pnls), 2) if all_pnls else 0,
            "best_trade": round(max(all_pnls), 2) if all_pnls else 0,
            "worst_trade":round(min(all_pnls), 2) if all_pnls else 0,
            "profit_factor": round(sum(overall_wins) / abs(sum(overall_losses)), 2)
                             if overall_losses and sum(overall_losses) != 0 else None,
        }
        return jsonify({"strategies": strategy_stats, "overall": overall})

    @app.route("/api/history/paired")
    @_with_db
    def api_history_paired(db: SQLServerConnection):
        days = int(request.args.get("days", 30))
        since = today_ist() - timedelta(days=days)
        rows = db.fetch_all(
            "SELECT TOP 200 "
            "  s.suggestion_id, s.underlying, s.strategy, s.generated_on, s.plain_english, "
            "  s.confidence_score, s.net_credit_suggested, s.status AS sug_status, "
            "  s.trade_name AS sug_trade_name, "
            "  t.trade_id, t.trade_name, t.executed_on, t.net_credit_actual, t.net_pnl, "
            "  t.status AS trade_status, t.closed_on, t.exit_instruction, t.position_type "
            "FROM options_suggestions s "
            "LEFT JOIN options_trades t ON t.suggestion_id = s.suggestion_id "
            "WHERE s.generated_on >= ? AND s.status IN ('EXECUTED', 'IGNORED') "
            "ORDER BY s.generated_on DESC",
            [since],
        )
        sug_repo = SuggestionRepo(db)
        trd_repo = TradeRepo(db)
        pairs = []
        for r in rows:
            r = _row(r)
            sug_legs = [_row(lg) for lg in sug_repo.legs(r["suggestion_id"])]
            trade_legs = []
            if r.get("trade_id"):
                trade_legs = [_row(lg) for lg in trd_repo.legs(r["trade_id"])]
            trade = {
                "trade_id":          r.get("trade_id"),
                "trade_name":        r.get("trade_name"),
                "executed_on":       r.get("executed_on"),
                "net_credit_actual": r.get("net_credit_actual"),
                "net_pnl":           r.get("net_pnl"),
                "status":            r.get("trade_status"),
                "closed_on":         r.get("closed_on"),
                "exit_instruction":  r.get("exit_instruction"),
                "position_type":     r.get("position_type"),
                "legs":              trade_legs,
            } if r.get("trade_id") else None
            pairs.append({
                "suggestion": {
                    "suggestion_id":    r["suggestion_id"],
                    "underlying":       r["underlying"],
                    "strategy":         r["strategy"],
                    "generated_on":     r["generated_on"],
                    "plain_english":    r["plain_english"],
                    "confidence_score": r["confidence_score"],
                    "net_credit":      r["net_credit_suggested"],
                    "status":           r["sug_status"],
                    "trade_name":       r["sug_trade_name"],
                    "legs":             sug_legs,
                },
                "trade": trade,
            })
        return jsonify({"pairs": pairs})

    @app.route("/api/history/closed-trades")
    @_with_db
    def api_history_closed_trades(db: SQLServerConnection):
        from_date_str, to_date_str = _parse_history_date_window(
            request.args.get("from_date", ""),
            request.args.get("to_date", ""),
            days_default=int(request.args.get("days", 30)),
        )
        underlying = request.args.get("underlying", "").strip()
        strategy_f = request.args.get("strategy", "").strip()
        pnl_f      = request.args.get("pnl", "").strip()
        quality_band = _normalize_quality_band(
            request.args.get("quality_band", ""),
            request.args.get("quality_min", ""),
        )

        sql = (
            "SELECT t.trade_id, t.suggestion_id, t.trade_name, t.executed_on, t.closed_on, "
            "  t.status, t.position_type, t.net_credit_actual, t.gross_pnl, t.net_pnl, "
            "  t.total_charges, t.spot_at_execution, t.exit_instruction, "
            "  t.actual_max_profit, t.actual_max_loss, "
            "  t.actual_upper_breakeven, t.actual_lower_breakeven, t.actual_stop_loss_level, "
            "  s.underlying, s.strategy, s.generated_on AS sug_generated_on, "
            "  s.net_credit_suggested AS sug_net_credit, s.confidence_score AS sug_confidence, "
            "  s.spot_at_generation AS sug_spot, s.trade_name AS sug_trade_name, "
            "  s.upper_breakeven, s.lower_breakeven, s.stop_loss_level, "
            "  s.max_profit AS sug_max_profit, s.max_loss AS sug_max_loss, "
            "  s.probability_of_profit AS sug_pop, "
            "  s.estimated_charges_total AS sug_est_charges, "
            "  s.estimated_net_pnl AS sug_est_net_pnl, "
            "  s.expiry_date AS sug_expiry, s.dte AS sug_dte, "
            "  s.entry_quality_score AS sug_entry_quality "
            "FROM options_trades t "
            "LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id "
            "WHERE t.status IN ('CLOSED', 'EXPIRED') "
            f"  AND CONVERT(date, {_CLOSED_TRADE_DATE_EXPR}) >= ? "
            f"  AND CONVERT(date, {_CLOSED_TRADE_DATE_EXPR}) <= ? "
        )
        params = [from_date_str, to_date_str]
        if underlying:
            sql += " AND s.underlying = ? "
            params.append(underlying)
        if strategy_f:
            sql += " AND s.strategy = ? "
            params.append(strategy_f)
        sql = _append_trade_pnl_filter(sql, pnl_f)
        sql = _append_quality_band_filter(sql, params, quality_band, column="s.entry_quality_score")
        sql += "ORDER BY COALESCE(t.closed_on, t.executed_on) DESC"

        rows = db.fetch_all(sql, params)
        trd_repo = TradeRepo(db)
        sug_repo = SuggestionRepo(db)

        out = []
        seen_sug = {}
        for r in rows:
            r = _row(r)
            trade_legs = [_row(lg) for lg in trd_repo.legs_with_suggestion_info(r["trade_id"])]
            sid = r.get("suggestion_id")
            if sid and sid not in seen_sug:
                seen_sug[sid] = [_row(lg) for lg in sug_repo.legs(sid)]
            sug_legs = seen_sug.get(sid, [])
            out.append({
                "trade_id":          r["trade_id"],
                "trade_name":        r["trade_name"],
                "executed_on":       r["executed_on"],
                "closed_on":         r["closed_on"],
                "status":            r["status"],
                "position_type":     r["position_type"],
                "net_credit_actual": r["net_credit_actual"],
                "gross_pnl":         r["gross_pnl"],
                "total_charges":     r["total_charges"],
                "net_pnl":           r["net_pnl"],
                "spot_at_execution": r["spot_at_execution"],
                "exit_instruction":  r["exit_instruction"],
                "actual_max_profit": r["actual_max_profit"],
                "actual_max_loss":   r["actual_max_loss"],
                "actual_upper_be":   r["actual_upper_breakeven"],
                "actual_lower_be":   r["actual_lower_breakeven"],
                "actual_stop_loss":  r["actual_stop_loss_level"],
                "entry_quality_score": r.get("sug_entry_quality"),
                "legs":              trade_legs,
                "suggestion": {
                    "underlying":  r.get("underlying"),
                    "strategy":    r.get("strategy"),
                    "generated_on":r.get("sug_generated_on"),
                    "net_credit":  r.get("sug_net_credit"),
                    "confidence":  r.get("sug_confidence"),
                    "spot":        r.get("sug_spot"),
                    "upper_be":    r.get("upper_breakeven"),
                    "lower_be":    r.get("lower_breakeven"),
                    "stop_loss":   r.get("stop_loss_level"),
                    "max_profit":  r.get("sug_max_profit"),
                    "max_loss":    r.get("sug_max_loss"),
                    "pop":         r.get("sug_pop"),
                    "est_charges": r.get("sug_est_charges"),
                    "est_net_pnl": r.get("sug_est_net_pnl"),
                    "expiry":      r.get("sug_expiry"),
                    "dte":         r.get("sug_dte"),
                    "entry_quality_score": r.get("sug_entry_quality"),
                    "legs":        sug_legs,
                } if r.get("underlying") else None,
            })

        # Distinct underlyings / strategies for filter dropdowns (date window only)
        facet_sql = (
            "SELECT DISTINCT s.underlying, s.strategy FROM options_trades t "
            "LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id "
            "WHERE t.status IN ('CLOSED','EXPIRED') "
            f"  AND CONVERT(date, {_CLOSED_TRADE_DATE_EXPR}) >= ? "
            f"  AND CONVERT(date, {_CLOSED_TRADE_DATE_EXPR}) <= ? "
            "  AND s.underlying IS NOT NULL"
        )
        facet_rows = db.fetch_all(facet_sql, [from_date_str, to_date_str])
        underlyings = sorted({u["underlying"] for u in facet_rows if u.get("underlying")})
        strategies = sorted({u["strategy"] for u in facet_rows if u.get("strategy")})
        return jsonify({"trades": out, "underlyings": underlyings, "strategies": strategies, "count": len(out)})

    @app.route("/api/history/simulation/<sid>")
    @_with_db
    def api_history_sim(db: SQLServerConnection, sid: str):
        sim = SimulationRepo(db)
        s = sim.get_summary(sid)
        legs = sim.get_legs(sid)
        return jsonify({
            "summary": _row(s) if s else None,
            "legs":    [_row(l) for l in legs],
        })

    # ---------- Tab 4: Logs ----------
    @app.route("/api/logs")
    @_with_db
    def api_logs(db: SQLServerConnection):
        repo = LogRepo(db)
        level = request.args.get("level") or None
        module = request.args.get("module") or None
        job_id = request.args.get("job_id") or None
        search = request.args.get("search") or None
        limit = int(request.args.get("limit", DASHBOARD_CONFIG["log_page_size"]))
        offset = int(request.args.get("offset", 0))
        since_h = request.args.get("since_hours")
        since = (now_ist() - timedelta(hours=int(since_h))) if since_h else None
        rows = repo.fetch(level=level, module=module, job_id=job_id,
                          since=since, search=search, limit=limit, offset=offset)
        return jsonify({"logs": [_row(r) for r in rows]})

    @app.route("/api/logs/level-counts")
    @_with_db
    def api_log_counts(db: SQLServerConnection):
        repo = LogRepo(db)
        hours = int(request.args.get("hours", 24))
        return jsonify(repo.counts_by_level(since_hours=hours))

    @app.route("/api/jobs/latest")
    @_with_db
    def api_jobs_latest(db: SQLServerConnection):
        repo = JobLogRepo(db)
        return jsonify({"jobs": [_row(r) for r in repo.latest_status_per_job()]})

    # ---------- Tab 5: Config ----------
    @app.route("/api/config")
    @_with_db
    def api_config_list(db: SQLServerConnection):
        return jsonify({"config": [_row(r) for r in ConfigRepo(db).get_all()]})

    @app.route("/api/config/<key>", methods=["GET"])
    @_with_db
    def api_config_get(db: SQLServerConnection, key: str):
        return jsonify({"key": key, "value": ConfigRepo(db).get(key)})

    @app.route("/api/config/<key>", methods=["PUT"])
    @_with_db
    def api_config_set(db: SQLServerConnection, key: str):
        payload = request.get_json(silent=True) or {}
        value = payload.get("value")
        if value is None:
            return jsonify({"error": "Missing 'value'"}), 400
        ConfigRepo(db).set(
            key=key, value=value,
            category=payload.get("category"),
            description=payload.get("description"),
        )
        db.commit()
        return jsonify({"ok": True})

    # ---------- Notifications ----------
    @app.route("/api/notifications")
    @_with_db
    def api_notifications(db: SQLServerConnection):
        args = request.args
        unread_only = args.get("unread") == "1"
        severity    = args.get("severity") or None       # CRITICAL / WARNING / INFO
        category    = args.get("category") or None       # sl / profit / exit / event / system / suggestion
        notif_type  = args.get("type") or None           # exact notif_type
        trade_id    = args.get("trade_id") or None
        from_date   = args.get("from") or None
        to_date     = args.get("to") or None
        limit       = min(int(args.get("limit") or 100), 500)
        offset      = int(args.get("offset") or 0)

        from_dt = datetime.fromisoformat(from_date) if from_date else None
        to_dt   = datetime.fromisoformat(to_date)   if to_date   else None

        repo = NotificationRepo(db)

        def _notif_row_simple(r):
            out = _row(r)
            out["is_read"] = r.get("read_at") is not None
            out.pop("_rn", None)
            return out

        # Legacy ?unread=1 path (used by global banner refresh)
        if unread_only and not any([severity, category, notif_type, trade_id, from_dt, to_dt]):
            rows = repo.unread(limit=limit)
            return jsonify({"notifications": [_notif_row_simple(r) for r in rows], "total": len(rows)})

        rows = repo.filtered(
            severity=severity,
            category=category,
            notif_type=notif_type,
            unread_only=unread_only,
            trade_id=trade_id,
            from_dt=from_dt,
            to_dt=to_dt,
            limit=limit,
            offset=offset,
        )
        total = repo.count_filtered(
            severity=severity,
            category=category,
            unread_only=unread_only,
            trade_id=trade_id,
            from_dt=from_dt,
            to_dt=to_dt,
        )

        return jsonify({
            "notifications": [_notif_row_simple(r) for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        })

    @app.route("/api/notifications/stats")
    @_with_db
    def api_notifications_stats(db: SQLServerConnection):
        """Return unread counts by severity and category for badge chips."""
        return jsonify(NotificationRepo(db).stats())

    @app.route("/api/notifications/<int:nid>/read", methods=["POST"])
    @_with_db
    def api_notifications_read(db: SQLServerConnection, nid: int):
        NotificationRepo(db).mark_read(nid)
        db.commit()
        return jsonify({"ok": True})

    @app.route("/api/notifications/read-all", methods=["POST"])
    @_with_db
    def api_notifications_read_all(db: SQLServerConnection):
        NotificationRepo(db).mark_all_read()
        db.commit()
        return jsonify({"ok": True})

    # ---------- Tab 6: Jobs (monitor + manual trigger) ----------
    @app.route("/api/jobs/list")
    @_with_db
    def api_jobs_list(db: SQLServerConnection):
        from scheduler.scheduler import (
            JOB_FUNCS as _JOB_FUNCS,
            _LAST_STATUS as _LAST,
            get_scheduler,
        )

        sch = get_scheduler()
        sch_running = bool(sch and sch.running)

        # Latest DB row per job_name (status, started, finished, error)
        repo = JobLogRepo(db)
        latest_rows = {r["job_name"]: r for r in repo.latest_status_per_job()}

        # Schedule config (cron triggers)
        cfg_jobs = SCHEDULER_CONFIG.get("jobs", {})

        out = []
        for name in _JOB_FUNCS.keys():
            cfg = cfg_jobs.get(name, {}) or {}
            meta = _JOB_META.get(name, {})

            # Next scheduled run from APScheduler (earliest of multi-trigger jobs)
            next_run = None
            if sch_running:
                next_run = _next_run_for_job(sch, name)

            # Determine display status
            row = latest_rows.get(name) or {}
            db_status = row.get("status") or ""
            mem_status = _LAST.get(name) or ""
            # Mem reflects most recent in-process state; DB row may be a stale
            # "RUNNING" if the worker died. Trust DB for finished states.
            if db_status == "RUNNING":
                disp = "RUNNING"
            elif db_status in ("SUCCESS", "FAILED", "SKIPPED"):
                disp = db_status
            elif mem_status:
                disp = mem_status
            else:
                disp = "NEVER"

            out.append({
                "job_name":      name,
                "display_name":  meta.get("name", name.replace("_", " ").title()),
                "icon":          meta.get("icon", "⚙️"),
                "description":   meta.get("description", ""),
                "schedule":      _summarize_cron(cfg),
                "enabled":       bool(cfg.get("enabled", True)),
                "status":        disp,
                "started_at":    _ist_iso(row.get("started_at")),
                "finished_at":   _ist_iso(row.get("finished_at")),
                "error_message": row.get("error_message") or "",
                "rows_processed": row.get("rows_processed"),
                "next_run":      _ist_iso(next_run),
            })

        return jsonify({
            "jobs": out,
            "scheduler_running": sch_running,
            "generated_at": _ist_iso(now_ist()),
        })

    @app.route("/api/jobs/<job_name>/trigger", methods=["POST"])
    @_with_db
    def api_jobs_trigger(db: SQLServerConnection, job_name: str):
        from scheduler.scheduler import JOB_FUNCS as _JOB_FUNCS, trigger_job_now

        if job_name not in _JOB_FUNCS:
            return jsonify({"error": f"Unknown job: {job_name}"}), 400

        # Block if already RUNNING (per latest DB row)
        latest = JobLogRepo(db).last_status(job_name)
        if latest == "RUNNING":
            return jsonify({"error": "Job is already running"}), 409

        # Optional trade_date override from JSON body: { "trade_date": "YYYY-MM-DD" }
        trade_date: str | None = None
        body = request.get_json(silent=True) or {}
        raw_td = body.get("trade_date")
        if raw_td:
            try:
                from datetime import date as _date
                _date.fromisoformat(str(raw_td))  # validate format
                trade_date = str(raw_td)
            except ValueError:
                return jsonify({"error": f"Invalid trade_date format: {raw_td!r} — use YYYY-MM-DD"}), 400

        try:
            ok = trigger_job_now(job_name, trade_date=trade_date)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        if not ok:
            return jsonify({"error": "Could not dispatch"}), 500
        return jsonify({"status": "queued", "job_name": job_name,
                        "trade_date": trade_date or "auto"})

    @app.route("/api/jobs/<job_name>/history")
    @_with_db
    def api_jobs_history(db: SQLServerConnection, job_name: str):
        limit = int(request.args.get("limit", 20))
        rows = db.fetch_all(
            "SELECT TOP (?) job_id, job_name, started_at, finished_at, status, "
            "rows_processed, error_message "
            "FROM options_job_log WHERE job_name = ? ORDER BY started_at DESC",
            [limit, job_name],
        )
        return jsonify({"runs": [_row(r) for r in rows]})

    # ---------- Runtime flags (Phase 4) ----------
    @app.route("/api/runtime-flags")
    @_with_db
    def api_runtime_flags_list(db: SQLServerConnection):
        from database.runtime_flags import RuntimeFlagsRepo
        repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
        return jsonify({
            "flags": [
                {
                    "key":           f.key,
                    "value":         f.value,
                    "type":          f.type,
                    "description":   f.description,
                    "last_modified": _ist_iso(f.last_modified) if f.last_modified else None,
                    "modified_by":   f.modified_by,
                }
                for f in repo.all()
            ]
        })

    @app.route("/api/runtime-flags/<flag_key>", methods=["POST"])
    @_with_db
    def api_runtime_flags_set(db: SQLServerConnection, flag_key: str):
        from database.runtime_flags import RuntimeFlagsRepo
        payload = request.get_json(silent=True) or {}
        if "value" not in payload:
            return jsonify({"error": "missing 'value' in body"}), 400
        repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
        try:
            repo.set(flag_key, payload["value"], modified_by="dashboard")
            db.commit()
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (ValueError, TypeError) as exc:
            db.rollback()
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "key": flag_key, "value": payload["value"]})

    # ---------- WS Monitor (Phase 2b telemetry surface) ------------------
    @app.route("/api/indices/spot")
    @_with_db
    def api_indices_spot(db: SQLServerConnection):
        """Header index strip: live Zerodha ticks when fresh, else latest EOD close."""
        now = now_ist()
        ms = market_state_at(now)
        session_open = ms in ("OPEN_VOLATILE", "OPEN_STABLE", "CLOSE_AUCTION")
        symbols = _index_ticker_symbols()
        want = set(symbols)

        snap = _load_ws_status_snapshot()
        live_quotes = _latest_live_index_quotes(snap, want) if snap else {}
        tick_age = _ws_tick_age_seconds(snap, now) if snap else None
        ws_connected = bool(
            snap
            and snap.get("connection_state") == "connected"
            and not snap.get("token_expired")
        )
        live_fresh = (
            ws_connected
            and tick_age is not None
            and tick_age <= (120.0 if session_open else 1800.0)
        )

        spot_repo = SpotEodRepo(db)
        vix_repo = VixRepo(db)
        indices: list[dict[str, Any]] = []

        for sym in symbols:
            label = _INDEX_LABELS.get(sym, sym)
            live = live_quotes.get(sym)
            use_live = bool(live_fresh and live and live.get("price") is not None)

            if use_live:
                raw_ts = live.get("as_of")
                live_as_of = None
                if raw_ts:
                    try:
                        dt = datetime.fromisoformat(str(raw_ts))
                        if dt.tzinfo is not None:
                            from zoneinfo import ZoneInfo
                            dt = dt.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
                        live_as_of = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        live_as_of = str(raw_ts)
                indices.append({
                    "symbol": sym,
                    "label": label,
                    "price": round(float(live["price"]), 2),
                    "source": "live",
                    "as_of": live_as_of,
                    "trade_date": None,
                })
                continue

            if sym == "VIX":
                row = vix_repo.latest()
                if row:
                    indices.append({
                        "symbol": sym,
                        "label": label,
                        "price": round(float(row["close_price"]), 2),
                        "source": "eod",
                        "as_of": None,
                        "trade_date": _ist_iso(row.get("trade_date")),
                    })
                else:
                    indices.append({
                        "symbol": sym,
                        "label": label,
                        "price": None,
                        "source": "unavailable",
                        "as_of": None,
                        "trade_date": None,
                    })
                continue

            row = spot_repo.latest(sym)
            if row:
                indices.append({
                    "symbol": sym,
                    "label": label,
                    "price": round(float(row["close_price"]), 2),
                    "source": "eod",
                    "as_of": None,
                    "trade_date": _ist_iso(row.get("trade_date")),
                })
            else:
                indices.append({
                    "symbol": sym,
                    "label": label,
                    "price": None,
                    "source": "unavailable",
                    "as_of": None,
                    "trade_date": None,
                })

        any_live = any(i.get("source") == "live" for i in indices)
        return jsonify({
            "as_of": _ist_iso(now),
            "session": ms,
            "session_open": session_open,
            "feed": "live" if any_live else "eod",
            "ws_connected": ws_connected,
            "indices": indices,
        })

    @app.route("/api/ws/monitor")
    def api_ws_monitor():
        """Read-only telemetry from the WS runner.

        Reads `data/ws_status.json` written by `providers/ws_monitor.py`
        inside the ws_runner container. Performs ZERO Zerodha calls.

        Optional query params:
            ?topic=tick|connection_state|token_expired   filter recent_events
            ?symbol=NIFTY                                filter recent_events
            ?limit=50                                    cap recent_events
        """
        from providers.ws_monitor import default_snapshot_path
        path = default_snapshot_path()
        if not path.exists():
            return jsonify({
                "available": False,
                "reason":    "ws_status.json not found \u2014 the WS runner is not "
                             "writing telemetry yet (start the stock_ws_runner "
                             "container or run `python main.py --ws-runner`).",
            })
        try:
            with path.open("r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError) as exc:
            return jsonify({
                "available": False,
                "reason":    f"failed to read ws_status.json: {exc}",
            })

        topic_f  = (request.args.get("topic")  or "").strip().lower()
        symbol_f = (request.args.get("symbol") or "").strip().upper()
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        events = snap.get("recent_events") or []
        if topic_f:
            events = [e for e in events if str(e.get("topic", "")).lower() == topic_f]
        if symbol_f:
            # Match against symbol alone OR the full "SYMBOL STRIKE OT" string
            # e.g. "NIFTY 23400 PE" or "23400" or "PE" all match option leg ticks.
            def _sym_match(e: dict) -> bool:
                sym    = str(e.get("symbol", "")).upper()
                strike = str(e.get("strike", "") or "").upper()
                ot     = str(e.get("option_type", "") or "").upper()
                full   = f"{sym} {strike} {ot}".strip()
                return (symbol_f in full) or (symbol_f == sym)
            events = [e for e in events if _sym_match(e)]
        # Most-recent first, capped.
        events = list(reversed(events))[:max(0, limit)]
        snap["recent_events"] = events
        snap["available"] = True

        # Derive a stale-state override: the WS runner can keep reporting
        # `connection_state=connected` even after the broker silently drops
        # the session (no ticks flowing, or zero subscribed tokens). The
        # raw value is preserved as `raw_connection_state` for diagnostics.
        try:
            from datetime import time as _time
            raw_state = snap.get("connection_state")
            if raw_state == "connected":
                ist_now = now_ist()
                in_market = (
                    ist_now.weekday() < 5
                    and _time(9, 15) <= ist_now.time() <= _time(15, 30)
                )
                threshold = 90.0 if in_market else 1800.0
                stale_reason: str | None = None

                # (a) zero subscribed tokens during market hours = dead feed
                subs = snap.get("subscribed_tokens")
                if in_market and subs is not None and int(subs) == 0:
                    stale_reason = "0 subscribed tokens during market hours"

                # (b) last_tick is too old
                if stale_reason is None:
                    last_tick = snap.get("last_tick_at")
                    if last_tick:
                        last_dt = datetime.fromisoformat(str(last_tick))
                        # Normalise: strip tzinfo if present (convert UTC→IST naive)
                        if last_dt.tzinfo is not None:
                            from zoneinfo import ZoneInfo as _Z
                            last_dt = last_dt.astimezone(_Z("Asia/Kolkata")).replace(tzinfo=None)
                        age_s = (ist_now - last_dt).total_seconds()
                        if age_s > threshold:
                            stale_reason = (
                                f"no ticks for {int(age_s)}s "
                                f"(threshold {int(threshold)}s)"
                            )
                    else:
                        # No ticks ever received — only flag once the
                        # runner has been up long enough that we'd
                        # expect ticks during market hours.
                        started = snap.get("started_at")
                        if in_market and started:
                            started_dt = _dt.fromisoformat(
                                str(started).replace("Z", "+00:00")
                            )
                            if started_dt.tzinfo is None:
                                started_dt = started_dt.replace(tzinfo=_tz.utc)
                            uptime_s = (_dt.now(_tz.utc) - started_dt).total_seconds()
                            if uptime_s > threshold:
                                stale_reason = (
                                    f"no ticks since runner start "
                                    f"({int(uptime_s)}s ago)"
                                )

                if stale_reason:
                    snap["raw_connection_state"] = raw_state
                    snap["connection_state"] = "stale"
                    snap["stale_reason"] = stale_reason
        except Exception:
            pass

        return jsonify(snap)

    # ---------- Health ----------
    @app.route("/health")
    def health():
        return jsonify({"status": "ok",
                        "service": "options_advisor_dashboard",
                        "port": DASHBOARD_CONFIG["port"]})

    # ---------- System status (read-only summary used by the UI banner) ----
    @app.route("/api/system-status")
    @_with_db
    def api_system_status(db: SQLServerConnection):
        """Lightweight read-only banner data for the dashboard.

        Returns the few runtime signals the UI surfaces as banners /
        chips so the page doesn't have to call /api/runtime-flags +
        scheduler endpoints separately. All values are best-effort.
        """
        from database.runtime_flags import (
            FLAG_CIRCUIT_BREAKER_ACTIVE,
            FLAG_KILL_SWITCH,
            FLAG_TRADE_EXECUTION_ENABLED,
            RuntimeFlagsRepo,
        )
        cb_active = False
        kill_switch = False
        trade_exec_enabled = True
        try:
            repo = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
            cb_active = repo.get_bool(FLAG_CIRCUIT_BREAKER_ACTIVE, default=False)
            kill_switch = repo.get_bool(FLAG_KILL_SWITCH, default=False)
            trade_exec_enabled = repo.get_bool(
                FLAG_TRADE_EXECUTION_ENABLED, default=True,
            )
        except Exception:
            logger.debug("system-status: runtime_flags read failed", exc_info=True)
        sch_running = False
        try:
            from scheduler.scheduler import get_scheduler
            sch = get_scheduler()
            sch_running = bool(sch and sch.running)
        except Exception:
            logger.debug("system-status: scheduler probe failed", exc_info=True)
        return jsonify({
            "circuit_breaker_active": cb_active,
            "kill_switch":             kill_switch,
            "trade_execution_enabled": trade_exec_enabled,
            "scheduler_running":       sch_running,
        })

    # ---------- Phase 3 — #9 Comprehensive system status JSON -----------
    # One-stop health snapshot for external monitors / on-call dashboards.
    # All probes are best-effort; on failure each section returns a stub
    # `{"available": False, "reason": ...}` so the endpoint never 500s.
    @app.route("/api/system/status")
    @_with_db
    def api_system_status_full(db: SQLServerConnection):
        from utils import now_ist as _now, today_ist as _today
        out: dict = {
            "as_of":   _now().isoformat(),
            "today":   _today().isoformat(),
        }

        # ---- runtime flags -------------------------------------------
        try:
            from database.runtime_flags import (
                FLAG_CIRCUIT_BREAKER_ACTIVE,
                FLAG_KILL_SWITCH,
                FLAG_TRADE_EXECUTION_ENABLED,
                RuntimeFlagsRepo,
            )
            r = RuntimeFlagsRepo(db, cache_ttl_seconds=0)
            out["runtime_flags"] = {
                "circuit_breaker_active": r.get_bool(FLAG_CIRCUIT_BREAKER_ACTIVE, default=False),
                "kill_switch":             r.get_bool(FLAG_KILL_SWITCH, default=False),
                "trade_execution_enabled": r.get_bool(FLAG_TRADE_EXECUTION_ENABLED, default=True),
            }
        except Exception as exc:  # pragma: no cover
            out["runtime_flags"] = {"available": False, "reason": str(exc)}

        # ---- scheduler ----------------------------------------------
        try:
            from scheduler.scheduler import get_scheduler
            sch = get_scheduler()
            out["scheduler"] = {
                "running":  bool(sch and sch.running),
                "job_count": len(sch.get_jobs()) if sch else 0,
            }
        except Exception as exc:
            out["scheduler"] = {"available": False, "reason": str(exc)}

        # ---- last-status per job -------------------------------------
        try:
            from database.log_repo import JobLogRepo
            jobs = JobLogRepo(db).latest_status_per_job()
            out["jobs_last_status"] = [
                {
                    "job_name":    j["job_name"],
                    "status":      j["status"],
                    "started_at":  j["started_at"].isoformat() if j.get("started_at") else None,
                    "finished_at": j["finished_at"].isoformat() if j.get("finished_at") else None,
                    "error":       (j.get("error_message") or "")[:200] or None,
                }
                for j in jobs
            ]
        except Exception as exc:
            out["jobs_last_status"] = {"available": False, "reason": str(exc)}

        # ---- data freshness -----------------------------------------
        today = _today()
        freshness: dict = {}
        def _age(d) -> dict:
            if d is None:
                return {"latest_date": None, "age_days": None}
            try:
                age = (today - d).days
            except Exception:
                age = None
            return {"latest_date": d.isoformat(), "age_days": age}
        try:
            from database.models import (
                FoEodRepo, SpotEodRepo, VixRepo, IvHistoryRepo,
            )
            freshness["fo_eod"]      = _age(FoEodRepo(db).latest_trade_date())
            freshness["iv_history"]  = _age(IvHistoryRepo(db).latest_trade_date())
            spot = SpotEodRepo(db).latest("NIFTY") or {}
            freshness["spot_nifty"]  = _age(spot.get("trade_date"))
            vix  = VixRepo(db).latest() or {}
            freshness["vix"]         = _age(vix.get("trade_date"))
        except Exception as exc:
            freshness = {"available": False, "reason": str(exc)}
        out["data_freshness"] = freshness

        # ---- counts -------------------------------------------------
        try:
            from database.models import SuggestionRepo, TradeRepo
            out["counts"] = {
                "open_trades":      len(TradeRepo(db).open_trades()),
                "pending_suggestions": len(SuggestionRepo(db).active_pending()),
            }
        except Exception as exc:
            out["counts"] = {"available": False, "reason": str(exc)}

        # ---- websocket ---------------------------------------------
        try:
            from providers.ws_monitor import default_snapshot_path
            path = default_snapshot_path()
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    snap = json.load(f)
                last_tick_at = snap.get("last_tick_at")
                age_sec: Optional[float] = None
                if last_tick_at:
                    try:
                        last_dt = datetime.fromisoformat(last_tick_at)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=_now().tzinfo)
                        age_sec = max(0.0, (_now() - last_dt).total_seconds())
                    except (TypeError, ValueError):
                        pass
                out["websocket"] = {
                    "available":         True,
                    "connection_state":  snap.get("connection_state"),
                    "last_tick_at":      last_tick_at,
                    "last_tick_age_sec": age_sec,
                    "subscribed_count":  snap.get("subscribed_count"),
                    "tick_count":        snap.get("tick_count"),
                }
            else:
                out["websocket"] = {
                    "available": False,
                    "reason":    "ws_status.json not found",
                }
        except Exception as exc:
            out["websocket"] = {"available": False, "reason": str(exc)}

        return jsonify(out)

    # ---------- Phase 3 — #3 Live MTM streaming via SSE -----------------
    # Polls data/live_mtm_state.json (written by the ws_runner container's
    # LiveRiskMonitor) and pushes changed trade MTM events over SSE so the
    # dashboard can show a live MTM ticker without polling.
    # Falls back to the in-process EventBus for single-container deployments.
    @app.route("/api/live/mtm/snapshot")
    def api_live_mtm_snapshot():
        """Return current per-trade MTM state written by ws_runner."""
        import json as _json
        import os as _os
        path = "data/live_mtm_state.json"
        if not _os.path.exists(path):
            return jsonify({"as_of": None, "trades": {}})
        try:
            with open(path, encoding="utf-8") as fh:
                return jsonify(_json.load(fh))
        except Exception:
            return jsonify({"as_of": None, "trades": {}})

    @app.route("/api/live/mtm")
    def api_live_mtm():
        import json as _json
        import os as _os
        import time as _time
        from flask import Response, stream_with_context

        MTM_STATE_PATH = "data/live_mtm_state.json"
        POLL_INTERVAL  = 1.0   # seconds between file reads

        @stream_with_context
        def _gen():
            last_seen: dict = {}   # trade_id → last mtm value sent
            initial_sent = False
            yield ": connected\n\n"
            heartbeat_at = _time.monotonic()
            while True:
                _time.sleep(POLL_INTERVAL)
                try:
                    if _os.path.exists(MTM_STATE_PATH):
                        with open(MTM_STATE_PATH, encoding="utf-8") as fh:
                            state = _json.load(fh)
                        for tid, payload in (state.get("trades") or {}).items():
                            cur_mtm = payload.get("mtm")
                            if not initial_sent or last_seen.get(tid) != cur_mtm:
                                last_seen[tid] = cur_mtm
                                yield f"data: {_json.dumps(payload)}\n\n"
                        initial_sent = True
                except Exception:
                    pass
                # Heartbeat every 15 s so proxies don't kill the connection.
                if _time.monotonic() - heartbeat_at >= 15:
                    heartbeat_at = _time.monotonic()
                    yield ": ping\n\n"
                    continue

        return Response(_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    return app


def run_dashboard():
    app = create_app()
    cfg = DASHBOARD_CONFIG
    logger.info("Starting dashboard on %s:%d", cfg["host"], cfg["port"])
    app.run(host=cfg["host"], port=cfg["port"], debug=cfg["debug"], threaded=True)
