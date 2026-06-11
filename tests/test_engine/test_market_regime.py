"""Tests for engine/market_regime.py — sit-out transparency helpers."""
from __future__ import annotations

import json

from engine.market_regime import (
    classify_market_regime,
    parse_conditions_metrics,
    regime_from_sit_out_row,
    summarize_market_sit_out,
)


class TestClassifyMarketRegime:
    def test_dead_zone_mid_iv(self):
        r = classify_market_regime(38.0, 1.17)
        assert r["id"] == "dead_zone"
        assert "30" in r["summary"] or "50" in r["summary"]

    def test_writing_regime(self):
        r = classify_market_regime(62.0, 1.1)
        assert r["id"] == "writing"

    def test_buying_regime(self):
        r = classify_market_regime(22.0, 0.95)
        assert r["id"] == "buying"


class TestParseConditionsMetrics:
    def test_extracts_iv_rank_and_premium(self):
        checks = [
            {
                "label": "IV Rank in actionable zone",
                "detail": "IV Rank 35.4 (need >50 or <30)",
            },
            {
                "label": "IV premium vs realised vol (HV-20)",
                "detail": "IV/HV ratio 1.17× (between 1.00 and 1.20 — neutral)",
            },
        ]
        m = parse_conditions_metrics(json.dumps(checks))
        assert m["iv_rank"] == 35.4
        assert m["iv_premium"] == 1.17


class TestSummarizeMarketSitOut:
    def test_all_dead_zone(self):
        row = {
            "underlying": "NIFTY",
            "conditions_json": json.dumps([
                {"label": "IV Rank in actionable zone",
                 "detail": "IV Rank 40.0 (need >50 or <30)"},
            ]),
        }
        summary = summarize_market_sit_out([row])
        assert summary is not None
        assert "dead zone" in summary["title"].lower()

    def test_regime_from_row(self):
        row = {
            "conditions_json": json.dumps([
                {"label": "IV Rank in actionable zone",
                 "detail": "IV Rank 55.0 (need >50 or <30)"},
            ]),
        }
        assert regime_from_sit_out_row(row)["id"] == "writing"
