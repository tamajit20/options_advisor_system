"""Optional API-key gate for the Flask dashboard."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from flask import jsonify, make_response, redirect, request

from config import DASHBOARD_CONFIG

logger = logging.getLogger(__name__)

AUTH_COOKIE = "oa_dashboard_key"
LOGIN_PATH = "/dashboard-login"

_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_EXACT = {
    "/health",
    "/zerodha/callback",
    LOGIN_PATH,
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


def _set_auth_cookie(response, api_key: str):
    response.set_cookie(
        AUTH_COOKIE,
        api_key,
        httponly=True,
        samesite="Lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


def _login_page_html(*, error: str = "", next_path: str = "/") -> str:
    err = f'<p class="err">{error}</p>' if error else ""
    nxt = quote(next_path or "/", safe="/")
    note = ""
    if "#" in configured_api_key():
        note = (
            '<p class="hint">Your access key contains <code>#</code> — use this form, '
            "not <code>?api_key=</code> in the URL (browsers strip text after <code>#</code>).</p>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard access</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f1419; color: #e6edf3;
    display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #1a2332; border: 1px solid #2a3744; border-radius: 12px;
    padding: 28px 32px; width: min(360px, 92vw); box-shadow: 0 8px 32px rgba(0,0,0,.35); }}
  h1 {{ margin: 0 0 8px; font-size: 1.15rem; }}
  p {{ margin: 0 0 16px; color: #8b9cb3; font-size: .9rem; line-height: 1.45; }}
  .hint {{ font-size: .8rem; color: #fbbf24; }}
  .err {{ color: #f87171; }}
  label {{ display: block; font-size: .85rem; margin-bottom: 6px; color: #8b9cb3; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
    border: 1px solid #2a3744; background: #0f1419; color: #e6edf3; font-size: 1rem; }}
  button {{ margin-top: 16px; width: 100%; padding: 10px; border: none; border-radius: 8px;
    background: #14b8a6; color: #042f2e; font-weight: 600; font-size: 1rem; cursor: pointer; }}
  button:hover {{ filter: brightness(1.08); }}
  code {{ background: #0f1419; padding: 1px 4px; border-radius: 4px; }}
</style></head><body>
<div class="card">
  <h1>Trading Dashboard</h1>
  <p>Enter your access key to continue. The browser will remember it for 30 days.</p>
  {note}
  {err}
  <form method="post" action="{LOGIN_PATH}?next={nxt}">
    <label for="api_key">Access key</label>
    <input id="api_key" name="api_key" type="password" autocomplete="current-password" required autofocus>
    <button type="submit">Continue</button>
  </form>
</div></body></html>"""


def register_dashboard_auth(app) -> None:
    """When OPT_DASHBOARD_API_KEY is set, require it for all non-public routes."""
    api_key = configured_api_key()
    if not api_key:
        logger.info("Dashboard auth disabled (OPT_DASHBOARD_API_KEY not set)")
        return

    logger.info("Dashboard auth enabled — use /dashboard-login or X-API-Key header")

    @app.route(LOGIN_PATH, methods=["GET", "POST"])
    def dashboard_login():  # noqa: ANN001
        next_path = (request.args.get("next") or "/").strip() or "/"
        if not next_path.startswith("/"):
            next_path = "/"
        if _provided_key() == api_key:
            return redirect(next_path)
        if request.method == "POST":
            entered = (request.form.get("api_key") or "").strip()
            if entered == api_key:
                resp = make_response(redirect(next_path))
                return _set_auth_cookie(resp, api_key)
            return _login_page_html(error="Incorrect access key.", next_path=next_path), 401
        return _login_page_html(next_path=next_path)

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
                "hint": f"POST access key to {LOGIN_PATH} or set header X-API-Key",
            }), 401
        return redirect(f"{LOGIN_PATH}?next={quote(path, safe='/')}")

    @app.after_request
    def _remember_dashboard_api_key(response):  # noqa: ANN001
        if request.args.get("api_key") == api_key:
            _set_auth_cookie(response, api_key)
        return response
