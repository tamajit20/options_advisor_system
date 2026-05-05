"""
options_advisor_system / contracts.py
=====================================

Inter-module data shapes (dataclasses) — the LINGUA FRANCA between modules.

Rules:
    1. Dataclasses ONLY. No business logic, no DB calls, no I/O.
    2. Imported by every layer (downloader, engine, database, dashboard, ...)
       to keep modules decoupled.
    3. All fields use built-in types or other contracts. No third-party types
       leak through (e.g., no `pyodbc.Row`, no `pandas.DataFrame`).
    4. Add fields freely; never remove/rename without checking call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Raw data shapes (downloader → database)
# ---------------------------------------------------------------------------

@dataclass
class FoBhavRow:
    """A single row from the NSE F&O EOD bhav copy (filtered to OPTIDX/OPTSTK)."""
    trade_date:   date
    symbol:       str            # e.g., NIFTY
    instrument:   str            # OPTIDX / OPTSTK
    expiry_date:  date
    strike:       float
    option_type:  str            # CE / PE
    open_price:   float
    high_price:   float
    low_price:    float
    close_price:  float
    settle_price: float
    contracts:    int
    open_interest: int
    change_in_oi: int


@dataclass
class SpotBhavRow:
    """A single row from the NSE Cash-Market EOD bhav copy."""
    trade_date:  date
    symbol:      str
    open_price:  float
    high_price:  float
    low_price:   float
    close_price: float
    volume:      int


@dataclass
class VixRow:
    trade_date: date
    open_price: float
    high_price: float
    low_price:  float
    close_price: float


@dataclass
class FiiOiRow:
    """A single client-type row from NSE participant-wise OI."""
    trade_date:        date
    client_type:       str         # FII / DII / Pro / Client
    future_long:       int
    future_short:      int
    option_call_long:  int
    option_call_short: int
    option_put_long:   int
    option_put_short:  int


# ---------------------------------------------------------------------------
# Engine outputs
# ---------------------------------------------------------------------------

@dataclass
class IVResult:
    symbol:       str
    expiry_date:  date
    strike:       float
    option_type:  str
    spot:         float
    market_price: float
    iv:           float
    converged:    bool


@dataclass
class IVRankResult:
    symbol:        str
    as_of:         date
    current_iv:    float
    iv_low_52w:    float
    iv_high_52w:   float
    iv_rank:       float    # 0–100
    iv_percentile: float    # 0–100


@dataclass
class MarketIndicators:
    """Snapshot of market context for a single underlying on a single day."""
    symbol:        str
    as_of:         date
    spot:          float
    pcr:           Optional[float]   # None = OI data absent (call OI was zero or chain empty)
    max_pain:      float
    atr_14:        Optional[float]   # None = insufficient spot history (< period+1 rows)
    trend:         str               # BULLISH / BEARISH / SIDEWAYS / UNKNOWN
    vix_close:     Optional[float]   # None = VIX row not available today
    vix_regime:    str               # STABLE / RISING / SPIKING / UNKNOWN
    oi_walls_call: List[float]       # top call walls (strikes); empty when OI absent
    oi_walls_put:  List[float]
    expected_move: float             # spot × IV × √(DTE/365), in spot points
    hv_20:         Optional[float]   # 20-day historical/realised volatility (annualised)
    iv_premium:    Optional[float]   # atm_iv / hv_20 — how expensive options are vs realised vol
    fii_net_futures: Optional[float] # FII net futures position (long − short contracts)
    adx_14:           Optional[float] = None  # ADX-14 trend strength; None = insufficient history
    sma20_slope_pct:  Optional[float] = None  # SMA20 5-day slope as % of price; None = insufficient history
    sma_diff_pct:     Optional[float] = None  # (SMA20 - SMA50) / SMA50 * 100; None = insufficient history
    oi_pcr_change:    Optional[float] = None  # ΣΔPut OI / ΣΔCall OI — >1 puts building, <1 calls building; None = not available
    # Trajectory fields (populated only in live mode when WS history is present).
    # All None in EOD mode and when the 5-min table has < 3 samples for the underlying/expiry.
    oi_pcr_slope_5min:    Optional[float] = None  # % per sample slope of oi_pcr_change over recent window
    oi_pcr_persistence:   Optional[float] = None  # 0..1 directional persistence of OI PCR deltas
    atm_iv_slope_5min:    Optional[float] = None  # % per sample slope of ATM IV
    atm_iv_persistence:   Optional[float] = None  # 0..1 directional persistence of ATM IV
    atm_call_spread_bps:  Optional[float] = None  # current ATM call bid-ask spread (bps of mid)
    atm_put_spread_bps:   Optional[float] = None  # current ATM put bid-ask spread (bps of mid)
    volume_burst_z:       Optional[float] = None  # z-score of last-bucket volume vs trailing window mean


@dataclass
class ChainTrajectory:
    """Bundle of recent 5-min snapshots passed into `build_indicators` to
    populate the live trajectory fields. Built by the live suggestion engine
    from `ChainTimeseriesRepo` + `AtmIvTimeseriesRepo`.

    Each list is in chronological order (oldest first). Lists may be empty
    (no history yet) — callers should treat that as "no trajectory signal"
    and leave the indicator fields at None.
    """
    oi_pcr_change_series:   List[Optional[float]] = field(default_factory=list)
    atm_iv_series:          List[Optional[float]] = field(default_factory=list)
    call_volume_series:     List[Optional[float]] = field(default_factory=list)
    put_volume_series:      List[Optional[float]] = field(default_factory=list)
    latest_call_spread_bps: Optional[float] = None
    latest_put_spread_bps:  Optional[float] = None


@dataclass
class ConfidenceCheck:
    label:  str
    status: str    # "PASS" | "FAIL" | "SOFT_FAIL" | "PASS_WARN" | "PASS_ERROR"
    detail: str

    @property
    def passed(self) -> bool:
        """True only for PASS / PASS_WARN / PASS_ERROR.
        FAIL and SOFT_FAIL both count as not-passed for scoring/display."""
        return self.status not in ("FAIL", "SOFT_FAIL")


@dataclass
class ConfidenceResult:
    score:            int                # number of passes (0..7)
    total:            int
    all_passed:       bool
    checks:           List[ConfidenceCheck]
    failed_reasons:   List[str] = field(default_factory=list)

    @property
    def conditions_met(self) -> List[str]:
        return [c.label for c in self.checks if c.passed]

    @property
    def conditions_failed(self) -> List[str]:
        return [c.label for c in self.checks if not c.passed]


# ---------------------------------------------------------------------------
# Suggestion shapes
# ---------------------------------------------------------------------------

@dataclass
class SuggestionLeg:
    """A single leg inside a suggestion."""
    leg_order:           int        # 1, 2, 3, 4
    hedge_pair_leg:      Optional[int]  # leg_order of paired hedge leg (None if standalone)
    symbol:              str        # underlying (e.g., NIFTY)
    expiry_date:         date
    strike:              float
    option_type:         str        # CE / PE
    action:              str        # BUY / SELL
    lots:                int
    lot_size:            int
    suggested_price:     float
    suggested_price_low: float
    suggested_price_high: float
    leg_purpose_note:    str        # plain-English explanation


@dataclass
class ChargeBreakdown:
    brokerage:   float
    stt:         float
    exchange:    float
    sebi:        float
    stamp_duty:  float
    gst:         float
    total:       float


@dataclass
class SuggestionEconomics:
    net_credit:           float       # positive = credit, negative = debit
    max_profit:            float
    max_loss:              float
    upper_breakeven:       Optional[float]
    lower_breakeven:       Optional[float]
    stop_loss_level:       Optional[float]
    probability_of_profit: float       # 0–100
    estimated_charges:     ChargeBreakdown
    estimated_net_pnl:     float       # max_profit − total charges (best-case net)


@dataclass
class Suggestion:
    """A complete trade suggestion ready for DB insert + dashboard display."""
    suggestion_id:    str             # SUG-YYYYMMDD-NNN
    trade_name:       str             # NIFTY-CONDOR-MAY2-26
    generated_on:     datetime
    strategy:         str             # IRON_CONDOR / BULL_PUT_SPREAD / ...
    strategy_type:    str             # WRITING / BUYING
    underlying:       str
    expiry_date:      date
    expiry_type:      str             # "Monthly" | "Weekly"
    dte:              int
    spot_at_generation: float
    confidence:       ConfidenceResult
    legs:             List[SuggestionLeg]
    economics:        SuggestionEconomics
    execution_window: str             # e.g., "9:20 AM – 9:45 AM tomorrow (IST)"
    plain_english:    str             # explanation shown to user
    data_date:        Optional[date] = None  # NSE bhav date the analysis is based on (FO+IV)
    entry_date:       Optional[date] = None  # intended execution date (next trading day)
    spot_data_date:   Optional[date] = None  # actual trade_date of the spot row used
    fii_data_date:    Optional[date] = None  # actual trade_date of the FII row used
    vix_data_date:    Optional[date] = None  # trade_date of the most recent VIX row used
    oi_pcr_change:    Optional[float] = None  # OI change momentum: ΣΔPut OI / ΣΔCall OI (EOD=day-over-day, LIVE=since open)


@dataclass
class NoSuggestion:
    """Recorded when system stayed silent — drives the 'why no suggestion' UI."""
    generated_on:     datetime
    underlying:       str
    confidence:       ConfidenceResult
    reason:           str             # human summary, e.g., "IV Rank 35 (not >50 or <30)"


# ---------------------------------------------------------------------------
# Trade / lifecycle shapes
# ---------------------------------------------------------------------------

@dataclass
class TradeLegFill:
    """User-supplied fill for a single leg when marking a suggestion as executed."""
    leg_order:    int
    executed:     bool
    fill_price:   Optional[float]  # required if executed=True
    fill_time:    Optional[datetime]
    not_filled_reason: Optional[str] = None  # e.g., "price moved"
    lots_override: Optional[int] = None      # user-specified lot count (overrides suggestion)


@dataclass
class BrokenTradeOption:
    """A single ranked option presented by the broken-trade advisor."""
    rank:               int
    label:              str             # "Exit immediately"
    recommended:        bool
    estimated_pnl:      float
    when_to_use:        str
    zerodha_steps:      str
    time_sensitivity:   str             # "URGENT" / "BEFORE_2PM" / etc.


@dataclass
class ExitDecision:
    trade_id:    str
    decision:    str           # HOLD / EXIT_TOMORROW / SL_HIT / EXPIRE / TAKE_PROFIT / TIME_DECAY_DONE
    reason:      str
    as_of:       datetime


# ---------------------------------------------------------------------------
# Simulation shapes
# ---------------------------------------------------------------------------

@dataclass
class SimulationDayUpdate:
    suggestion_id:   str
    leg_order:       int
    sim_date:        date
    suggested_price: float
    sim_entry_price: Optional[float]
    open_price:      float
    high_price:      float
    low_price:       float
    settle_price:    float
    quality:         str             # FULL_VALID / ADJUSTED / VOID
    adjustment_note: str
    day_pnl:         float
    cumulative_pnl:  float
    is_expiry_day:   bool
    final_settle:    Optional[float]


# ---------------------------------------------------------------------------
# Logging / job tracking
# ---------------------------------------------------------------------------

@dataclass
class LogEntry:
    logged_at:  datetime
    level:      str
    module:     str
    job_id:     Optional[str]
    message:    str
    exception:  Optional[str]
    context:    Dict[str, object] = field(default_factory=dict)


@dataclass
class JobRun:
    job_id:        str        # e.g., "fo_bhav_download-2026-04-30"
    job_name:      str
    started_at:    datetime
    finished_at:   Optional[datetime]
    status:        str        # RUNNING / SUCCESS / FAILED / SKIPPED / CRITICAL
    error_message: Optional[str]
    rows_processed: Optional[int] = None


@dataclass
class Notification:
    created_at:               datetime
    notif_type:               str   # JOB_FAILURE / NO_SUGGESTION / NEW_SUGGESTION / ...
    severity:                 str   # INFO / WARNING / ERROR / CRITICAL
    title:                    str
    body:                     str
    related_suggestion_id:    Optional[str] = None
    related_trade_id:         Optional[str] = None
    # ---- Phase 2c provenance markers (all optional; None = unknown) ----
    source_event_id:          Optional[str] = None      # tick id / cycle id that triggered this
    provider:                 Optional[str] = None      # e.g. 'zerodha', 'nse_eod'
    tick_age_ms:              Optional[int] = None      # age of the tick at dispatch
    flag_state_at_dispatch:   Optional[str] = None      # JSON snapshot of runtime flags
