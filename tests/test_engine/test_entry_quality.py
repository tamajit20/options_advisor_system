"""Unit tests for engine.entry_quality — composite suggestion quality score."""

from engine.entry_quality import compute


class TestCompute:
    def test_all_none_returns_none(self):
        assert compute(edge_score=None, confidence_score=None, probability_of_profit=None) is None

    def test_matches_dashboard_formula(self):
        # edge=72, conf=10, pop=68 → 72*0.5 + (10/14*100)*0.3 + 68*0.2 = 36+21.43+13.6 ≈ 71
        assert compute(edge_score=72, confidence_score=10, probability_of_profit=68) == 71

    def test_confidence_capped_at_100_pct(self):
        assert compute(edge_score=0, confidence_score=14, probability_of_profit=0) == 30

    def test_missing_confidence_uses_neutral_50(self):
        # 0 + 50*0.3 + 0 = 15
        assert compute(edge_score=0, confidence_score=None, probability_of_profit=0) == 15

    def test_edge_only(self):
        # neutral confidence (50) contributes 50*0.3 = 15
        assert compute(edge_score=80, confidence_score=None, probability_of_profit=None) == 55
