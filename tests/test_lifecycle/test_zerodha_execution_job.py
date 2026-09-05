"""Tests for lifecycle/zerodha_execution_job.py."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lifecycle.zerodha_execution_job import submit_execution_job


def test_submit_rejects_when_suggestion_job_already_running(mock_db, mocker):
    repo = MagicMock()
    repo.running_for_suggestion.return_value = {"id": 9, "status": "RUNNING"}
    mocker.patch(
        "lifecycle.zerodha_execution_job.ZerodhaExecutionJobRepo",
        return_value=repo,
    )
    with pytest.raises(RuntimeError, match="already running"):
        submit_execution_job(
            mock_db,
            operation="ENTRY",
            suggestion_id="SUG-1",
            trade_id=None,
            total_legs=2,
            runner=lambda *_a: None,
        )
    repo.insert.assert_not_called()


def test_submit_rejects_when_trade_job_already_running(mock_db, mocker):
    repo = MagicMock()
    repo.running_for_suggestion.return_value = None
    repo.running_for_trade.return_value = {"id": 3, "status": "RUNNING"}
    mocker.patch(
        "lifecycle.zerodha_execution_job.ZerodhaExecutionJobRepo",
        return_value=repo,
    )
    with pytest.raises(RuntimeError, match="already running"):
        submit_execution_job(
            mock_db,
            operation="EXIT",
            suggestion_id=None,
            trade_id="TRD-1",
            total_legs=2,
            runner=lambda *_a: None,
        )
    repo.insert.assert_not_called()
