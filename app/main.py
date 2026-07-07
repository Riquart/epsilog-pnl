"""FastAPI app: API routes + serves the single-page dashboard."""
from __future__ import annotations

import os
import time
from typing import Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .auth import (
    AuthMiddleware,
    COOKIE_NAME,
    SESSION_TTL,
    check_password,
    make_token,
)
from .parsing.detail import build_drill, parse_detail
from .parsing.mapping import PROVISIONAL_BANNER, Mapper
from .parsing.pnl import STRUCT, get_value, parse_pnl

# Poste labels a GL account can be assigned to (everything but section headers).
POSTE_LABELS = [lbl for lbl, _lvl, kind in STRUCT if kind != "header"]

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="EPSILOG P&L Dashboard")
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
async def api_login(password: str = Form("")):
    if not check_password(password):
        raise HTTPException(status_code=401, detail="Mot de passe incorrect")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME, make_token(), max_age=SESSION_TTL,
        httponly=True, samesite="lax", secure=False,
    )
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


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
):
    pnl_bytes = await pnl.read()
    cost_bytes = await cost.read() if cost is not None else None
    cogs_bytes = await cogs.read() if cogs is not None else None
    try:
        snap = assemble_snapshot(pnl_bytes, cost_bytes, cogs_bytes)
    except Exception as e:  # noqa: BLE001 - surface parsing errors to the user
        raise HTTPException(status_code=400, detail=f"Échec du parsing : {e}")
    entry = store.save_snapshot(snap)
    _drill, unmapped = _resolve_drill(snap)
    return {
        "ok": True,
        "period": entry["period"],
        "label": entry["label"],
        "unmapped_accounts": len(unmapped),
        "has_drill": bool(snap.get("accounts")),
    }


@app.delete("/api/snapshot")
def api_delete(period: str):
    if not store.delete_snapshot(period):
        raise HTTPException(status_code=404, detail=f"Période {period} introuvable")
    return {"ok": True}


@app.get("/api/labels")
def api_get_labels():
    return store.get_labels()


@app.put("/api/labels")
async def api_put_labels(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Format attendu : objet {compte: libellé}")
    return store.set_labels(body)


@app.put("/api/labels/{account}")
async def api_put_one_label(account: str, request: Request):
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
async def api_put_mapping(request: Request):
    """Replace the whole mapping. Body: {"rules": [{prefix, poste}, ...]} or a bare list."""
    body = await request.json()
    rules = body.get("rules", []) if isinstance(body, dict) else body
    saved = store.set_mapping_rules(rules)
    return {"ok": True, "count": len(saved)}


@app.post("/api/mapping/rule")
async def api_upsert_rule(request: Request):
    """Add/update a single rule. Body: {prefix, poste}. Empty poste deletes it."""
    body = await request.json()
    prefix = str(body.get("prefix", "")).strip()
    poste = str(body.get("poste", "")).strip()
    if not prefix:
        raise HTTPException(status_code=400, detail="Préfixe/compte requis")
    saved = store.upsert_mapping_rule(prefix, poste)
    return {"ok": True, "count": len(saved)}


@app.post("/api/mapping/reset")
def api_reset_mapping():
    """Discard in-app edits and re-seed from the CSV default."""
    saved = store.reset_mapping()
    return {"ok": True, "count": len(saved)}


# ------------------------------------------------------------- notes API ---

@app.get("/api/notes")
def api_get_notes(period: Optional[str] = None):
    period = period or store.latest_period()
    return {"period": period, "notes": store.get_notes(period) if period else {}}


@app.put("/api/notes")
async def api_put_note(request: Request):
    """Set/clear a note on a P&L line. Body: {period, label, text}."""
    body = await request.json()
    period = str(body.get("period", "")).strip() or store.latest_period()
    label = str(body.get("label", "")).strip()
    text = body.get("text", "")
    if not period or not label:
        raise HTTPException(status_code=400, detail="Période et ligne requises")
    notes = store.set_note(period, label, text)
    return {"ok": True, "period": period, "notes": notes}
