"""
options_advisor_system / config.py
==================================

SINGLE SOURCE OF TRUTH for all configurable values.

Rules (enforced by code review):
    1. NO hardcoded values, URLs, thresholds, time windows, or charge rates may
       appear anywhere else in the codebase. Every such value lives here.
    2. Runtime overrides come from the DB table `options_config` (UI-editable).
       Use `database.config_repo.get(key, default=...)` to read overrides.
    3. The config dictionaries below define DEFAULTS. Sensitive infrastructure
       values (DB credentials, SMTP password) are loaded from environment
       variables (typically populated by `.env.docker`).

Boundary: this module imports ONLY from the standard library.
"""

from __future__ import annotations

import os
from datetime import time as _time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_CONFIG = {
    "server":             _env("OPT_DB_SERVER", "TAMAJITLAPTOP\\SQLEXPRESS"),
    "database":           _env("OPT_DB_NAME",   "OptionsAdvisorDB"),
    "username":           _env("OPT_DB_USER",   ""),     # blank → Windows auth
    "password":           _env("OPT_DB_PASSWORD", ""),
    "connection_timeout": _env_int("OPT_DB_TIMEOUT", 30),
    # SQL Server SET LOCK_TIMEOUT (ms). Any statement that would wait
    # for a lock longer than this fails fast with error 1222 instead of
    # blocking forever -- protects us from a single hung writer holding
    # X-locks indefinitely. -1 = wait forever (SQL Server default).
    "lock_timeout_ms":    _env_int("OPT_DB_LOCK_TIMEOUT_MS", 30_000),
    # Per-statement query timeout (s). Belt-and-braces alongside
    # lock_timeout: catches genuinely long-running queries too.
    "query_timeout":      _env_int("OPT_DB_QUERY_TIMEOUT", 60),
    # If True, `python main.py --init-db` issues `CREATE DATABASE` if missing.
    "create_if_missing":  _env_bool("OPT_DB_CREATE_IF_MISSING", True),
}


# ---------------------------------------------------------------------------
# Scheduler (all times IST)
# ---------------------------------------------------------------------------
SCHEDULER_CONFIG = {
    "timezone": "Asia/Kolkata",
    "jobs": {
        # Data downloads (post-market)
        "fo_bhav_download":   {"hour": 18, "minute": 30, "enabled": True},
        "spot_bhav_download": {"hour": 18, "minute": 35, "enabled": True},
        "vix_download":       {"hour": 18, "minute": 40, "enabled": True},
        "fii_download":       {"hour": 18, "minute": 45, "enabled": True},
        # Calculations
        "iv_calculation":     {"hour": 19, "minute":  0, "enabled": True},
        # Suggestions + lifecycle
        "suggestion_engine":  {"hour": 19, "minute": 30, "enabled": True},
        # Live suggestion: re-evaluate with live Zerodha chain during market hours.
        # Requires OPT_PROVIDERS=zerodha.  Runs at 11:00 IST — gives the WS
        # 5-min aggregator ~20 samples (90 min from 09:30 open) for slope
        # estimation while still leaving plenty of session for execution.
        # Retires the stale EOD suggestion and replaces it with a fresh
        # suggestion based on current spot, chain, IV, and trajectory metrics.
        # No-op (logged + skipped) when Zerodha is unavailable.
        "live_suggestion_engine": {
            "day_of_week": "mon-fri", "hour": 11, "minute": 0, "enabled": True,
        },
        # Phase 3 — #1 / #13. Additional live-suggest windows so users get
        # multiple refreshed suggestions per session instead of one fixed
        # 11:00 run. Each entry registers as its own scheduled job.
        # Set "enabled": False on any window to skip.
        "live_suggestion_engine_0945": {
            "day_of_week": "mon-fri", "hour": 9, "minute": 45, "enabled": True,
        },
        "live_suggestion_engine_1300": {
            "day_of_week": "mon-fri", "hour": 13, "minute": 0, "enabled": True,
        },
        "live_suggestion_engine_1430": {
            "day_of_week": "mon-fri", "hour": 14, "minute": 30, "enabled": True,
        },
        # Phase 3 — #5. Event-eve review: at 14:30 IST, if there is a HIGH-impact
        # event scheduled for tomorrow (or today afternoon), post one
        # EVENT_AHEAD_REVIEW notification per ACTIVE trade so the user
        # decides whether to close before the event.
        "event_eve_review": {
            "day_of_week": "mon-fri", "hour": 14, "minute": 30, "enabled": True,
        },
        "simulation_update":  {"hour": 19, "minute": 45, "enabled": True},
        "exit_engine":        {"hour": 19, "minute": 50, "enabled": True},
        # Phase 2b.1 — live-vs-settled drift detection
        # 15:35 IST: capture live LTP for every leg of every ACTIVE trade.
        # 19:35 IST: compare to today's settled close (loaded by fo_bhav at
        #            18:30) and fire a DRIFT_WARNING for legs that diverge
        #            beyond STRATEGY_CONFIG["intraday_close_drift_pct"].
        "intraday_close_snapshot": {
            "day_of_week": "mon-fri", "hour": 15, "minute": 35, "enabled": True,
        },
        "drift_verifier": {
            "day_of_week": "mon-fri", "hour": 19, "minute": 35, "enabled": True,
        },
        # 09:35 IST: re-validate today's PENDING suggestions against live
        # opening chain. Avoids 09:30 by 5 min so the worst of opening-tick
        # noise has settled.
        "intraday_validator": {
            "day_of_week": "mon-fri", "hour": 9, "minute": 35, "enabled": True,
        },
        # Maintenance (Sunday 02:00)
        "weekly_cleanup":     {"day_of_week": "sun", "hour": 2,  "minute":  0, "enabled": True},
        # Events calendar sync — Monday 07:00 before market open
        "events_seed":        {"day_of_week": "mon", "hour": 7,  "minute":  0, "enabled": True},
    },
    # Each job also gets a max wallclock budget (seconds) — enforced by
    # `_run_job` via a watchdog thread that closes the DB connection on
    # expiry (severs SQL Server locks) and marks the row FAILED.
    "job_timeout_seconds": {
        "fo_bhav_download":   600,
        "spot_bhav_download": 300,
        "vix_download":       180,
        "fii_download":       300,
        "iv_calculation":     900,
        "suggestion_engine":  600,
        # Live-suggestion windows: short timeout because they fan out
        # to several HTTP fetches; if any one stalls we want the row
        # to FAIL fast so the next window isn't blocked.
        "live_suggestion_engine":      300,
        "live_suggestion_engine_0945": 300,
        "live_suggestion_engine_1300": 300,
        "live_suggestion_engine_1430": 300,
        "simulation_update":  600,
        "exit_engine":        300,
        "events_seed":        300,
        "event_eve_review":   180,
        "weekly_cleanup":     1800,
        "intraday_close_snapshot": 300,
        "drift_verifier":          120,
        "intraday_validator":      180,
    },
    # Default for jobs not listed above.
    "default_job_timeout_seconds": 600,
}


# ---------------------------------------------------------------------------
# NSE Data sources
# ---------------------------------------------------------------------------
NSE_CONFIG = {
    # F&O bhav copy (zip containing CSV) — yyyymmdd
    "fo_bhav_url":    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip",
    # Cash market bhav copy (zip containing CSV) — yyyymmdd
    "spot_bhav_url":  "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip",
    # VIX history (full archive CSV)
    "vix_archive_url": "https://www.niftyindices.com/IndexConstituent/IndiaVIX_Historical_Data.csv",
    # NSE participant-wise OI (yyyymmdd)
    "fii_oi_url":      "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv",
    # Live option chain JSON (intraday) — Phase 3 #8 failsafe provider
    "option_chain_url": "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
    # Warm-up endpoint to obtain cookies before downloading archives
    "warmup_url":      "https://www.nseindia.com",
    "request_timeout": 30,
    "max_retries":     3,
    "retry_backoff_seconds": 5,
    "headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com",
    },
}


# ---------------------------------------------------------------------------
# Strategy / Confidence engine
# ---------------------------------------------------------------------------
STRATEGY_CONFIG = {
    # Risk-free rate for Black-Scholes IV
    "risk_free_rate": 0.065,

    # IV Rank gates
    "iv_rank_writing_min":   50.0,   # >50% → consider writing path
    "iv_rank_buying_max":    30.0,   # <30% → consider buying path
    "iv_rank_butterfly_min": 70.0,   # very-high IV → prefer iron butterfly over condor
    # Butterfly also requires IV/HV-20 premium ≥ this threshold — i.e. options must be
    # materially more expensive than realised vol justifies. When IV rank is high but
    # iv_premium is moderate (1.1–1.3) the elevated IV is a genuine fear premium and
    # the EM is wide → ATM short legs would be too tight; fall back to IRON_CONDOR.
    "iv_butterfly_min_premium": 1.40,
    "iv_rank_naked_long_max": 20.0,  # very-low IV → naked long preferred over debit spread
    # 30–50 mid-zone → debit spreads (BULL_CALL_SPREAD / BEAR_PUT_SPREAD), no sideways play

    # PCR conviction bands (for picking strong vs mild directional plays)
    "pcr_strong_bullish_below": 0.55,   # heavy call buying = strong bullish
    "pcr_strong_bearish_above": 1.55,   # heavy put buying = strong bearish

    # PCR neutral band
    "pcr_neutral_low":  0.7,
    "pcr_neutral_high": 1.3,

    # DTE band
    "dte_min": 7,
    "dte_max": 21,

    # Confidence — tiered gating
    # Hard gate (DTE): any FAIL = no suggestion regardless of score
    # Soft gates (IV Rank, VIX, PCR, OI Walls, Trend, IV premium, FII): SOFT_FAIL
    #   if condition not met; trade proceeds if at least soft_gate_min_pass of 7 pass.
    # Event gate: SOFT_FAIL warning only — not counted in the soft-gate tally.
    "confidence_min_pass_count": 7,     # legacy — no longer used by engine
    "soft_gate_min_pass": 5,            # need ≥5 of 7 soft gates to pass

    # Phase 3: per-strategy soft-gate minimum (Naked longs are the riskiest —
    # require all 7 soft gates. Debit / uncapped — 6/7. Spreads default to 5/7.)
    "strategy_min_soft_pass": {
        "LONG_CALL":      7,
        "LONG_PUT":       7,
        "LONG_STRADDLE":  6,
        "LONG_STRANGLE":  6,
        "JADE_LIZARD":    6,
    },

    # IV premium vs realised volatility (HV-20) thresholds — REGIME-WIDE GATE
    # These are the *default* bands used by engine/confidence.py for ALL strategies.
    # The regime-wide gate is intentionally permissive (SOFT_FAIL only above
    # iv_premium_buy_max) so that no single strategy class is silently vetoed
    # by a shared knob. Strategy-specific stricter limits live below in
    # `strategy_iv_premium_buy_max` (analogous to `iv_butterfly_min_prem`).
    #
    # WRITING (selling premium):
    #   PASS    if IV/HV ≥ iv_premium_sell_min            (premium adequate)
    # BUYING (paying premium) — TIERED display, single SOFT_FAIL boundary:
    #   PASS       if IV/HV ≤ iv_premium_buy_pass         (real edge: IV ≤ realised vol)
    #   PASS_WARN  if IV/HV ≤ iv_premium_buy_warn         (neutral / no edge)
    #   PASS_WARN  if IV/HV ≤ iv_premium_buy_max          (warn but does not block)
    #   SOFT_FAIL  otherwise                              (overpaying badly)
    "iv_premium_sell_min": 0.90,
    "iv_premium_buy_pass": 1.00,        # ≤1.00 → real buying edge
    "iv_premium_buy_warn": 1.20,        # 1.00–1.20 → neutral; cosmetic boundary only
    "iv_premium_buy_max":  1.50,        # >1.50 → SOFT_FAIL (regime-wide ceiling)

    # ---------------------------------------------------------------
    # Per-strategy IV/HV ceiling for BUYING regime (pattern mirrors
    # `iv_butterfly_min_prem` for IRON_BUTTERFLY).
    #
    # When a strategy appears in this map, strategy_selector enforces a
    # *stricter* veto than the regime-wide `iv_premium_buy_max`. Strategies
    # NOT listed here are unaffected and continue to use only the regime gate.
    #
    # Naked long premium structures suffer most from overpaying for IV (no
    # offsetting short leg to recover the IV decay), so we cap them tighter.
    # Spreads/condors/credit strategies are not listed — unchanged behaviour.
    # ---------------------------------------------------------------
    "strategy_iv_premium_buy_max": {
        "LONG_STRADDLE":     1.20,   # both legs are long premium — strictest
        "LONG_STRANGLE":     1.20,   # same risk profile as straddle
        "LONG_CALL":         1.20,   # naked long single leg
        "LONG_PUT":          1.20,   # naked long single leg
        # BULL_CALL_SPREAD / BEAR_PUT_SPREAD (debit verticals): NOT listed.
        #   Short leg partially funds the long, so IV-overpay impact is muted.
        # IRON_CONDOR / IRON_BUTTERFLY / BPS / BCS / JL: NOT listed (credit).
    },

    # Long-premium profit-target multiplier (multiplied by debit paid).
    #
    #   target_debit_multiple = base + dte/dte_scale, capped at max
    #
    # Example with defaults (base=0.50, dte_scale=14, max=1.50):
    #     DTE  =  3 → 0.50 + 3/14  ≈ 0.71×  → 71% of debit
    #     DTE  =  7 → 0.50 + 7/14  ≈ 1.00×  → 100% of debit  (was hard-coded 2×)
    #     DTE  = 14 → 0.50 + 14/14 = 1.50×  → 150% of debit
    #     DTE  = 30 → capped       = 1.50×
    #
    # Rationale: a long straddle held to expiry needs spot to move past BE; with
    # only 7 DTE remaining, theta decay makes 2× a low-probability target that
    # encourages users to hold past optimal exit. Scaling with DTE (and capping
    # at 1.5×) sets a realistic anchor that matches expected behaviour.
    "long_premium_target_base": 0.50,
    "long_premium_target_dte_scale": 14.0,
    "long_premium_target_max": 1.50,

    # FII net futures positioning (long − short contracts) threshold
    # FII position beyond this magnitude against the trend triggers a soft-fail.
    "fii_net_futures_threshold": 50_000,

    # ── Trajectory thresholds (5-min WS aggregator → confidence gates) ──
    # Window of recent 5-min samples loaded for slope/persistence.
    # 12 samples = 60 minutes of trajectory.
    "trajectory_window_samples":      12,
    # Min samples required before trajectory metrics are emitted at all.
    "trajectory_min_samples":         3,
    # _iv_trajectory_gate: SOFT_FAIL credit strategies when ATM IV is rising
    # sustainedly (slope_pct > min and persistence > min).
    "iv_traj_slope_warn_pct":         0.5,   # % per 5-min sample
    "iv_traj_persistence_warn":       0.7,   # 70% of deltas same sign
    # _oi_momentum_gate: SOFT_FAIL Iron Condor / Iron Butterfly when OI PCR
    # shows sustained directional drift (regime not actually sideways).
    "oi_pcr_traj_slope_warn_pct":     1.0,   # % per 5-min sample
    "oi_pcr_traj_persistence_warn":   0.7,
    # _spread_quality_gate: hard FAIL when ATM bid-ask spread sum exceeds budget.
    # bps of mid; index ATM spreads typically run 5-30 bps; >60 bps = illiquid.
    "spread_quality_max_total_bps":   60.0,
    # IV slope magnitude that biases select_strategy toward writing (negative
    # = falling IV) or buying (positive = rising IV). Smaller than the gate
    # threshold above so bias kicks in before veto.
    "iv_traj_bias_slope_pct":         0.3,

    # IV calc bisection params
    "iv_bisection_low":  0.001,
    "iv_bisection_high": 5.0,
    "iv_bisection_tol":  1e-4,
    "iv_bisection_max_iter": 100,

    # Underlyings to consider (in priority order)
    "underlyings": ["NIFTY", "BANKNIFTY", "FINNIFTY"],

    # Lot sizes — overridden by data/lot_sizes.csv if present
    "default_lot_sizes": {
        "NIFTY":      75,
        "BANKNIFTY":  35,
        "FINNIFTY":   65,
    },

    # Net credit must be at least this fraction of spread width to be viable.
    # Used as default fallback when a strategy is not in the per-strategy override.
    "min_credit_to_width_ratio": 0.20,   # 20% — e.g. ₹40 credit on ₹200-wide condor

    # Per-strategy credit-to-width minimum — strategies absent from the map fall
    # back to `min_credit_to_width_ratio`. Tightening one strategy here cannot
    # affect any other strategy (strict isolation).
    "strategy_min_credit_to_width_ratio": {
        "IRON_CONDOR":      0.20,
        "IRON_BUTTERFLY":   0.30,   # ATM butterfly demands richer credit
        "BULL_PUT_SPREAD":  0.22,
        "BEAR_CALL_SPREAD": 0.22,
        "JADE_LIZARD":      0.25,
    },

    # Credit-to-width grading tiers (drive edge_score; do not gate).
    # "weak" range is [hard_min, good); "good" is [good, strong); "strong" is ≥strong.
    # Values below `min_credit_to_width_ratio` (or per-strategy override) are still
    # rejected outright in strategy_selector — these tiers refine the GOOD trades.
    "credit_to_width_grade_thresholds": {
        "good":   0.25,    # 25–30% → solid premium
        "strong": 0.30,    # ≥30%   → premium-rich, top tier
    },

    # ---------------------------------------------------------------
    # Per-strategy "real edge" IV/HV threshold for BUYING regime.
    # Mirrors `strategy_iv_premium_buy_max` but represents the *upper bound*
    # for "real buying edge" (vs the regime-wide PASS threshold of 1.00).
    # Used by edge_score AND as a soft veto in strategy_selector when
    # iv_premium > buy_pass × (1 + iv_premium_buy_pass_tolerance) in the
    # buying regime. Strategies absent from the map skip the veto.
    # ---------------------------------------------------------------
    "strategy_iv_premium_buy_pass": {
        "LONG_STRADDLE":     0.85,   # IV ≤ 85% of HV → real edge for long premium
        "LONG_STRANGLE":     0.85,
        "LONG_CALL":         0.90,   # naked long single leg — slightly more lenient
        "LONG_PUT":          0.90,
        "BULL_CALL_SPREAD":  0.95,   # debit vertical — short leg offsets some IV
        "BEAR_PUT_SPREAD":   0.95,
    },
    # Tolerance buffer above the per-strategy buy_pass threshold before vetoing.
    # LONG_STRADDLE buy_pass=0.85 × (1 + 0.15) = 0.978× ceiling — keeps
    # strategies near the edge alive while culling clearly marginal IV regimes.
    # Strategy isolation: tightening this affects all per-strategy buy_pass
    # users uniformly; per-strategy thresholds remain independent.
    "iv_premium_buy_pass_tolerance": 0.15,

    # ---------------------------------------------------------------
    # Expected-move calibration (review item #10).  After every expiry
    # settles, `lifecycle/em_calibration_recorder` logs realised/expected
    # for each suggestion to `options_em_calibration`.  At suggestion
    # time we look up the median realised/expected for the same
    # (underlying, dte_band) cohort and surface a warning chip when it
    # deviates from 1.0 by more than the threshold below.  The cohort
    # must contain at least `em_calibration_min_samples` rows or no
    # warning fires (small samples are statistically meaningless).
    # ---------------------------------------------------------------
    "em_calibration_min_samples":         4,
    "em_calibration_deviation_threshold": 0.25,
    "em_calibration_lookback_limit":      12,   # most recent N expiries per cohort

    # ---------------------------------------------------------------
    # Edge score — numeric quality score 0–100 (display + ranking only;
    # never blocks a suggestion). Component weights MUST sum to ≤100.
    # ---------------------------------------------------------------
    "edge_score_weights": {
        "pop":             40.0,   # PoP (probability of profit, 0–100)
        "credit_or_debit": 25.0,   # credit-to-width grade (credit) OR debit-discount (debit)
        "iv_alignment":    20.0,   # IV regime alignment (writing: IV/HV high; buying: IV/HV low)
        "confidence":      15.0,   # soft-pass count above the strategy's required minimum
    },

    # Take-profit threshold for exit engine (fraction of max profit)
    "take_profit_fraction": 0.80,   # default fallback when strategy not in override map

    # Strategy-aware take-profit overrides (Phase 2)
    # IC / BPS / BCS — close at 50% credit captured (tastytrade convention; gamma risk dominates after).
    # IRON_BUTTERFLY  — ATM short tends to give back profits fast; book at 75%.
    # Debit / naked — keep default (0.80) so directional plays run.
    "strategy_take_profit_fraction": {
        "IRON_CONDOR":      0.50,
        "BULL_PUT_SPREAD":  0.50,
        "BEAR_CALL_SPREAD": 0.50,
        "IRON_BUTTERFLY":   0.75,
        "JADE_LIZARD":      0.50,
    },

    # Time-decay exit (Phase 2)
    # When DTE drops to or below this threshold, credit spreads have already extracted
    # most theta and face exploding gamma risk. Exit alert fires regardless of P&L.
    "time_decay_exit_dte": 3,
    "time_decay_exit_strategies": [
        "IRON_CONDOR", "IRON_BUTTERFLY",
        "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD",
        "JADE_LIZARD",
    ],

    # Stop-loss threshold for exit engine (fraction of max loss)
    # Exit when current loss reaches this fraction of the defined max loss.
    "stop_loss_fraction": 0.50,   # 50% of max loss — standard for defined-risk options

    # Phase 2b-iii — intraday WS-driven SL alert
    # Per-leg "short premium doubled" rule used by lifecycle/intraday_monitor.
    # When a SHORT leg's live LTP rises above fill_price * intraday_sl_multiplier
    # we fire a CRITICAL SL_TRIGGER notification (once per leg per day).
    "intraday_sl_multiplier": 2.0,

    # Live trade-level risk monitor (lifecycle/live_risk_monitor.py)
    # On every WS tick that updates a leg of an ACTIVE trade we recompute the
    # whole-trade MTM via engine.exit_engine.evaluate_exit() and emit:
    #   * SL_TRIGGER  — when current_pnl <= -(stop_loss_fraction * max_loss)
    #   * TARGET_HIT  — when current_pnl >= live_target_fraction * max_profit
    # Stricter than EOD take-profit (0.5) because intraday wiggle can briefly
    # cross 0.5 and reverse; 0.7 leaves room before alerting the user.
    # Re-fires every cooldown_minutes while the trade remains in breach so
    # the user keeps getting reminded; alerts stop the moment the trade
    # leaves ACTIVE status (user closed it).
    "live_risk_monitor": {
        "enabled":             True,
        "live_target_fraction": 0.70,
        "cooldown_minutes":    15,
        "reload_interval_sec": 60,
        # Only run during NSE cash session (09:15 – 15:30 IST).
        # All times are interpreted as Asia/Kolkata local time (utils.now_ist()).
        "session_start": "09:15",
        "session_end":   "15:30",
        # Stale-data guard: any leg whose last tick is older than this is
        # treated as "no fresh price"; trade evaluation is skipped until it
        # ticks again. Prevents false alerts on illiquid legs.
        "stale_leg_seconds": 30,
        # Pre-breach soft warning: emit PRE_BREACH_WARNING once when current
        # loss first crosses this fraction of max loss (default 30%).
        # Gives the user lead time before the hard SL_TRIGGER (50%).
        "pre_breach_fraction": 0.30,
        # DTE-aware target tightening: at low DTE we tighten the target so
        # we don't sit through gamma; at high DTE we let theta run.
        # Linear interpolation between the two endpoints (clamped).
        "target_fraction_at_min_dte": 0.50,   # at DTE <= target_min_dte
        "target_fraction_at_max_dte": 0.80,   # at DTE >= target_max_dte
        "target_min_dte": 3,
        "target_max_dte": 15,
        # Spot-based SL: when the underlying spot crosses
        # `actual_stop_loss_level` for an ACTIVE trade, fire SL_TRIGGER.
        # Independent of premium-based SL (loss >= stop_loss_fraction × max_loss).
        "spot_sl_enabled": True,
        # Optional dashboard URL prefix for action links in alerts.
        # When set, notification body will include
        # `<dashboard_url>/#/trade/<trade_id>`. Leave None / empty to disable.
        "dashboard_url": None,
        # Status JSON snapshot path (counters for ticks/alerts). Written every
        # `status_write_interval_sec`. Set to None to disable.
        "status_path": "data/live_risk_status.json",
        "status_write_interval_sec": 30,
        # Trailing SL on profit (Phase 3 — #4).
        # Each step is (profit_fraction_trigger, lock_floor_fraction_of_max_profit).
        # When current_pnl crosses trigger, the trade's PnL floor is set to
        # lock × max_profit; if PnL ever drops below the floor, fire SL_TRIGGER.
        # Steps must be sorted by ascending trigger.
        # Example: at 50% of target, lock breakeven (0.0); at 80% lock 40%.
        "trailing_sl_steps": [
            [0.50, 0.0],
            [0.80, 0.40],
        ],
        # Live MTM streaming (Phase 3 — #3).
        # Throttle TOPIC_TRADE_MTM publishes to this many seconds per trade
        # so the SSE stream stays cheap on fast-ticking trades.
        "mtm_publish_interval_sec": 1.0,
        # Event-eve tightening (Phase 3 — #5).
        # When events_repo reports a HIGH-impact event for tomorrow, the live
        # monitor uses this tighter pre-breach fraction (default 0.20)
        # instead of `pre_breach_fraction` (default 0.30) so the user gets
        # earlier warnings on event-eve.
        "event_eve_pre_breach_fraction": 0.20,
    },

    # Suggestion freshness (Phase 3 — #2).
    # A PENDING suggestion older than this many minutes is considered STALE.
    # The execution validator (and dashboard badge) gates executions of stale
    # rows unless the user explicitly bypasses with `force=True`.
    # 30 min covers normal click-latency without letting a 09:30 suggestion
    # be acted on at 14:55 with completely different premiums.
    "suggestion_freshness_minutes": 30,

    # Phase 3 — #7: dead-man WebSocket watchdog. During market hours, if
    # the WS runner has not seen any tick within `stale_threshold_sec`,
    # fire ONE WS_DEAD_MAN notification (re-arms only after recovery).
    # Cheap loop runs at `check_interval_sec` cadence.
    "ws_watchdog": {
        "enabled":             True,
        "stale_threshold_sec": 60,
        "check_interval_sec":  30,
        "session_start":       "09:15",
        "session_end":         "15:30",
    },

    # Phase 2b.1 — drift verifier threshold (%)
    # The 19:35 drift verifier compares each 15:35 live LTP capture to the
    # corresponding settled close from the EOD bhav. Any leg whose abs
    # drift exceeds this percentage fires a single rolled-up DRIFT_WARNING
    # notification. 5% is calibrated to be loud only on real feed problems.
    "intraday_close_drift_pct": 5.0,

    # 09:35 IST opening-bell validator (lifecycle/intraday_validator.py)
    # Re-prices each PENDING suggestion against the live opening chain;
    # net credit moving more than this percentage off the originally
    # suggested credit flips the suggestion to STALE_0935 + status='IGNORED'.
    # 15% allows for normal post-open volatility settling without
    # being so loose that a real regime shift slips through.
    "intraday_validator_tolerance_pct": 15.0,

    # Centralized pre-execution gate (engine/execution_validator.py)
    # Run by lifecycle/trade_executor.mark_executed before flipping a
    # suggestion to a real trade. Setting `execution_validator_enabled`
    # to False is an emergency override — ALL checks below are skipped.
    "execution_validator_enabled": True,
    # Hard ceiling on the age of the underlying data backing the
    # suggestion. 240m = 4h covers the EOD->next-day window comfortably
    # while blocking yesterday's stale rollover suggestions if they leak.
    "execution_validator_max_data_age_minutes": 240.0,
    # Minimum distance from spot for any SELL leg, expressed as a % of spot.
    # 1.5% on NIFTY 23,000 ≈ 350 pts — enough to avoid a structurally bad
    # short strike a single tick away from being ITM, without rejecting
    # the typical ATM-edge short on a credit spread.
    "min_short_strike_buffer_pct": 1.5,

    # Adverse-move auto-advisor (engine/adverse_move_advisor.py).
    # Fires an ADVERSE_MOVE_WARNING notification once a trade's MTM
    # crosses this percentage of max_loss while still under the hard SL
    # threshold (stop_loss_fraction). 30% is roughly the midpoint of the
    # 0..SL band and gives the user time to plan a roll/partial-close.
    "adverse_move_warning_pct": 30.0,

    # Daily P&L circuit breaker (engine/circuit_breaker.py).
    # Aggregate open-trade MTM is checked at the end of every EOD
    # exit-engine run. If total_pnl < -capital * pct/100, we set the
    # `circuit_breaker_active` runtime flag and fire a CRITICAL
    # notification. While the flag is on, execution_validator vetoes
    # all new executions.
    # Capital should reflect the operator's actual options trading float.
    "daily_pnl_circuit_breaker_capital_rs": 500_000.0,
    "daily_pnl_circuit_breaker_pct":         3.0,

    # Opportunity-regen-on-tick (lifecycle/opportunity_regen_watcher.py)
    # When the live spot / VIX moves more than these thresholds vs the
    # day's first observed tick, fire a single OPPORTUNITY_REGEN_HINT
    # per (trigger, symbol, day) so the user knows that morning's
    # suggestions may need a refresh. Tight thresholds are noisy; loose
    # ones miss real regime shifts. 5%/0.7% are calibrated to flag the
    # kind of intraday move that historically invalidates a sideways IC
    # without flagging routine chop.
    "regen_vix_pct_threshold":  5.0,
    "regen_spot_pct_threshold": 0.7,
    # ATM IV move threshold (vol points) — when ATM IV (per-underlying)
    # moves more than this from the day's first observed value the
    # OpportunityRegenWatcher fires an OPPORTUNITY_REGEN_HINT. Values are
    # absolute IV deltas (e.g. 12.0 → 17.0 = 5.0).
    "regen_iv_pct_threshold": 5.0,

    # VIX regime thresholds (% change vs prior close)
    "vix_rising_threshold":  5.0,
    "vix_spiking_threshold": 10.0,

    # Trend detection (Phase 1 upgrade)
    # Old: SMA20 vs SMA50 with 0.5% threshold (too sensitive to chop).
    # New: SMA crossover + slope direction + ADX strength.
    "trend_sma_diff_pct":     0.5,    # SMA20 vs SMA50 minimum % gap
    "trend_slope_min_pct":    0.05,   # SMA20 5-day slope (% of price) minimum
    "trend_adx_min":          20.0,   # ADX-14 below this = trend too weak, force SIDEWAYS
}


# ---------------------------------------------------------------------------
# Zerodha — charges calculator
# ---------------------------------------------------------------------------
ZERODHA_CONFIG = {
    "brokerage_per_order_inr": 20.0,            # flat ₹20 per leg per order
    "stt_sell_premium_pct":     0.0005,         # 0.05% of sell-side premium
    "stt_itm_expiry_intrinsic_pct": 0.00125,    # 0.125% on ITM expiry intrinsic value
    "exchange_txn_pct":         0.000530,       # 0.053% × turnover (both sides)
    "sebi_charges_pct":         0.000001,       # 0.0001% × turnover
    "stamp_duty_buy_pct":       0.00003,        # 0.003% × buy-side premium
    "gst_pct":                  0.18,           # 18% on (brokerage + exchange + SEBI)
}


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
SIMULATION_CONFIG = {
    # Day-1 entry classification
    "adjusted_max_gap_pct": 10.0,   # ≤10% gap from suggested → ADJUSTED; >10% → VOID
}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASHBOARD_CONFIG = {
    "host":         _env("OPT_DASHBOARD_HOST", "0.0.0.0"),
    "port":         _env_int("OPT_DASHBOARD_PORT", 5001),
    "debug":        _env_bool("OPT_DASHBOARD_DEBUG", False),
    "log_page_size": 200,
    "history_page_size": 50,
    # Note dismissal — a contextual note is hidden after this many displays
    "note_hide_after_views": 5,
    # Color scheme tokens (also exposed to CSS via /api/theme)
    "theme": {
        "primary":     "#0F766E",  # dark teal/emerald
        "primary_dim": "#0B5E58",
        "accent":      "#F59E0B",  # amber
        "surface":     "#1F2937",
        "surface_alt": "#111827",
        "text":        "#F3F4F6",
        "text_dim":    "#9CA3AF",
        "ok":          "#10B981",
        "warn":        "#F59E0B",
        "err":         "#EF4444",
        "info":        "#06B6D4",
    },
}


# ---------------------------------------------------------------------------
# Market-data providers (pluggable)
# ---------------------------------------------------------------------------
# OPT_PROVIDERS controls which adapter serves live market data:
#     ""         → nse_eod only (Mode A — current behaviour, no live feed)
#     "zerodha"  → Zerodha Kite for live; nse_eod for history & fallback
#
# READ-ONLY enforcement: the provider layer is market-data-only. The Zerodha
# adapter NEVER calls order/portfolio/holdings/margin endpoints — see
# /memories/session/plan.md "MARKET-DATA-ONLY ZERODHA INTEGRATION".
PROVIDERS_CONFIG = {
    "active": _env("OPT_PROVIDERS", "").strip().lower(),
    # Per-key TTLs for the in-process live cache (seconds).
    "cache_ttl_seconds_quote": float(_env("OPT_PROVIDER_CACHE_TTL_QUOTE", "5")),
    "cache_ttl_seconds_chain": float(_env("OPT_PROVIDER_CACHE_TTL_CHAIN", "5")),
    # Hard cap on the cache to avoid runaway memory.
    "cache_max_entries": _env_int("OPT_PROVIDER_CACHE_MAX_ENTRIES", 10_000),
}


# ---------------------------------------------------------------------------
# Zerodha (Kite Connect) — read-only market data API
# ---------------------------------------------------------------------------
# Used only when PROVIDERS_CONFIG["active"] == "zerodha".
# Credentials come from a SEPARATE Zerodha account (data subscription only),
# never the user's trading account. Daily login flow refreshes access_token
# (Kite tokens expire 06:00 IST every day).
#
# Note: the older `ZERODHA_CONFIG` above is the trading-charges calculator
# (brokerage, STT, etc.) and is unrelated to this API config.
ZERODHA_API_CONFIG = {
    "api_key":      _env("OPT_ZERODHA_API_KEY", ""),
    "api_secret":   _env("OPT_ZERODHA_API_SECRET", ""),
    # Persisted access_token — refreshed by the daily login job; safe to keep
    # blank in env (the dashboard supplies it after the request_token flow).
    "access_token": _env("OPT_ZERODHA_ACCESS_TOKEN", ""),
    # Hard kill switch — if False, the adapter refuses to initialise even if
    # OPT_PROVIDERS=zerodha. Useful for emergency disable from .env without
    # touching code.
    "enabled":      _env_bool("OPT_ZERODHA_ENABLED", True),
}


# ---------------------------------------------------------------------------
# Alerts / Notifications
# ---------------------------------------------------------------------------
ALERTS_CONFIG = {
    "email_enabled": _env_bool("OPT_EMAIL_ENABLED", False),
    "smtp_host":     _env("OPT_SMTP_HOST", ""),
    "smtp_port":     _env_int("OPT_SMTP_PORT", 587),
    "smtp_user":     _env("OPT_SMTP_USER", ""),
    "smtp_password": _env("OPT_SMTP_PASSWORD", ""),
    "smtp_from":     _env("OPT_SMTP_FROM", ""),
    "smtp_to":       [a.strip() for a in _env("OPT_SMTP_TO", "").split(",") if a.strip()],
    "smtp_use_tls":  _env_bool("OPT_SMTP_USE_TLS", True),

    # Severity levels that trigger email (lower severities only go to dashboard)
    "email_severities": ["CRITICAL", "ERROR"],

    # Telegram channel (Phase 5). The bot is created via @BotFather; chat_id can
    # be a personal user id, a group id (negative number), or a channel username
    # like "@my_channel". Disabled by default — must be explicitly opted in.
    "telegram_enabled":   _env_bool("OPT_TELEGRAM_ENABLED", False),
    "telegram_bot_token": _env("OPT_TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id":   _env("OPT_TELEGRAM_CHAT_ID", ""),
    # Severity floor for Telegram. Anything at or above this level is sent.
    "telegram_severities": ["CRITICAL", "ERROR", "WARNING"],
    "telegram_timeout_seconds": _env_int("OPT_TELEGRAM_TIMEOUT_SECONDS", 5),
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING_CONFIG = {
    # Levels: DEBUG / INFO / WARNING / ERROR / CRITICAL
    "console_level": _env("OPT_LOG_CONSOLE_LEVEL", "INFO"),
    "file_level":    _env("OPT_LOG_FILE_LEVEL", "INFO"),
    "db_level":      _env("OPT_LOG_DB_LEVEL", "INFO"),
    "log_dir":       _env("OPT_LOG_DIR", "logs"),
    "log_file_name": "options_advisor.log",
    "max_bytes":     10 * 1024 * 1024,
    "backup_count":  5,
    "format":        "%(asctime)s %(levelname)s %(name)s: %(message)s",
}


# ---------------------------------------------------------------------------
# Retention (weekly cleanup)
# ---------------------------------------------------------------------------
RETENTION_CONFIG = {
    "fo_bhav_keep_days":          730,   # 2 years of F&O EOD
    "spot_bhav_keep_days":        730,
    "vix_keep_days":              3650,  # 10 years (cheap, useful for IV%ile)
    "fii_keep_days":              730,
    "iv_history_keep_days":       730,
    "suggestions_keep_days":      1825,  # 5 years (audit)
    "trades_keep_days":           1825,
    "simulations_keep_days":      730,
    "system_logs_keep_days":      90,
    "job_log_keep_days":          90,
    "notifications_keep_days":    180,
    # 5-min chain trajectory tables (Zerodha WS aggregator).
    # ~450 rows/day per table (75 slots * 3 underlyings * 2 expiries).
    # 180 days ~= 80K rows ~= 10 MB; cheap and gives enough history
    # for the time-series replay backtester (future scope).
    "chain_5min_keep_days":       180,
    "atm_iv_5min_keep_days":      180,
}


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------
PATHS = {
    "data_dir":     _env("OPT_DATA_DIR", "data"),
    "logs_dir":     _env("OPT_LOGS_DIR", "logs"),
    "archive_dir":  _env("OPT_ARCHIVE_DIR", "archive"),
}


# ---------------------------------------------------------------------------
# High-impact events calendar
# ---------------------------------------------------------------------------
# Manually maintained list of HIGH-impact market events.
# The events_seeder job syncs this list to options_events_calendar on startup
# and weekly — so adding an event here is enough to get it into the gate.
#
# Sources to verify against each year:
#   RBI MPC schedule : https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx
#   Union Budget     : Ministry of Finance press releases
#   F&O Expiry       : already in options_expiry_calendar via fo_bhav_download
#
# impact must be 'HIGH' for the confidence gate to block suggestions.
# event_type codes: RBI_MPC, UNION_BUDGET, US_FOMC, GDP_RELEASE, CPI_RELEASE
EVENTS_CONFIG: list[dict] = [
    # ── 2026 RBI MPC Policy Decisions ──────────────────────────────────────
    {"date": "2026-02-07", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Feb 2026)",      "impact": "HIGH"},
    {"date": "2026-04-09", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Apr 2026)",      "impact": "HIGH"},
    {"date": "2026-06-06", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Jun 2026)",      "impact": "HIGH"},
    {"date": "2026-08-07", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Aug 2026)",      "impact": "HIGH"},
    {"date": "2026-10-09", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Oct 2026)",      "impact": "HIGH"},
    {"date": "2026-12-04", "event_type": "RBI_MPC",       "description": "RBI MPC Policy Decision (Dec 2026)",      "impact": "HIGH"},

    # ── 2026 Union Budget ───────────────────────────────────────────────────
    {"date": "2026-02-01", "event_type": "UNION_BUDGET",  "description": "Union Budget 2026-27 presentation",       "impact": "HIGH"},

    # ── 2026 US Fed FOMC Decisions (affect Indian VIX / FII flows) ─────────
    {"date": "2026-01-29", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Jan 2026)",    "impact": "HIGH"},
    {"date": "2026-03-19", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Mar 2026)",    "impact": "HIGH"},
    {"date": "2026-05-07", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (May 2026)",    "impact": "HIGH"},
    {"date": "2026-06-18", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Jun 2026)",    "impact": "HIGH"},
    {"date": "2026-07-30", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Jul 2026)",    "impact": "HIGH"},
    {"date": "2026-09-17", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Sep 2026)",    "impact": "HIGH"},
    {"date": "2026-11-05", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Nov 2026)",    "impact": "HIGH"},
    {"date": "2026-12-17", "event_type": "US_FOMC",       "description": "US Fed FOMC Rate Decision (Dec 2026)",    "impact": "HIGH"},

    # ── 2026 India GDP (quarterly, released ~2 months after quarter end) ───
    {"date": "2026-02-28", "event_type": "GDP_RELEASE",   "description": "India GDP Q3 FY26 (Oct-Dec 2025)",        "impact": "HIGH"},
    {"date": "2026-05-29", "event_type": "GDP_RELEASE",   "description": "India GDP Q4 FY26 (Jan-Mar 2026)",        "impact": "HIGH"},
    {"date": "2026-08-28", "event_type": "GDP_RELEASE",   "description": "India GDP Q1 FY27 (Apr-Jun 2026)",        "impact": "HIGH"},
    {"date": "2026-11-27", "event_type": "GDP_RELEASE",   "description": "India GDP Q2 FY27 (Jul-Sep 2026)",        "impact": "HIGH"},
]

# ---------------------------------------------------------------------------
# Sanity check on import (cheap, fail-fast)
# ---------------------------------------------------------------------------
def _validate() -> None:
    assert STRATEGY_CONFIG["dte_min"] < STRATEGY_CONFIG["dte_max"], "dte_min must be < dte_max"
    assert 0.0 < STRATEGY_CONFIG["risk_free_rate"] < 1.0, "risk_free_rate out of range"
    assert STRATEGY_CONFIG["iv_rank_buying_max"] < STRATEGY_CONFIG["iv_rank_writing_min"], \
        "iv_rank gates overlap"
    # Edge-score weights must sum to ≤100 (allows future components without overflow).
    _w = STRATEGY_CONFIG.get("edge_score_weights", {}) or {}
    assert sum(_w.values()) <= 100.0 + 1e-9, "edge_score_weights sum to >100"
    # Credit-to-width grading tiers must be ordered.
    _t = STRATEGY_CONFIG.get("credit_to_width_grade_thresholds", {}) or {}
    if _t:
        assert _t.get("good", 0) < _t.get("strong", 1), "credit_to_width grade tiers misordered"
    assert 0.0 <= ZERODHA_CONFIG["gst_pct"] < 1.0
    assert DASHBOARD_CONFIG["port"] != 5000, "5001 is the dedicated options port"


_validate()
