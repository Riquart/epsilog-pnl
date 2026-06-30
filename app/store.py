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
from typing import Dict, List, Optional

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
INDEX_FILE = "index.json"
LABELS_FILE = "account_labels.json"

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
