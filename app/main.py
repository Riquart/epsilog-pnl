"""FastAPI app: API routes + serves the single-page dashboard."""
from __future__ import annotations

import io
import os
import time
from typing import Dict, Optional

import pyotp
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .auth import (
    APP_PASSWORD,
    AuthMiddleware,
    COOKIE_NAME,
    PRE_COOKIE_NAME,
    SecurityHeadersMiddleware,
    auth_enabled,
    clear_fails,
    client_ip,
    current_user,
    hash_password,
    is_locked,
    pre_auth_email,
    record_fail,
    set_session_cookie,
    verify_password,
)
from .parsing.detail import build_drill, parse_detail
from .parsing.mapping import PROVISIONAL_BANNER, Mapper
from .parsing.pnl import STRUCT, get_value, parse_pnl

# Poste labels a GL account can be assigned to (everything but section headers).
POSTE_LABELS = [lbl for lbl, _lvl, kind in STRUCT if kind != "header"]

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Recovery escape hatch: set RESET_2FA=1 to wipe the current user's 2FA on startup.
if os.environ.get("RESET_2FA") in ("1", "true", "True"):
    store.save_auth({"totp_secret": None, "enabled": False})
    email = os.environ.get("ADMIN_EMAIL", "admin").strip().lower()
    if store.get_user(email):
        store.upsert_user(email, {"totp_secret": None, "totp_enabled": False})

# Bootstrap the first admin from APP_PASSWORD so access is never interrupted.
def _bootstrap_admin():
    if not APP_PASSWORD or store.get_users():
        return
    email = os.environ.get("ADMIN_EMAIL", "admin").strip().lower()
    store.upsert_user(email, {
        "pw_hash": hash_password(APP_PASSWORD),
        "role": "admin",
        "totp_secret": None,
        "totp_enabled": False,
        "disabled": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    store.audit(email, "bootstrap_admin", "compte admin initial créé depuis APP_PASSWORD")


_bootstrap_admin()

app = FastAPI(title="EPSILOG P&L Dashboard")
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    i = svg.find("<svg")  # drop any <?xml …?> prolog for safe inline insertion
    return svg[i:] if i >= 0 else svg


# -------------------------------------------------- authorization ---

def require_user(request: Request) -> str:
    """Dependency: any authenticated user (email), or 'dev' when auth is disabled."""
    if not auth_enabled():
        return "dev"
    email = current_user(request)
    if not email:
        raise HTTPException(status_code=401, detail="Authentification requise")
    return email


def require_admin(request: Request) -> str:
    """Dependency: user must have the admin role (open in dev/no-auth mode)."""
    if not auth_enabled():
        return "dev"
    email = current_user(request)
    if not email:
        raise HTTPException(status_code=401, detail="Authentification requise")
    user = store.get_user(email)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs")
    return email


# ----------------------------------------------------- snapshot assembly ---

def _merge_accounts(*dicts: Dict[str, dict]) -> Dict[str, dict]:
    merged: Dict[str, dict] = {}
    for d in dicts:
        for acc, info in d.items():
            bucket = merged.setdefault(acc, {"sum": 0.0, "lines": []})
            bucket["sum"] += info["sum"]
            bucket["lines"].extend(info["lines"])
    return merged


def assemble_snapshot(
    pnl_bytes: bytes,
    cost_bytes: Optional[bytes] = None,
    cogs_bytes: Optional[bytes] = None,
) -> dict:
    """Parse the export and store the RAW GL accounts. The per-poste drill-down is
    computed at serve-time from the current mapping (so mapping edits apply live)."""
    snap = parse_pnl(pnl_bytes)

    accounts: Dict[str, dict] = {}
    if cost_bytes:
        accounts = _merge_accounts(accounts, parse_detail(cost_bytes))
    if cogs_bytes:
        accounts = _merge_accounts(accounts, parse_detail(cogs_bytes))

    snap["accounts"] = {
        a: {"sum": round(v["sum"], 2), "lines": v["lines"]} for a, v in accounts.items()
    }
    snap["_uploaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return snap


def _resolve_drill(snapshot: dict):
    """Build the drill-down from stored raw accounts using the CURRENT mapping.
    Falls back to a legacy frozen ``drill`` for snapshots saved before this change."""
    accounts = snapshot.get("accounts")
    if not accounts:
        return snapshot.get("drill", {}), snapshot.get("unmapped_accounts", [])

    mapper = Mapper(rules=store.get_mapping_rules())

    def monthly_actual_for(poste: str):
        return get_value(snapshot["lines"], poste, "monthly", "actual")

    return build_drill(accounts, mapper, monthly_actual_for, note=PROVISIONAL_BANNER)


def _prepare_snapshot(snapshot: dict) -> dict:
    """Resolve the drill from the current mapping and inject shared account labels."""
    drill, unmapped = _resolve_drill(snapshot)
    labels = store.get_labels()
    for poste in drill.values():
        for acc in poste.get("accounts", []):
            acc["label"] = labels.get(acc["account"], "")
    snapshot = dict(snapshot)
    snapshot.pop("accounts", None)  # keep the response lean; lines live inside drill
    snapshot["drill"] = drill
    snapshot["unmapped_accounts"] = unmapped
    return snapshot


# ------------------------------------------------------------- pages ---

@app.get("/health")
def health():
    return {"status": "ok", "periods": len(store.list_periods())}


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ------------------------------------------------------------- auth API ---

@app.post("/api/login")
async def api_login(request: Request, email: str = Form(""), password: str = Form("")):
    ip = client_ip(request)
    if is_locked(ip):
        raise HTTPException(status_code=429, detail="Trop de tentatives. Réessayez dans quelques minutes.")
    email = (email or "").strip().lower()
    user = store.get_user(email)
    ok = bool(user) and not user.get("disabled") and verify_password(password, user.get("pw_hash", ""))
    if not ok:
        record_fail(ip)
        store.audit(email or "?", "login_failed", ip)
        raise HTTPException(status_code=401, detail="Identifiant ou mot de passe incorrect")
    if user.get("totp_enabled") and user.get("totp_secret"):
        resp = JSONResponse({"ok": True, "step": "2fa"})
        set_session_cookie(resp, "pre", email)  # password verified, awaiting code
        return resp
    clear_fails(ip)
    store.audit(email, "login", ip)
    resp = JSONResponse({"ok": True, "step": "done"})
    set_session_cookie(resp, "session", email)
    return resp


@app.post("/api/2fa/verify")
async def api_2fa_verify(request: Request, code: str = Form("")):
    ip = client_ip(request)
    if is_locked(ip):
        raise HTTPException(status_code=429, detail="Trop de tentatives. Réessayez dans quelques minutes.")
    email = pre_auth_email(request)
    if not email:
        raise HTTPException(status_code=401, detail="Session expirée, reconnectez-vous.")
    user = store.get_user(email)
    secret = (user or {}).get("totp_secret")
    if not secret or not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        record_fail(ip)
        store.audit(email, "login_failed", "code 2FA")
        raise HTTPException(status_code=401, detail="Code incorrect")
    clear_fails(ip)
    store.audit(email, "login", ip)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, "session", email)
    resp.delete_cookie(PRE_COOKIE_NAME)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    resp.delete_cookie(PRE_COOKIE_NAME)
    return resp


@app.get("/api/me")
def api_me(request: Request):
    if not auth_enabled():
        return {"email": None, "role": "admin", "auth": False}
    email = current_user(request)
    user = store.get_user(email) if email else None
    return {"email": email, "role": (user or {}).get("role", "viewer"), "auth": True}


# --- per-user 2FA (operates on the current user) ---

@app.get("/api/2fa/status")
def api_2fa_status(email: str = Depends(require_user)):
    user = store.get_user(email) or {}
    return {"enabled": bool(user.get("totp_enabled")), "configured": bool(user.get("totp_secret"))}


@app.post("/api/2fa/setup")
def api_2fa_setup(email: str = Depends(require_user)):
    secret = pyotp.random_base32()
    store.upsert_user(email, {"totp_secret": secret, "totp_enabled": False})
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="EPSILOG P&L")
    return {"secret": secret, "otpauth": uri, "qr_svg": _qr_svg(uri)}


@app.post("/api/2fa/activate")
async def api_2fa_activate(code: str = Form(""), email: str = Depends(require_user)):
    user = store.get_user(email) or {}
    secret = user.get("totp_secret")
    if not secret:
        raise HTTPException(status_code=400, detail="Lancez d'abord la configuration.")
    if not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        raise HTTPException(status_code=401, detail="Code incorrect — vérifiez l'heure de votre téléphone.")
    store.upsert_user(email, {"totp_enabled": True})
    store.audit(email, "2fa_enable", "")
    return {"ok": True, "enabled": True}


@app.post("/api/2fa/disable")
def api_2fa_disable(email: str = Depends(require_user)):
    store.upsert_user(email, {"totp_secret": None, "totp_enabled": False})
    store.audit(email, "2fa_disable", "")
    return {"ok": True, "enabled": False}


# --- user management (admin only) ---

def _user_public(email: str, u: dict) -> dict:
    return {
        "email": email,
        "role": u.get("role", "viewer"),
        "disabled": bool(u.get("disabled")),
        "totp_enabled": bool(u.get("totp_enabled")),
        "created_at": u.get("created_at", ""),
    }


@app.get("/api/users")
def api_users(admin: str = Depends(require_admin)):
    return {"users": [_user_public(e, u) for e, u in sorted(store.get_users().items())]}


@app.post("/api/users")
async def api_user_create(request: Request, admin: str = Depends(require_admin)):
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    role = "admin" if body.get("role") == "admin" else "viewer"
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email invalide")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Mot de passe : 8 caractères minimum")
    if store.get_user(email):
        raise HTTPException(status_code=409, detail="Cet utilisateur existe déjà")
    store.upsert_user(email, {
        "pw_hash": hash_password(password), "role": role,
        "totp_secret": None, "totp_enabled": False, "disabled": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    store.audit(admin, "user_create", f"{email} ({role})")
    return {"ok": True}


@app.put("/api/users/{email}")
async def api_user_update(email: str, request: Request, admin: str = Depends(require_admin)):
    email = email.strip().lower()
    user = store.get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    body = await request.json()
    patch: Dict[str, object] = {}
    if "role" in body:
        patch["role"] = "admin" if body["role"] == "admin" else "viewer"
    if "disabled" in body:
        patch["disabled"] = bool(body["disabled"])
    if body.get("password"):
        if len(str(body["password"])) < 8:
            raise HTTPException(status_code=400, detail="Mot de passe : 8 caractères minimum")
        patch["pw_hash"] = hash_password(str(body["password"]))
    if body.get("reset_2fa"):
        patch["totp_secret"] = None
        patch["totp_enabled"] = False
    # Don't let the last admin lose admin / be disabled.
    admins = [e for e, u in store.get_users().items() if u.get("role") == "admin" and not u.get("disabled")]
    if email in admins and (patch.get("role") == "viewer" or patch.get("disabled")) and len(admins) <= 1:
        raise HTTPException(status_code=400, detail="Impossible : c'est le dernier administrateur actif")
    store.upsert_user(email, patch)
    store.audit(admin, "user_update", f"{email}: {list(patch.keys())}")
    return {"ok": True}


@app.delete("/api/users/{email}")
def api_user_delete(email: str, admin: str = Depends(require_admin)):
    email = email.strip().lower()
    if email == admin:
        raise HTTPException(status_code=400, detail="Impossible de supprimer votre propre compte")
    if not store.get_user(email):
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    store.delete_user(email)
    store.audit(admin, "user_delete", email)
    return {"ok": True}


@app.get("/api/audit")
def api_audit(admin: str = Depends(require_admin), limit: int = 200):
    return {"events": store.read_audit(limit)}


# ------------------------------------------------------------- data API ---

@app.get("/api/periods")
def api_periods():
    return {"periods": store.list_periods(), "latest": store.latest_period()}


@app.get("/api/snapshot")
def api_snapshot(period: Optional[str] = None):
    period = period or store.latest_period()
    if not period:
        raise HTTPException(status_code=404, detail="Aucune période importée")
    snap = store.get_snapshot(period)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"Période {period} introuvable")
    return _prepare_snapshot(snap)


@app.post("/api/upload")
async def api_upload(
    pnl: UploadFile = File(...),
    cost: Optional[UploadFile] = File(None),
    cogs: Optional[UploadFile] = File(None),
    admin: str = Depends(require_admin),
):
    pnl_bytes = await pnl.read()
    cost_bytes = await cost.read() if cost is not None else None
    cogs_bytes = await cogs.read() if cogs is not None else None
    try:
        snap = assemble_snapshot(pnl_bytes, cost_bytes, cogs_bytes)
    except Exception as e:  # noqa: BLE001 - surface parsing errors to the user
        raise HTTPException(status_code=400, detail=f"Échec du parsing : {e}")
    entry = store.save_snapshot(snap)
    store.audit(admin, "upload", entry["period"])
    _drill, unmapped = _resolve_drill(snap)
    return {
        "ok": True,
        "period": entry["period"],
        "label": entry["label"],
        "unmapped_accounts": len(unmapped),
        "has_drill": bool(snap.get("accounts")),
    }


@app.delete("/api/snapshot")
def api_delete(period: str, admin: str = Depends(require_admin)):
    if not store.delete_snapshot(period):
        raise HTTPException(status_code=404, detail=f"Période {period} introuvable")
    store.audit(admin, "delete_period", period)
    return {"ok": True}


@app.get("/api/labels")
def api_get_labels():
    return store.get_labels()


@app.put("/api/labels")
async def api_put_labels(request: Request, admin: str = Depends(require_admin)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Format attendu : objet {compte: libellé}")
    return store.set_labels(body)


@app.put("/api/labels/{account}")
async def api_put_one_label(account: str, request: Request, admin: str = Depends(require_admin)):
    body = await request.json()
    label = body.get("label", "") if isinstance(body, dict) else ""
    return store.update_label(account, label)


@app.get("/api/unmapped")
def api_unmapped(period: Optional[str] = None):
    """Diagnostic: GL accounts that did not match any mapping rule."""
    period = period or store.latest_period()
    snap = store.get_snapshot(period) if period else None
    if snap is None:
        return {"period": period, "unmapped_accounts": []}
    _drill, unmapped = _resolve_drill(snap)
    return {"period": period, "unmapped_accounts": unmapped}


# ---------------------------------------------------------- mapping API ---

def _rules_payload(rules):
    return [{"prefix": p, "poste": poste} for p, poste in rules]


@app.get("/api/mapping")
def api_get_mapping():
    """Current mapping rules + the list of postes an account can be assigned to."""
    rules = sorted(store.get_mapping_rules(), key=lambda r: (r[1], r[0]))
    return {"rules": _rules_payload(rules), "postes": POSTE_LABELS}


@app.put("/api/mapping")
async def api_put_mapping(request: Request, admin: str = Depends(require_admin)):
    """Replace the whole mapping. Body: {"rules": [{prefix, poste}, ...]} or a bare list."""
    body = await request.json()
    rules = body.get("rules", []) if isinstance(body, dict) else body
    saved = store.set_mapping_rules(rules)
    store.audit(admin, "mapping_replace", f"{len(saved)} règles")
    return {"ok": True, "count": len(saved)}


@app.post("/api/mapping/rule")
async def api_upsert_rule(request: Request, admin: str = Depends(require_admin)):
    """Add/update a single rule. Body: {prefix, poste}. Empty poste deletes it."""
    body = await request.json()
    prefix = str(body.get("prefix", "")).strip()
    poste = str(body.get("poste", "")).strip()
    if not prefix:
        raise HTTPException(status_code=400, detail="Préfixe/compte requis")
    saved = store.upsert_mapping_rule(prefix, poste)
    store.audit(admin, "mapping_rule", f"{prefix} -> {poste or '(supprimé)'}")
    return {"ok": True, "count": len(saved)}


@app.post("/api/mapping/reset")
def api_reset_mapping(admin: str = Depends(require_admin)):
    """Discard in-app edits and re-seed from the CSV default."""
    saved = store.reset_mapping()
    store.audit(admin, "mapping_reset", f"{len(saved)} règles")
    return {"ok": True, "count": len(saved)}


# ------------------------------------------------------------- notes API ---

@app.get("/api/notes")
def api_get_notes(period: Optional[str] = None):
    period = period or store.latest_period()
    return {"period": period, "notes": store.get_notes(period) if period else {}}


@app.put("/api/notes")
async def api_put_note(request: Request, admin: str = Depends(require_admin)):
    """Set/clear a note on a P&L line. Body: {period, label, text}."""
    body = await request.json()
    period = str(body.get("period", "")).strip() or store.latest_period()
    label = str(body.get("label", "")).strip()
    text = body.get("text", "")
    if not period or not label:
        raise HTTPException(status_code=400, detail="Période et ligne requises")
    notes = store.set_note(period, label, text)
    store.audit(admin, "note", f"{period} / {label}")
    return {"ok": True, "period": period, "notes": notes}
