"""Shared-password gate with a signed session cookie.

No anonymous access: every request except the login page, the static login assets
and ``/health`` must carry a valid signed cookie. Set ``APP_PASSWORD`` and
``SECRET_KEY`` in the environment.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
COOKIE_NAME = "epsilog_session"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days

# Paths reachable without a session.
_OPEN_PATHS = {"/health", "/login", "/api/login", "/favicon.ico"}
_OPEN_PREFIXES = ("/static/login",)


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def valid_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    issued, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(issued), sig):
        return False
    try:
        return (time.time() - int(issued)) < SESSION_TTL
    except ValueError:
        return False


def check_password(candidate: str) -> bool:
    # If no password is configured, the app is open (local dev convenience).
    if not APP_PASSWORD:
        return True
    return hmac.compare_digest(candidate or "", APP_PASSWORD)


def is_authenticated(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    return valid_token(request.cookies.get(COOKIE_NAME, ""))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            not APP_PASSWORD
            or path in _OPEN_PATHS
            or any(path.startswith(p) for p in _OPEN_PREFIXES)
        ):
            return await call_next(request)
        if is_authenticated(request):
            return await call_next(request)
        # Browsers asking for HTML -> redirect to the login page; API -> 401.
        accept = request.headers.get("accept", "")
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentification requise"}, status_code=401)
        if "text/html" in accept:
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"detail": "Authentification requise"}, status_code=401)
