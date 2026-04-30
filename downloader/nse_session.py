"""
downloader/nse_session.py
=========================

Shared NSE HTTP session with cookie warm-up + retry.

Returns a `requests.Session` with NSE's anti-bot cookies installed.
Used by every NSE downloader. NO business logic, NO DB writes.

Pattern adapted (copied, NOT imported) from the equity stock_analyzer's
`download_eod.py` — the two systems must remain independent.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from config import NSE_CONFIG

logger = logging.getLogger(__name__)


def make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(NSE_CONFIG["headers"])
    try:
        sess.get(NSE_CONFIG["warmup_url"], timeout=NSE_CONFIG["request_timeout"])
    except Exception as exc:
        logger.debug("NSE warm-up request failed (non-fatal): %s", exc)
    return sess


def fetch_with_retry(
    session: requests.Session,
    url: str,
    *,
    timeout: Optional[int] = None,
    max_retries: Optional[int] = None,
    accept_404: bool = False,
) -> Optional[requests.Response]:
    """GET `url` with retry/backoff. Returns the response on success.

    On 404 (file not posted yet) returns None when `accept_404=True`,
    otherwise raises.
    """
    timeout = timeout or NSE_CONFIG["request_timeout"]
    max_retries = max_retries if max_retries is not None else NSE_CONFIG["max_retries"]
    backoff = NSE_CONFIG["retry_backoff_seconds"]

    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404 and accept_404:
                logger.info("404 (accepted) for %s", url)
                return None
            logger.warning(
                "Non-200 (%d) for %s [attempt %d/%d]",
                resp.status_code, url, attempt, max_retries,
            )
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                "Request error %s [attempt %d/%d]: %s",
                url, attempt, max_retries, exc,
            )
        time.sleep(backoff * attempt)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts")
