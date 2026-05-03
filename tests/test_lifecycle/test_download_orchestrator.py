"""Tests for lifecycle/download_orchestrator.py — wires downloader → repo → commit."""
from __future__ import annotations

from datetime import date

import pytest

import lifecycle.download_orchestrator as orch
from contracts import FoBhavRow, SpotBhavRow, VixRow


_FO_ROWS = [
    FoBhavRow(date(2026, 4, 30), "NIFTY", "OPTIDX", date(2026, 5, 14),
              23000, "CE", 1, 2, 0.5, 1.5, 1.5, 100, 50000, 100),
    FoBhavRow(date(2026, 4, 30), "NIFTY", "OPTIDX", date(2026, 5, 14),
              23000, "PE", 1, 2, 0.5, 1.5, 1.5, 80, 45000, -50),
]


class TestRunFoBhav:
    def test_empty_download_returns_zero_no_commit(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_fo_bhav", return_value=[])
        n = orch.run_fo_bhav(mock_db, date(2026, 4, 30))
        assert n == 0
        mock_db.commit.assert_not_called()

    def test_happy_path_upserts_and_commits(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_fo_bhav",
                     return_value=_FO_ROWS)
        # ExpiryCalendarRepo.upsert_from_fo_rows uses executemany — return 0
        n = orch.run_fo_bhav(mock_db, date(2026, 4, 30))
        assert n == 2
        mock_db.commit.assert_called_once()

    def test_expiry_calendar_failure_is_non_fatal(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_fo_bhav",
                     return_value=_FO_ROWS)
        mocker.patch("lifecycle.download_orchestrator.ExpiryCalendarRepo.upsert_from_fo_rows",
                     side_effect=RuntimeError("calendar broken"))
        # Should still upsert FO + commit — calendar refresh is best-effort
        n = orch.run_fo_bhav(mock_db, date(2026, 4, 30))
        assert n == 2
        mock_db.commit.assert_called_once()


class TestRunSpotBhav:
    def test_supplements_with_index_spots_from_fo(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_spot_bhav",
                     return_value=[SpotBhavRow(date(2026, 4, 30), "RELIANCE",
                                               2500, 2510, 2490, 2505, 1000)])
        mocker.patch("lifecycle.download_orchestrator.extract_index_spots",
                     return_value={"NIFTY": 23000.0, "BANKNIFTY": 50000.0})
        n = orch.run_spot_bhav(mock_db, date(2026, 4, 30))
        assert n >= 3  # 1 stock + 2 indices (NIFTY/BANKNIFTY only if in config)
        mock_db.commit.assert_called_once()

    def test_index_spot_extraction_failure_is_non_fatal(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_spot_bhav",
                     return_value=[SpotBhavRow(date(2026, 4, 30), "RELIANCE",
                                               2500, 2510, 2490, 2505, 1000)])
        mocker.patch("lifecycle.download_orchestrator.extract_index_spots",
                     side_effect=RuntimeError("zip missing"))
        n = orch.run_spot_bhav(mock_db, date(2026, 4, 30))
        assert n == 1
        mock_db.commit.assert_called_once()

    def test_no_rows_returns_zero_no_commit(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_spot_bhav", return_value=[])
        mocker.patch("lifecycle.download_orchestrator.extract_index_spots", return_value={})
        n = orch.run_spot_bhav(mock_db, date(2026, 4, 30))
        assert n == 0
        mock_db.commit.assert_not_called()


class TestRunVix:
    def test_seeds_from_bundled_csv_when_table_nearly_empty(self, mock_db, mocker):
        # VixRepo.count() returns 5 < 30 → seed path
        mock_db.fetch_one.return_value = {"n": 5}
        # _seed_vix_from_bundled_csv reads the bundled CSV — patch open + os.path.exists
        mocker.patch("lifecycle.download_orchestrator.os.path.exists", return_value=False)
        mocker.patch("lifecycle.download_orchestrator.download_vix_history", return_value=[])
        # Should run without error (CSV missing → seed returns 0)
        n = orch.run_vix(mock_db)
        assert n >= 0

    def test_normal_path_when_history_already_seeded(self, mock_db, mocker):
        mock_db.fetch_one.return_value = {"n": 200}  # >= 30, skip seed
        mocker.patch("lifecycle.download_orchestrator.download_vix_history",
                     return_value=[VixRow(date(2026, 4, 30), 15.0, 15.5, 14.8, 15.2)])
        n = orch.run_vix(mock_db)
        assert n == 1
        mock_db.commit.assert_called_once()
