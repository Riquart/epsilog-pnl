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
from .parsing.pnl import get_value, parse_pnl

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
    snap = parse_pnl(pnl_bytes)

    accounts: Dict[str, dict] = {}
    if cost_bytes:
        accounts = _merge_accounts(accounts, parse_detail(cost_bytes))
    if cogs_bytes:
        accounts = _merge_accounts(accounts, parse_detail(cogs_bytes))

    drill: Dict[str, dict] = {}
    unmapped = []
    if accounts:
        mapper = Mapper()

        def monthly_actual_for(poste: str):
            return get_value(snap["lines"], poste, "monthly", "actual")

        drill, unmapped = build_drill(
            accounts, mapper, monthly_actual_for, note=PROVISIONAL_BANNER
        )

    snap["drill"] = drill
    snap["unmapped_accounts"] = unmapped
    snap["_uploaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return snap


def _with_labels(snapshot: dict) -> dict:
    """Inject the shared server-side account labels into a snapshot's drill."""
    labels = store.get_labels()
    drill = snapshot.get("drill", {})
    for poste in drill.values():
        for acc in poste.get("accounts", []):
            acc["label"] = labels.get(acc["account"], "")
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
    return _with_labels(snap)


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
    return {
        "ok": True,
        "period": entry["period"],
        "label": entry["label"],
        "unmapped_accounts": len(snap.get("unmapped_accounts", [])),
        "has_drill": bool(snap.get("drill")),
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
    return {"period": period, "unmapped_accounts": snap.get("unmapped_accounts", [])}
