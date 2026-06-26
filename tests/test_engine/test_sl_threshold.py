"""Tests for engine.sl_threshold — per-strategy SL limits."""
import pytest

from engine.sl_threshold import effective_sl_rs, strategy_sl_config


def test_long_straddle_cap_binds_on_large_debit():
    threshold, label = effective_sl_rs(strategy="LONG_STRADDLE", max_loss_rs=70393.75)
    assert threshold == 10_000.0
    assert "cap" in label


def test_long_straddle_premium_fraction_on_small_debit():
    # Long strangle SL is the profit-first 50% fraction (wider MTM room for the
    # long-vol theta bleed); ₹10k cap doesn't bind on a ₹5k debit.
    threshold, label = effective_sl_rs(strategy="LONG_STRANGLE", max_loss_rs=5000.0)
    assert threshold == 2500.0
    assert "50%" in label


def test_iron_condor_fraction_only_when_max_loss_below_cap_floor():
    # max loss ₹15k < ₹20k floor → 50% only = ₹7.5k
    threshold, label = effective_sl_rs(strategy="IRON_CONDOR", max_loss_rs=15_000.0)
    assert threshold == 7500.0
    assert "50%" in label


def test_iron_condor_fraction_when_50pct_below_cap():
    # 50% × 28906 = 14453 < ₹15k cap
    threshold, label = effective_sl_rs(strategy="IRON_CONDOR", max_loss_rs=28906.5)
    assert threshold == pytest.approx(14453.25, abs=0.1)
    assert "50%" in label


def test_iron_condor_cap_binds_on_very_large_max_loss():
    # 50% × 40k = 20k > ₹15k cap (and max loss ≥ ₹20k floor)
    threshold, label = effective_sl_rs(strategy="IRON_CONDOR", max_loss_rs=40_000.0)
    assert threshold == 15_000.0
    assert "cap" in label


def test_bear_call_spread_uses_conditional_cap():
    cfg = strategy_sl_config("BEAR_CALL_SPREAD")
    assert cfg["cap_min_max_loss_rs"] == 20_000.0
    assert cfg["absolute_cap_rs"] == 15_000.0


def test_bull_put_spread_keeps_10k_cap_always():
    threshold, _ = effective_sl_rs(strategy="BULL_PUT_SPREAD", max_loss_rs=30_000.0)
    assert threshold == 10_000.0


def test_bear_call_spread_fraction_when_below_cap():
    threshold, label = effective_sl_rs(strategy="BEAR_CALL_SPREAD", max_loss_rs=5250.0)
    assert threshold == 2625.0
    assert "50%" in label


def test_jade_lizard_matches_bull_put_spread_fraction():
    jl = strategy_sl_config("JADE_LIZARD")
    bps = strategy_sl_config("BULL_PUT_SPREAD")
    assert jl["loss_fraction"] == bps["loss_fraction"]
