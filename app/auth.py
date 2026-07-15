"""Authentication & security hardening.

- Shared password (``APP_PASSWORD``) with a signed, scoped session cookie.
- Optional TOTP 2FA (enrolled from the app; off until activated).
- Per-IP brute-force lockout on the login / 2FA endpoints.
- Security headers (CSP, HSTS, anti-clickjacking, nosniff) on every response.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import defaultdict
from typing import Dict, List

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
COOKIE_NAME = "epsilog_session"
PRE_COOKIE_NAME = "epsilog_pre"          # password ok, awaiting 2FA
SESSION_TTL = 60 * 60 * 24 * 7           # 7 days
PRE_TTL = 60 * 10                        # 10 minutes to enter the 2FA code
# Secure cookies (HTTPS only). Default on; set COOKIE_SECURE=0 for local http.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") not in ("0", "false", "False", "")

# Fail closed: a weak/default SECRET_KEY makes session cookies forgeable.
if APP_PASSWORD and (not SECRET_KEY or SECRET_KEY == "dev-insecure-change-me"):
    raise RuntimeError(
        "SECRET_KEY must be set to a strong random value in production "
        "(APP_PASSWORD is configured but SECRET_KEY is missing/default)."
    )

# Brute-force lockout (in-memory; single worker).
LOCKOUT_MAX = 6
LOCKOUT_WINDOW = 600  # 10 minutes
_fails: Dict[str, List[float]] = defaultdict(list)

_OPEN_PATHS = {"/health", "/login", "/api/login", "/api/2fa/verify", "/favicon.ico"}
_OPEN_PREFIXES = ("/static/login",)


# ------------------------------------------------------------- tokens ---

def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(scope: str = "session") -> str:
    issued = str(int(time.time()))
    body = f"{scope}.{issued}"
    return f"{body}.{_sign(body)}"


def valid_token(token: str, scope: str = "session", ttl: int = SESSION_TTL) -> bool:
    if not token or token.count(".") != 2:
        return False
    tscope, issued, sig = token.split(".")
    if tscope != scope:
        return False
    if not hmac.compare_digest(_sign(f"{tscope}.{issued}"), sig):
        return False
    try:
        return (time.time() - int(issued)) < ttl
    except ValueError:
        return False


# ------------------------------------------------------------- checks ---

def check_password(candidate: str) -> bool:
    if not APP_PASSWORD:
        return True  # open (local dev convenience)
    return hmac.compare_digest(candidate or "", APP_PASSWORD)


def is_authenticated(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    return valid_token(request.cookies.get(COOKIE_NAME, ""), "session", SESSION_TTL)


def has_pre_auth(request: Request) -> bool:
    return valid_token(request.cookies.get(PRE_COOKIE_NAME, ""), "pre", PRE_TTL)


def set_session_cookie(resp, scope: str = "session"):
    name = COOKIE_NAME if scope == "session" else PRE_COOKIE_NAME
    max_age = SESSION_TTL if scope == "session" else PRE_TTL
    resp.set_cookie(
        name, make_token(scope), max_age=max_age,
        httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )


# --------------------------------------------------------- brute-force ---

def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def is_locked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < LOCKOUT_WINDOW]
    _fails[ip] = recent
    return len(recent) >= LOCKOUT_MAX


def record_fail(ip: str) -> None:
    _fails[ip].append(time.time())


def clear_fails(ip: str) -> None:
    _fails.pop(ip, None)


# ------------------------------------------------------------ middleware ---

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
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentification requise"}, status_code=401)
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"detail": "Authentification requise"}, status_code=401)


_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = _CSP
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if COOKIE_SECURE:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp
