"""App-user login for the Flask dashboard (multi-user).

Primary auth: ``app_users`` login + signed session cookie.
Legacy ``OPT_DASHBOARD_API_KEY`` remains optional break-glass (maps to admin).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

from flask import g, jsonify, make_response, redirect, request

from config import DASHBOARD_CONFIG

logger = logging.getLogger(__name__)

AUTH_COOKIE = "oa_user_session"
LEGACY_API_COOKIE = "oa_dashboard_key"
LOGIN_PATH = "/dashboard-login"
LOGOUT_PATH = "/dashboard-logout"

_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_EXACT = {
    "/health",
    "/zerodha/callback",
    LOGIN_PATH,
    LOGOUT_PATH,
}

_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0, private"


def configured_api_key() -> str:
    return str(DASHBOARD_CONFIG.get("api_key") or "").strip()


def session_secret() -> str:
    secret = str(DASHBOARD_CONFIG.get("session_secret") or "").strip()
    if secret:
        return secret
    # Deterministic fallback so existing installs work before secret is set.
    api = configured_api_key() or "options-advisor-dev"
    return hashlib.sha256(f"oa-session|{api}".encode("utf-8")).hexdigest()


def _sign(payload: str) -> str:
    return hmac.new(
        session_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def mint_session_token(user_id: int, *, ttl_sec: int = 60 * 60 * 24 * 30) -> str:
    exp = int(time.time()) + int(ttl_sec)
    body = f"{int(user_id)}.{exp}"
    return f"{body}.{_sign(body)}"


def parse_session_token(token: str) -> Optional[int]:
    raw = (token or "").strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    uid_s, exp_s, sig = parts
    body = f"{uid_s}.{exp_s}"
    if not hmac.compare_digest(_sign(body), sig):
        return None
    try:
        if int(exp_s) < int(time.time()):
            return None
        return int(uid_s)
    except (TypeError, ValueError):
        return None


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _set_user_cookie(response, user_id: int):
    response.set_cookie(
        AUTH_COOKIE,
        mint_session_token(user_id),
        httponly=True,
        samesite="Lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return response


def _clear_auth_cookies(response):
    """Expire app auth cookies for every common path/host quirk."""
    for name in (AUTH_COOKIE, LEGACY_API_COOKIE):
        response.set_cookie(
            name,
            "",
            expires=0,
            max_age=0,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        response.delete_cookie(name, path="/")
    return response


def _apply_no_store(response):
    response.headers["Cache-Control"] = _NO_STORE
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _login_page_html(*, error: str = "", next_path: str = "/", signed_out: bool = False) -> str:
    err = f'<p class="err">{error}</p>' if error else ""
    nxt = quote(next_path or "/", safe="/")
    signed = (
        '<p class="ok">Signed out. Live market feed is unchanged for other users.</p>'
        if signed_out else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<title>Dashboard login</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f1419; color: #e6edf3;
    display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #1a2332; border: 1px solid #2a3744; border-radius: 12px;
    padding: 28px 32px; width: min(380px, 92vw); box-shadow: 0 8px 32px rgba(0,0,0,.35); }}
  h1 {{ margin: 0 0 8px; font-size: 1.15rem; }}
  p {{ margin: 0 0 16px; color: #8b9cb3; font-size: .9rem; line-height: 1.45; }}
  .err {{ color: #f87171; }}
  .ok {{ color: #6ee7b7; }}
  label {{ display: block; font-size: .85rem; margin: 12px 0 6px; color: #8b9cb3; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
    border: 1px solid #2a3744; background: #0f1419; color: #e6edf3; font-size: 1rem; }}
  button {{ margin-top: 16px; width: 100%; padding: 10px; border: none; border-radius: 8px;
    background: #14b8a6; color: #042f2e; font-weight: 600; font-size: 1rem; cursor: pointer; }}
  button:hover {{ filter: brightness(1.08); }}
</style>
<script>
(function () {{
  // Drop any dashboard history entry so Back stays on login.
  try {{
    if (window.history && history.replaceState) {{
      history.replaceState(null, "", "{LOGIN_PATH}");
    }}
  }} catch (_) {{}}
  window.addEventListener("pageshow", function (ev) {{
    // BFCache restore of a prior dashboard must bounce back to login.
    if (ev.persisted) {{
      try {{ location.replace("{LOGIN_PATH}"); }} catch (_) {{ location.href = "{LOGIN_PATH}"; }}
    }}
  }});
}})();
</script>
</head><body>
<div class="card">
  <h1>Trading Dashboard</h1>
  <p>Sign in with your app username and password.</p>
  {signed}
  {err}
  <form method="post" action="{LOGIN_PATH}?next={nxt}">
    <label for="login">Username</label>
    <input id="login" name="login" type="text" autocomplete="username" required autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
</div></body></html>"""


def _load_user_from_cookie() -> Optional[dict]:
    token = (request.cookies.get(AUTH_COOKIE) or "").strip()
    uid = parse_session_token(token) if token else None
    if uid is None:
        return None
    try:
        from database.connection import SQLServerConnection
        from database.app_user_repo import AppUserRepo
        db = SQLServerConnection()
        db.connect()
        try:
            user = AppUserRepo(db).get(uid)
            if user and int(user.get("is_active") or 0):
                return AppUserRepo.public_row(user)
        finally:
            db.close()
    except Exception:
        logger.debug("auth: load user failed", exc_info=True)
    return None


def _legacy_api_key_ok() -> bool:
    api_key = configured_api_key()
    if not api_key:
        return False
    header = (request.headers.get("X-API-Key") or "").strip()
    cookie = (request.cookies.get(LEGACY_API_COOKIE) or "").strip()
    query = (request.args.get("api_key") or "").strip()
    provided = header or cookie or query
    return bool(provided) and hmac.compare_digest(provided, api_key)


def _admin_break_glass_user() -> Optional[dict]:
    """Map valid legacy API key to the current admin user."""
    if not _legacy_api_key_ok():
        return None
    try:
        from database.connection import SQLServerConnection
        from database.app_user_repo import AppUserRepo
        db = SQLServerConnection()
        db.connect()
        try:
            admin = AppUserRepo(db).get_admin()
            return AppUserRepo.public_row(admin)
        finally:
            db.close()
    except Exception:
        logger.debug("auth: break-glass admin lookup failed", exc_info=True)
    return None


def current_user() -> Optional[Dict[str, Any]]:
    return getattr(g, "app_user", None)


def current_user_id() -> Optional[int]:
    u = current_user()
    if not u:
        return None
    try:
        return int(u["id"])
    except (KeyError, TypeError, ValueError):
        return None


def require_admin() -> Optional[tuple]:
    """Return a Flask (response, status) if caller is not admin; else None."""
    u = current_user()
    if u and u.get("is_admin"):
        return None
    return jsonify({"error": "Admin only"}), 403


def users_exist() -> bool:
    try:
        from database.connection import SQLServerConnection
        from database.app_user_repo import AppUserRepo
        db = SQLServerConnection()
        db.connect()
        try:
            return AppUserRepo(db).count() > 0
        finally:
            db.close()
    except Exception:
        return False


def auth_required() -> bool:
    if not bool(DASHBOARD_CONFIG.get("require_app_login", True)):
        return False
    if users_exist():
        return True
    # No users yet — still require login form once bootstrap env is present
    login = str(DASHBOARD_CONFIG.get("bootstrap_admin_login") or "").strip()
    password = str(DASHBOARD_CONFIG.get("bootstrap_admin_password") or "")
    return bool(login and password)


def register_dashboard_auth(app) -> None:
    """Require app login (or legacy API key → admin) for non-public routes."""

    @app.route(LOGIN_PATH, methods=["GET", "POST"])
    def dashboard_login():  # noqa: ANN001
        next_path = (request.args.get("next") or "/").strip() or "/"
        if not next_path.startswith("/"):
            next_path = "/"
        # Never bounce a just-signed-out browser back into the dashboard.
        signed_out = (request.args.get("signed_out") or "") in ("1", "true", "yes")
        if not signed_out and (
            current_user() or _load_user_from_cookie() or _admin_break_glass_user()
        ):
            user = current_user() or _load_user_from_cookie() or _admin_break_glass_user()
            if user:
                resp = make_response(redirect(next_path))
                return _apply_no_store(_set_user_cookie(resp, int(user["id"])))
        if request.method == "POST":
            login = (request.form.get("login") or "").strip()
            password = request.form.get("password") or ""
            # Break-glass: single field API key posted as password with empty login
            if not login and password and configured_api_key() and hmac.compare_digest(
                password, configured_api_key()
            ):
                admin = _admin_break_glass_user()
                if admin:
                    resp = make_response(redirect(next_path))
                    return _apply_no_store(_set_user_cookie(resp, int(admin["id"])))
            try:
                from database.connection import SQLServerConnection
                from database.app_user_repo import AppUserRepo
                from lifecycle.bootstrap_admin import ensure_multi_user_bootstrap
                db = SQLServerConnection()
                db.connect()
                try:
                    ensure_multi_user_bootstrap(db)
                    user = AppUserRepo(db).authenticate(login, password)
                    if user:
                        resp = make_response(redirect(next_path))
                        return _apply_no_store(_set_user_cookie(resp, int(user["id"])))
                finally:
                    db.close()
            except Exception:
                logger.exception("login failed")
            html = _login_page_html(
                error="Incorrect username or password.", next_path=next_path,
            )
            return _apply_no_store(make_response(html, 401))
        html = _login_page_html(next_path=next_path, signed_out=signed_out)
        return _apply_no_store(make_response(html))

    @app.route(LOGOUT_PATH, methods=["GET", "POST"])
    def dashboard_logout():  # noqa: ANN001
        """Clear app login only — never touches Zerodha / tick feed."""
        resp = make_response(redirect(f"{LOGIN_PATH}?signed_out=1", code=303))
        _clear_auth_cookies(resp)
        _apply_no_store(resp)
        # Ask supporting browsers to drop origin cookies/storage for this site.
        resp.headers["Clear-Site-Data"] = '"cookies", "storage"'
        return resp

    @app.after_request
    def _no_store_authenticated_html(response):  # noqa: ANN001
        # Prevent BFCache / back-button from resurrecting a signed-in dashboard.
        try:
            ct = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in ct and not _is_public_path(request.path or "/"):
                _apply_no_store(response)
        except Exception:
            pass
        return response

    @app.before_request
    def _bind_app_user():  # noqa: ANN001
        g.app_user = None
        user = _load_user_from_cookie()
        if user is None:
            user = _admin_break_glass_user()
        g.app_user = user

        path = request.path or "/"
        if _is_public_path(path):
            return None
        if not auth_required():
            return None
        if g.app_user:
            return None
        if path.startswith("/api/") or request.method != "GET":
            return jsonify({
                "error": "Unauthorized",
                "hint": f"POST login/password to {LOGIN_PATH}",
            }), 401
        return redirect(f"{LOGIN_PATH}?next={quote(path, safe='/')}")
