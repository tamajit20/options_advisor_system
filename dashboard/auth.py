"""Optional API-key gate for the Flask dashboard."""

from __future__ import annotations

import logging
from typing import Optional

from flask import jsonify, request

from config import DASHBOARD_CONFIG

logger = logging.getLogger(__name__)

AUTH_COOKIE = "oa_dashboard_key"

_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_EXACT = {
    "/health",
    "/zerodha/callback",
}


def configured_api_key() -> str:
    return str(DASHBOARD_CONFIG.get("api_key") or "").strip()


def _provided_key() -> Optional[str]:
    header = (request.headers.get("X-API-Key") or "").strip()
    if header:
        return header
    cookie = (request.cookies.get(AUTH_COOKIE) or "").strip()
    if cookie:
        return cookie
    query = (request.args.get("api_key") or "").strip()
    if query:
        return query
    return None


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def register_dashboard_auth(app) -> None:
    """When OPT_DASHBOARD_API_KEY is set, require it for all non-public routes."""
    api_key = configured_api_key()
    if not api_key:
        logger.info("Dashboard auth disabled (OPT_DASHBOARD_API_KEY not set)")
        return

    logger.info("Dashboard auth enabled — use X-API-Key header or ?api_key= on first visit")

    @app.before_request
    def _require_dashboard_api_key():  # noqa: ANN001
        path = request.path or "/"
        if _is_public_path(path):
            return None
        if _provided_key() == api_key:
            return None
        if path.startswith("/api/") or request.method != "GET":
            return jsonify({
                "error": "Unauthorized",
                "hint": "Set header X-API-Key or visit /?api_key=YOUR_KEY once to store a cookie",
            }), 401
        return (
            "Unauthorized. Append ?api_key=YOUR_KEY to this URL once, "
            "or set the X-API-Key header on API calls.",
            401,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    @app.after_request
    def _remember_dashboard_api_key(response):  # noqa: ANN001
        if request.args.get("api_key") == api_key:
            response.set_cookie(
                AUTH_COOKIE,
                api_key,
                httponly=True,
                samesite="Lax",
                max_age=60 * 60 * 24 * 30,
            )
        return response
