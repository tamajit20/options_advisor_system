"""Closed-trade gate and condition report."""
import json

from engine.closed_trade_report import build_closed_trade_report, exit_bucket


def _trade(pnl, *, checks=None, underlying="NIFTY", dte=12, iv_detail=None,
           credit="good", quality=70, exit_signal=None, daily_status=None):
    rows = list(checks or [])
    if iv_detail:
        rows.append({
            "label": "IV Rank in actionable zone",
            "status": "PASS",
            "detail": iv_detail,
            "kind": "SOFT",
        })
    return {
        "net_pnl": pnl,
        "underlying": underlying,
        "dte": dte,
        "credit_grade": credit,
        "entry_quality_score": quality,
        "conditions_json": json.dumps(rows),
        "exit_signal": exit_signal,
        "daily_status": daily_status,
    }


def test_gate_that_passes_both_sides_is_not_separating():
    check = {"label": "VIX stable or falling", "status": "PASS", "kind": "SOFT"}
    trades = [
        _trade(100, checks=[check]),
        _trade(80, checks=[check]),
        _trade(-50, checks=[check]),
        _trade(-20, checks=[check]),
    ]
    report = build_closed_trade_report(trades)
    row = report["gates"][0]
    assert row["win_pass"] == 2 and row["loss_pass"] == 2
    assert "not separating" in row["read"]


def test_warning_that_lands_on_losers_is_called_out():
    def note(status):
        return {"label": "Range trade vs OI PCR drift", "status": status, "kind": "ADVISORY"}
    trades = [
        _trade(100, checks=[note("PASS")]),
        _trade(90, checks=[note("PASS")]),
        _trade(80, checks=[note("PASS")]),
        _trade(-40, checks=[note("SOFT_FAIL")]),
        _trade(-30, checks=[note("SOFT_FAIL")]),
        _trade(-20, checks=[note("SOFT_FAIL")]),
    ]
    report = build_closed_trade_report(trades)
    assert report["gates"] == []
    row = report["advisories"][0]
    assert row["win_pass"] == 3 and row["loss_pass"] == 0
    assert row["read"] == "Warning sits more often on losers"
    assert row["avg_pnl_warned"] < 0


def test_slices_cover_index_dte_iv_exit_and_event():
    quiet = {"label": "No high-impact event this week", "status": "PASS", "kind": "ADVISORY"}
    event = {"label": "Event inside the hold", "status": "SOFT_FAIL", "kind": "ADVISORY"}
    trades = [
        _trade(100, underlying="NIFTY", dte=8, iv_detail="IV Rank 60.0",
               checks=[quiet], exit_signal="TARGET_HIT"),
        _trade(-200, underlying="BANKNIFTY", dte=18, iv_detail="IV Rank 40.0",
               checks=[event], exit_signal="LOSS_LIMIT_HIT"),
        _trade(50, underlying="NIFTY", dte=8, iv_detail="IV Rank 62",
               daily_status="AUTO_SETTLED"),
    ]
    report = build_closed_trade_report(trades)
    names = {r["name"] for r in report["by_underlying"]}
    assert names == {"NIFTY", "BANKNIFTY"}
    dte_names = {r["name"] for r in report["by_dte"]}
    assert "7–10 DTE" in dte_names and "15–21 DTE" in dte_names
    iv_names = {r["name"] for r in report["by_iv_rank"]}
    assert "IV rank 50–70" in iv_names and "IV rank 30–50" in iv_names
    exits = {r["name"] for r in report["by_exit"]}
    assert "Target" in exits and "Stop" in exits and "Expiry settlement" in exits
    events = {r["name"]: r for r in report["by_event"]}
    assert events["High-impact event in the hold"]["total"] == 1
    assert events["No high-impact event flagged"]["wins"] == 2


def test_thin_sample_and_exit_bucket():
    report = build_closed_trade_report([
        _trade(10, checks=[{"label": "PCR in neutral band", "status": "PASS", "kind": "SOFT"}]),
    ])
    assert report["gates"][0]["read"] == "Too few trades to judge"
    assert exit_bucket(None, None) == "Manual close"
    assert exit_bucket("PROFIT_FLOOR_HIT", None) == "Profit floor"
