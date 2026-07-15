"""Authentication & security hardening.

- Shared password (``APP_PASSWORD``) with a signed, scoped session cookie.
- Optional TOTP 2FA (enrolled from the app; off until activated).
- Per-IP brute-force lockout on the login / 2FA endpoints.
- Security headers (CSP, HSTS, anti-clickjacking, nosniff) on every response.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional

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


# ------------------------------------------------- password hashing ---

def hash_password(pw: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", (pw or "").encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ------------------------------------------------------------- tokens ---
# Token format: "<scope>.<email_b64>.<issued>.<sig>", sig over the first 3 parts.

def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode()


def make_token(scope: str = "session", email: str = "") -> str:
    issued = str(int(time.time()))
    body = f"{scope}.{_b64(email)}.{issued}"
    return f"{body}.{_sign(body)}"


def token_email(token: str, scope: str = "session", ttl: int = SESSION_TTL) -> Optional[str]:
    """Return the email carried by a valid token for ``scope``, else None."""
    if not token or token.count(".") != 3:
        return None
    tscope, email_b64, issued, sig = token.split(".")
    if tscope != scope:
        return None
    if not hmac.compare_digest(_sign(f"{tscope}.{email_b64}.{issued}"), sig):
        return None
    try:
        if (time.time() - int(issued)) >= ttl:
            return None
        return _unb64(email_b64)
    except (ValueError, Exception):
        return None


# ------------------------------------------------------------- checks ---

def auth_enabled() -> bool:
    """Auth is enforced if a shared password is set OR any user account exists."""
    if APP_PASSWORD:
        return True
    from . import store  # lazy to avoid import cycle
    return bool(store.get_users())


def current_user(request: Request) -> Optional[str]:
    """Email of the authenticated user (session cookie), or None."""
    return token_email(request.cookies.get(COOKIE_NAME, ""), "session", SESSION_TTL)


def pre_auth_email(request: Request) -> Optional[str]:
    """Email of a password-verified user awaiting 2FA (pre cookie), or None."""
    return token_email(request.cookies.get(PRE_COOKIE_NAME, ""), "pre", PRE_TTL)


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    return current_user(request) is not None


def set_session_cookie(resp, scope: str = "session", email: str = ""):
    name = COOKIE_NAME if scope == "session" else PRE_COOKIE_NAME
    max_age = SESSION_TTL if scope == "session" else PRE_TTL
    resp.set_cookie(
        name, make_token(scope, email), max_age=max_age,
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
            not auth_enabled()
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
