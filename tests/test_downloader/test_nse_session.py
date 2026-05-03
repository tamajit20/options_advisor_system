"""Tests for downloader/nse_session.py — retry/backoff behaviour with mocked Session."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from downloader.nse_session import fetch_with_retry


def _resp(status: int, body: bytes = b""):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.content = body
    return r


class TestFetchWithRetry:
    def test_returns_response_on_200(self):
        sess = MagicMock()
        sess.get.return_value = _resp(200, b"ok")
        out = fetch_with_retry(sess, "http://x", max_retries=1)
        assert out is not None
        assert out.status_code == 200
        assert sess.get.call_count == 1

    def test_returns_none_on_404_when_accept_404(self):
        sess = MagicMock()
        sess.get.return_value = _resp(404)
        out = fetch_with_retry(sess, "http://x", accept_404=True, max_retries=1)
        assert out is None

    def test_retries_on_5xx(self, mocker):
        """500 is treated as retryable until success or attempts exhausted."""
        mocker.patch("downloader.nse_session.time.sleep", return_value=None)
        sess = MagicMock()
        sess.get.side_effect = [_resp(500), _resp(500), _resp(200)]
        out = fetch_with_retry(sess, "http://x", max_retries=3)
        assert out is not None and out.status_code == 200
        assert sess.get.call_count == 3

    def test_raises_after_exhausting_retries(self, mocker):
        mocker.patch("downloader.nse_session.time.sleep", return_value=None)
        sess = MagicMock()
        sess.get.side_effect = requests.ConnectionError("boom")
        with pytest.raises(Exception):
            fetch_with_retry(sess, "http://x", max_retries=2)
        assert sess.get.call_count == 2

    def test_404_without_accept_flag_eventually_raises_or_returns(self, mocker):
        """A 404 when accept_404=False should not silently succeed.
        It either raises or is treated as an error after retries."""
        mocker.patch("downloader.nse_session.time.sleep", return_value=None)
        sess = MagicMock()
        sess.get.return_value = _resp(404)
        # The function should NOT return a 404 response as success
        try:
            result = fetch_with_retry(sess, "http://x", accept_404=False, max_retries=1)
        except Exception:
            return  # acceptable: raised
        assert result is None or result.status_code != 404
