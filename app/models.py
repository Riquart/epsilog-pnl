"""Pydantic schemas for the EPSILOG P&L dashboard.

A *Snapshot* is the parsed, structured representation of one monthly export.
It is what gets persisted as JSON in ``DATA_DIR`` and served to the frontend.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


class Metadata(BaseModel):
    report: str = ""
    scenario: str = ""
    period: str = ""
    entity: str = ""
    run_by: str = ""
    currency: str = "EUR"
    sheet: str = ""


class BlockValues(BaseModel):
    """The set of figures for one time block (monthly or cumulated)."""

    actual: Optional[float] = None
    actual_adj: Optional[float] = None
    budget: Optional[float] = None
    py: Optional[float] = None
    fc: Optional[float] = None
    d_bud: Optional[float] = None
    d_py: Optional[float] = None
    d_fc: Optional[float] = None


class LineValues(BaseModel):
    monthly: Dict[str, Optional[float]] = {}
    cumul: Dict[str, Optional[float]] = {}


class PnLLine(BaseModel):
    label: str
    level: int
    kind: str  # header | kpi | sub | line
    values: Optional[LineValues] = None
    polarity: Optional[str] = None  # income | cost


class DrillEntry(BaseModel):
    """One GL account inside a drill-down, with its sample postings."""

    date: str
    account: str
    text: str
    amount: float


class DrillAccount(BaseModel):
    account: str
    label: str = ""
    sum: float
    lines: List[DrillEntry] = []


class DrillPoste(BaseModel):
    note: str = ""
    meta: Dict[str, Optional[float]] = {}
    accounts: List[DrillAccount] = []


class Snapshot(BaseModel):
    """Full parsed export for one period."""

    period: str  # canonical key, e.g. "2026-05"
    metadata: Metadata
    lines: List[PnLLine]
    fte: Dict[str, LineValues] = {}
    drill: Dict[str, DrillPoste] = {}
    unmapped_accounts: List[Dict[str, object]] = []


class PeriodInfo(BaseModel):
    period: str           # "2026-05"
    label: str            # "May 2026"
    entity: str = ""
    uploaded_at: str = ""
