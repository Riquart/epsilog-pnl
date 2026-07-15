"""Snapshot & label persistence in ``DATA_DIR``.

Each parsed period is stored as ``<period>.json`` (e.g. ``2026-05.json``).
``index.json`` lists known periods (newest first). ``account_labels.json`` holds the
shared, server-side business labels for GL accounts (survive re-imports).

On Railway, mount a Volume on ``DATA_DIR`` so these files persist.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
INDEX_FILE = "index.json"
LABELS_FILE = "account_labels.json"
MAPPING_FILE = "mapping.json"
NOTES_FILE = "notes.json"
AUTH_FILE = "auth.json"
USERS_FILE = "users.json"
AUDIT_FILE = "audit.jsonl"

_lock = threading.Lock()


def _ensure_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _read_json(name: str, default):
    p = _path(name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(name: str, data) -> None:
    _ensure_dir()
    tmp = _path(name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path(name))


# ---------------------------------------------------------------- snapshots ---

def save_snapshot(snapshot: dict) -> dict:
    """Persist a snapshot and update the index. Returns the period info entry."""
    period = snapshot["period"]
    with _lock:
        _write_json(f"{period}.json", snapshot)
        index = _read_json(INDEX_FILE, [])
        index = [e for e in index if e.get("period") != period]
        from .parsing.pnl import _derive_period  # late import to avoid cycle

        _, label = _derive_period(snapshot.get("metadata", {}))
        entry = {
            "period": period,
            "label": label,
            "entity": snapshot.get("metadata", {}).get("entity", ""),
            "uploaded_at": snapshot.get("_uploaded_at", ""),
        }
        index.append(entry)
        index.sort(key=lambda e: e.get("period", ""), reverse=True)
        _write_json(INDEX_FILE, index)
        return entry


def list_periods() -> List[dict]:
    return _read_json(INDEX_FILE, [])


def get_snapshot(period: str) -> Optional[dict]:
    return _read_json(f"{period}.json", None)


def latest_period() -> Optional[str]:
    index = list_periods()
    return index[0]["period"] if index else None


def delete_snapshot(period: str) -> bool:
    with _lock:
        p = _path(f"{period}.json")
        existed = os.path.exists(p)
        if existed:
            os.remove(p)
        index = [e for e in list_periods() if e.get("period") != period]
        _write_json(INDEX_FILE, index)
        return existed


# ------------------------------------------------------------------- labels ---

def get_labels() -> Dict[str, str]:
    return _read_json(LABELS_FILE, {})


def set_labels(labels: Dict[str, str]) -> Dict[str, str]:
    with _lock:
        clean = {str(k): str(v).strip() for k, v in labels.items() if str(v).strip()}
        _write_json(LABELS_FILE, clean)
        return clean


def update_label(account: str, label: str) -> Dict[str, str]:
    with _lock:
        labels = get_labels()
        label = (label or "").strip()
        if label:
            labels[account] = label
        else:
            labels.pop(account, None)
        _write_json(LABELS_FILE, labels)
        return labels


# ------------------------------------------------------------------ mapping ---

def get_mapping_rules() -> List[List[str]]:
    """Return the effective mapping as ``[[prefix, poste], ...]``.

    Seeded once from ``mapping/account_to_poste.csv`` on first use, then editable
    and persisted in ``DATA_DIR/mapping.json`` (source of truth thereafter).
    """
    data = _read_json(MAPPING_FILE, None)
    if data is None:
        from .parsing.mapping import load_mapping  # late import to avoid cycle

        data = [[p, poste] for p, poste in load_mapping()]
        _write_json(MAPPING_FILE, data)
    return data


def set_mapping_rules(rules) -> List[List[str]]:
    """Replace the mapping. Accepts a list of ``{prefix,poste}`` or ``[prefix,poste]``."""
    with _lock:
        clean: List[List[str]] = []
        seen = set()
        for r in rules or []:
            if isinstance(r, dict):
                prefix = str(r.get("prefix", "")).strip()
                poste = str(r.get("poste", "")).strip()
            else:
                prefix = str(r[0]).strip()
                poste = str(r[1]).strip()
            if prefix and poste and prefix not in seen:
                seen.add(prefix)
                clean.append([prefix, poste])
        _write_json(MAPPING_FILE, clean)
        return clean


def upsert_mapping_rule(prefix: str, poste: str) -> List[List[str]]:
    """Add or update a single ``prefix -> poste`` rule."""
    prefix = (prefix or "").strip()
    poste = (poste or "").strip()
    rules = [r for r in get_mapping_rules() if r[0] != prefix]
    if poste:  # empty poste = delete the rule
        rules.append([prefix, poste])
    return set_mapping_rules(rules)


def reset_mapping() -> List[List[str]]:
    """Discard in-app edits and re-seed from the CSV default."""
    with _lock:
        p = _path(MAPPING_FILE)
        if os.path.exists(p):
            os.remove(p)
    return get_mapping_rules()


# -------------------------------------------------------------------- notes ---

def get_notes(period: Optional[str] = None):
    """All notes ``{period: {label: text}}`` or, if ``period`` is given, that
    period's ``{label: text}``."""
    data = _read_json(NOTES_FILE, {})
    if period is None:
        return data
    return data.get(period, {})


def set_note(period: str, label: str, text: str) -> Dict[str, str]:
    """Set (or clear, if ``text`` is empty) a note on one P&L line for a period."""
    with _lock:
        data = _read_json(NOTES_FILE, {})
        per = data.get(period, {})
        text = (text or "").strip()
        if text:
            per[label] = text
        else:
            per.pop(label, None)
        if per:
            data[period] = per
        else:
            data.pop(period, None)
        _write_json(NOTES_FILE, data)
        return per


# --------------------------------------------------------- 2FA / auth ---

def get_auth() -> dict:
    """Legacy shared-TOTP config (kept for single-user mode)."""
    return _read_json(AUTH_FILE, {"totp_secret": None, "enabled": False})


def save_auth(data: dict) -> dict:
    with _lock:
        _write_json(AUTH_FILE, data)
        return data


# ------------------------------------------------------------- users ---

def get_users() -> Dict[str, dict]:
    return _read_json(USERS_FILE, {})


def get_user(email: str) -> Optional[dict]:
    return get_users().get((email or "").strip().lower())


def upsert_user(email: str, data: dict) -> dict:
    email = (email or "").strip().lower()
    with _lock:
        users = _read_json(USERS_FILE, {})
        users[email] = {**users.get(email, {}), **data}
        _write_json(USERS_FILE, users)
        return users[email]


def delete_user(email: str) -> None:
    email = (email or "").strip().lower()
    with _lock:
        users = _read_json(USERS_FILE, {})
        users.pop(email, None)
        _write_json(USERS_FILE, users)


# ------------------------------------------------------------- audit ---

def audit(user: str, action: str, detail: str = "") -> None:
    _ensure_dir()
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user": user or "?",
        "action": action,
        "detail": detail,
    }
    with _lock:
        with open(_path(AUDIT_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_audit(limit: int = 200) -> List[dict]:
    p = _path(AUDIT_FILE)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out.reverse()  # newest first
    return out
