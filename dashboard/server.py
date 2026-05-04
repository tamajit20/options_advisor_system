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
from typing import Any, Dict

from flask import Flask, jsonify, render_template, request

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
from database.connection import SQLServerConnection
from database.log_repo import JobLogRepo, LogRepo
from database.models import (
    ConfigRepo,
    FoEodRepo,
    NotificationRepo,
    SimulationRepo,
    SuggestionRepo,
    TradeRepo,
)
from lifecycle.resuggestion_engine import generate_resuggestion
from lifecycle.trade_executor import close_trade_with_fills, mark_executed, supplement_trade
from utils import today_ist

logger = logging.getLogger(__name__)


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
    "simulation_update":  {"icon": "🎯", "name": "Simulation Update",
                            "description": "Updates daily P/L simulation for past suggestions."},
    "exit_engine":        {"icon": "🚪", "name": "Exit Engine",
                            "description": "Evaluates open trades and emits exit instructions."},
    "weekly_cleanup":     {"icon": "🧹", "name": "Weekly Cleanup",
                            "description": "Applies retention policy and trims historical data."},
}

_DOW_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
               "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def _summarize_cron(cfg: Dict[str, Any]) -> str:
    """Render a SCHEDULER_CONFIG entry as a human-readable schedule string."""
    if not cfg:
        return ""
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

    # ---------- HTML ----------
    @app.route("/")
    def index():
        import time
        return render_template("dashboard.html",
                               theme=DASHBOARD_CONFIG["theme"],
                               port=DASHBOARD_CONFIG["port"],
                               cache_bust=int(time.time()))

    # ---------- Theme tokens ----------
    @app.route("/api/theme")
    def api_theme():
        return jsonify(DASHBOARD_CONFIG["theme"])

    # ---------- Tab 1: Suggestion ----------
    @app.route("/api/suggestion/today")
    @_with_db
    def api_suggestion_today(db: SQLServerConnection):
        sug = SuggestionRepo(db)
        # Return suggestions whose execution window hasn't closed yet:
        #   - entry_date > today: execute in the future (e.g. Friday→Monday)
        #   - entry_date = today AND time ≤ 15:30 IST: still actionable today
        # After 15:30 on the entry day the suggestion disappears automatically.
        # Falls back to legacy PENDING rows that pre-date the entry_date column.
        rows = sug.active_pending()
        out = []
        for r in rows:
            r_out = _row(r)
            if "net_credit_suggested" in r_out:
                r_out["net_credit"] = r_out.pop("net_credit_suggested")
            r_out["legs"] = [_row(l) for l in sug.legs(r["suggestion_id"])]
            out.append(r_out)
        return jsonify({"suggestions": out})

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
        rows = trd.open_trades()
        out = []
        for r in rows:
            r_out = _row(r)
            r_out["legs"] = [_row(l) for l in trd.legs_with_suggestion_info(r["trade_id"])]
            # Attach the original suggestion so the UI can show its rationale
            if r.get("suggestion_id"):
                sug_row = sug.get(r["suggestion_id"])
                if sug_row:
                    sug_out = _row(sug_row)
                    if "net_credit_suggested" in sug_out:
                        sug_out["net_credit"] = sug_out.pop("net_credit_suggested")
                    sug_out["legs"] = [_row(l) for l in sug.legs(r["suggestion_id"])]
                    r_out["suggestion"] = sug_out
                else:
                    r_out["suggestion"] = None
            else:
                r_out["suggestion"] = None
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
        fo = FoEodRepo(db)
        chain = fo.get_chain(underlying, today_ist(), expiry)
        chain_mid = {
            (float(c["strike"]), c["option_type"]):
                float(c.get("settle_price") or c.get("close_price") or 0.0)
            for c in chain
        }
        out = []
        est = 0.0
        for l in executed:
            mid = chain_mid.get((float(l["strike"]), l["option_type"]), 0.0)
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
            from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")

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

        where = " AND ".join(filters)
        rows = db.fetch_all(
            f"SELECT TOP 300 * FROM options_suggestions "
            f"WHERE {where} ORDER BY generated_on DESC",
            params,
        )
        # Collect unique underlyings for the dropdown
        underlyings = sorted({r["underlying"] for r in rows if r.get("underlying")})
        return jsonify({"suggestions": [_row(r) for r in rows], "underlyings": underlyings})

    @app.route("/api/history/trades")
    @_with_db
    def api_history_trades(db: SQLServerConnection):
        days = int(request.args.get("days", 30))
        since = (datetime.utcnow() - timedelta(days=days))
        rows = db.fetch_all(
            "SELECT TOP 200 * FROM options_trades "
            "WHERE executed_on >= ? AND status IN ('CLOSED', 'EXPIRED') "
            "ORDER BY executed_on DESC",
            [since],
        )
        return jsonify({"trades": [_row(r) for r in rows]})

    @app.route("/api/history/paired")
    @_with_db
    def api_history_paired(db: SQLServerConnection):
        days = int(request.args.get("days", 30))
        since = datetime.utcnow() - timedelta(days=days)
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
        from_date_str = request.args.get("from_date", "").strip()
        to_date_str   = request.args.get("to_date", "").strip()
        if from_date_str and to_date_str:
            try:
                since = datetime.strptime(from_date_str, "%Y-%m-%d")
                until = datetime.strptime(to_date_str,   "%Y-%m-%d") + timedelta(days=1)
            except ValueError:
                since = datetime.utcnow() - timedelta(days=30)
                until = datetime.utcnow() + timedelta(days=1)
        else:
            days  = int(request.args.get("days", 30))
            since = datetime.utcnow() - timedelta(days=days)
            until = datetime.utcnow() + timedelta(days=1)
        underlying = request.args.get("underlying", "").strip()

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
            "  s.expiry_date AS sug_expiry, s.dte AS sug_dte "
            "FROM options_trades t "
            "LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id "
            "WHERE t.status IN ('CLOSED', 'EXPIRED') "
            "  AND t.executed_on >= ? AND t.executed_on < ? "
        )
        params = [since, until]
        if underlying:
            sql += " AND s.underlying = ? "
            params.append(underlying)
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
                    "legs":        sug_legs,
                } if r.get("underlying") else None,
            })

        # Distinct underlyings for the filter dropdown
        und_rows = db.fetch_all(
            "SELECT DISTINCT s.underlying FROM options_trades t "
            "LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id "
            "WHERE t.status IN ('CLOSED','EXPIRED') AND s.underlying IS NOT NULL "
            "ORDER BY s.underlying",
            [],
        )
        underlyings = [u["underlying"] for u in und_rows if u.get("underlying")]
        return jsonify({"trades": out, "underlyings": underlyings})

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
        since = (datetime.utcnow() - timedelta(hours=int(since_h))) if since_h else None
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
        unread = request.args.get("unread") == "1"
        repo = NotificationRepo(db)
        rows = repo.unread() if unread else repo.recent()
        return jsonify({"notifications": [_row(r) for r in rows]})

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

            # Next scheduled run from APScheduler
            next_run = None
            if sch_running:
                aps_job = sch.get_job(name)
                if aps_job and aps_job.next_run_time:
                    next_run = aps_job.next_run_time

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
            "generated_at": _ist_iso(datetime.now()),
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

    return app


def run_dashboard():
    app = create_app()
    cfg = DASHBOARD_CONFIG
    logger.info("Starting dashboard on %s:%d", cfg["host"], cfg["port"])
    app.run(host=cfg["host"], port=cfg["port"], debug=cfg["debug"], threaded=True)
