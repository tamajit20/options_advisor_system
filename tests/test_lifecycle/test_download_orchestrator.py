"""Tests for lifecycle/download_orchestrator.py — wires downloader → repo → commit."""
from __future__ import annotations

from datetime import date

import pytest

import lifecycle.download_orchestrator as orch
from contracts import FoBhavRow, SpotBhavRow, VixRow
from exceptions import NoDataError


_FO_ROWS = [
    FoBhavRow(date(2026, 4, 30), "NIFTY", "OPTIDX", date(2026, 5, 14),
              23000, "CE", 1, 2, 0.5, 1.5, 1.5, 100, 50000, 100),
    FoBhavRow(date(2026, 4, 30), "NIFTY", "OPTIDX", date(2026, 5, 14),
              23000, "PE", 1, 2, 0.5, 1.5, 1.5, 80, 45000, -50),
]


class TestRunFoBhav:
    def test_empty_download_includes_latest_available(self, mock_db, mocker):
        mock_db.scalar.return_value = date(2026, 7, 29)
        mocker.patch("lifecycle.download_orchestrator.download_fo_bhav", return_value=[])
        with pytest.raises(NoDataError, match="Latest available in DB: 2026-07-29"):
            orch.run_fo_bhav(mock_db, date(2026, 7, 30))
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
        mocker.patch("lifecycle.download_orchestrator.download_nse_index_spot",
                     return_value=[
                         SpotBhavRow(date(2026, 4, 30), "NIFTY",
                                     22900, 23100, 22800, 23050, 0),
                         SpotBhavRow(date(2026, 4, 30), "BANKNIFTY",
                                     49900, 50100, 49800, 50050, 0),
                     ])
        mocker.patch("lifecycle.download_orchestrator.extract_index_spots",
                     return_value={"NIFTY": 23000.0, "BANKNIFTY": 50000.0})
        n = orch.run_spot_bhav(mock_db, date(2026, 4, 30))
        assert n >= 3  # 1 stock + 2 indices (NIFTY/BANKNIFTY only if in config)
        mock_db.commit.assert_called_once()

    def test_index_spot_extraction_failure_is_non_fatal(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_spot_bhav",
                     return_value=[SpotBhavRow(date(2026, 4, 30), "RELIANCE",
                                               2500, 2510, 2490, 2505, 1000)])
        mocker.patch("lifecycle.download_orchestrator.download_nse_index_spot", return_value=[])
        mocker.patch("lifecycle.download_orchestrator.extract_index_spots",
                     side_effect=RuntimeError("zip missing"))
        n = orch.run_spot_bhav(mock_db, date(2026, 4, 30))
        assert n == 1
        mock_db.commit.assert_called_once()

    def test_no_rows_raises_no_data_error(self, mock_db, mocker):
        mocker.patch("lifecycle.download_orchestrator.download_spot_bhav", return_value=[])
        mocker.patch("lifecycle.download_orchestrator.download_nse_index_spot", return_value=[])
        mocker.patch("lifecycle.download_orchestrator.extract_index_spots", return_value={})
        with pytest.raises(NoDataError, match="Spot bhavcopy not available"):
            orch.run_spot_bhav(mock_db, date(2026, 4, 30))
        mock_db.commit.assert_not_called()


class TestRunVix:
    def test_seeds_from_bundled_csv_when_table_nearly_empty(self, mock_db, mocker):
        mock_db.fetch_one.return_value = {"n": 5}
        mocker.patch("lifecycle.download_orchestrator.load_bundled_vix_rows", return_value=[])
        mocker.patch("lifecycle.data_backfill.dates_to_process", return_value=[])
        mocker.patch("lifecycle.download_orchestrator.download_vix_history", return_value=[])
        with pytest.raises(NoDataError, match="VIX data not available"):
            orch.run_vix(mock_db)

    def test_normal_path_when_history_already_seeded(self, mock_db, mocker):
        mock_db.fetch_one.return_value = {"n": 200}
        mocker.patch("lifecycle.data_backfill.dates_to_process",
                     return_value=[date(2026, 4, 30)])
        mocker.patch(
            "lifecycle.download_orchestrator._run_vix_for_date",
            return_value=1,
        )
        hist = mocker.patch("lifecycle.download_orchestrator.download_vix_history")
        n = orch.run_vix(mock_db)
        assert n == 1
        hist.assert_not_called()

    def test_trade_date_override_uses_per_date_download(self, mock_db, mocker):
        mock_db.fetch_one.return_value = {"n": 200}
        mocker.patch(
            "lifecycle.download_orchestrator.download_vix_for_date",
            return_value=[VixRow(date(2026, 4, 30), 15.0, 15.5, 14.8, 15.2)],
        )
        hist = mocker.patch("lifecycle.download_orchestrator.download_vix_history")
        n = orch.run_vix(mock_db, date(2026, 4, 30))
        assert n == 1
        hist.assert_not_called()
        mock_db.commit.assert_called_once()

    def test_trade_date_override_raises_when_missing(self, mock_db, mocker):
        mock_db.fetch_one.return_value = {"n": 200}
        mocker.patch("lifecycle.download_orchestrator.download_vix_for_date", return_value=[])
        with pytest.raises(NoDataError, match="VIX data not available for 2026-04-30"):
            orch.run_vix(mock_db, date(2026, 4, 30))

    def test_auto_mode_backfills_missing_dates(self, mock_db, mocker):
        mocker.patch("lifecycle.data_backfill.dates_to_process",
                     return_value=[date(2026, 4, 28), date(2026, 4, 30)])
        single = mocker.patch(
            "lifecycle.download_orchestrator._run_fo_bhav_for_date",
            side_effect=[5, 7],
        )
        n = orch.run_fo_bhav(mock_db)
        assert n == 12
        assert single.call_count == 2
