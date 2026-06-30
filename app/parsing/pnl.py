"""Parser of the CGM-group management P&L export (report 212-000).

Verified against the real EPSILOG SAS export of May 2026 — see ``tests/test_parsing.py``.
Identify each P&L line by its (stable) label rather than its row number, since the
sheet name and exact row offsets change from month to month.
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Dict, Optional, Tuple, Union

import numpy as np
import pandas as pd

# ---- column map inside the P&L (0-based offsets within the sheet) ----
# "May - Cumulated" (YTD) block, then "May - Monthly" block.
CUM = dict(actual=4, actual_adj=6, budget=8, py=9, fc=10, d_bud=12, d_py=13, d_fc=14)
MON = dict(actual=16, actual_adj=18, budget=20, py=21, fc=22, d_bud=24, d_py=25, d_fc=26)

# ---- curated P&L skeleton: (label, level, kind) ----
# kind: header / kpi / sub / line ; level: 0 / 1 / 2
STRUCT = [
    ("§Produits", 0, "header"),
    ("One-Time Revenues", 1, "sub"),
    ("Revenue Software license", 2, "line"),
    ("Revenue Hardware", 2, "line"),
    ("Revenue Professional services", 2, "line"),
    ("Revenue Advertising, eDetailing, Data", 2, "line"),
    ("Revenue Other", 2, "line"),
    ("Recurring Revenues", 1, "sub"),
    ("Revenue Software maintenance + hotline", 2, "line"),
    ("Revenue Software as a Service (SAAS)", 2, "line"),
    ("Revenue Other recurring service fees", 2, "line"),
    ("Internal Revenues (Group Companies)", 1, "line"),
    ("Total Revenues", 0, "kpi"),
    ("§Coût des ventes (COGS)", 0, "header"),
    ("One Time COGS", 1, "sub"),
    ("COGS Hardware", 2, "line"),
    ("COGS Professional services", 2, "line"),
    ("Recurring COGS", 1, "sub"),
    ("COGS Other recurring service fees", 2, "line"),
    ("Total COGS", 0, "kpi"),
    ("Gross Profit Total", 0, "kpi"),
    ("§Charges opérationnelles (OPEX)", 0, "header"),
    ("Personnel expenses", 1, "line"),
    ("Outsourcing", 1, "line"),
    ("Contractors", 1, "line"),
    ("Other OPEX", 1, "sub"),
    ("Occupancy", 2, "line"),
    ("ICT", 2, "line"),
    ("Marketing", 2, "line"),
    ("Company Cars", 2, "line"),
    ("Travel", 2, "line"),
    ("Office supplies", 2, "line"),
    ("Law and consultancy fees", 2, "line"),
    ("Insurances", 2, "line"),
    ("Uncollectable debts", 2, "line"),
    ("Other expenses", 2, "line"),
    ("Total Costs", 0, "kpi"),
    ("§Résultat", 0, "header"),
    ("Capitalized in-house services", 1, "line"),
    ("Other income", 1, "line"),
    ("EBITDA after Central IC", 0, "kpi"),
    ("Depreciation", 1, "line"),
    ("Amortization", 1, "line"),
    ("EBIT", 0, "kpi"),
    ("Financial result", 1, "line"),
    ("Tax result", 1, "line"),
    ("Net result", 0, "kpi"),
]

# polarity overrides (lines whose favourable direction differs from their section)
_COST_SECTIONS = {"Coût des ventes (COGS)", "Charges opérationnelles (OPEX)"}
_COST_OVERRIDE = {"Depreciation", "Amortization"}  # costs sitting in the Résultat section
_INCOME_OVERRIDE = {"Gross Profit Total"}          # profit line sitting in the COGS section

_MONTHS = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}

ExcelSource = Union[str, bytes, BytesIO]


def _excelfile(src: ExcelSource) -> pd.ExcelFile:
    if isinstance(src, (bytes, bytearray)):
        return pd.ExcelFile(BytesIO(src))
    return pd.ExcelFile(src)


def pick_pnl_sheet(xls: pd.ExcelFile) -> str:
    """Pick the data sheet: ignore ``_*`` and all-caps random names, take the longest."""
    cands = [
        s for s in xls.sheet_names
        if not s.startswith("_") and not re.fullmatch(r"[A-Z]{10,}", s)
    ]
    if not cands:
        cands = list(xls.sheet_names)
    best, best_rows = cands[0], -1
    for s in cands:
        full = xls.parse(s, header=None)
        if full.shape[0] > best_rows:
            best, best_rows = s, full.shape[0]
    return best


def _num(v) -> Optional[float]:
    if isinstance(v, (int, float, np.floating)) and pd.notna(v):
        return float(v)
    return None


def _derive_period(metadata: Dict[str, str]) -> Tuple[str, str]:
    """Return (canonical_key, human_label), e.g. ("2026-05", "May 2026")."""
    period = metadata.get("period", "")
    scenario = metadata.get("scenario", "")
    mm = re.search(r"\b(0?[1-9]|1[0-2])\b", period)
    month = mm.group(1).zfill(2) if mm else "00"
    ym = re.search(r"\b(20\d{2})\b", scenario) or re.search(r"\b(20\d{2})\b", period)
    year = ym.group(1) if ym else "0000"
    label_month = _MONTHS.get(month, month)
    return f"{year}-{month}", f"{label_month} {year}".strip()


def parse_pnl(src: ExcelSource) -> dict:
    """Parse the P&L export into ``{period, metadata, lines, fte}``."""
    xls = _excelfile(src)
    sheet = pick_pnl_sheet(xls)
    raw = xls.parse(sheet, header=None)

    def meta(r: int, c: int) -> str:
        v = raw.iat[r, c] if r < raw.shape[0] and c < raw.shape[1] else None
        return str(v).strip() if pd.notna(v) else ""

    metadata = {
        "report": meta(1, 6),
        "scenario": meta(2, 6),
        "period": meta(3, 6),
        "entity": meta(5, 6),
        "run_by": meta(6, 6),
        "currency": "EUR",
        "sheet": sheet,
    }

    # label -> (row, has_data) ; prefer the occurrence that actually carries figures
    label_row: Dict[str, Tuple[int, bool]] = {}
    for r in range(12, raw.shape[0]):
        lbl = raw.iat[r, 1]
        if pd.isna(lbl):
            continue
        lbl = str(lbl).strip()
        if not lbl or lbl == " ":
            continue
        has_data = any(_num(raw.iat[r, c]) is not None for c in (4, 16))
        if lbl not in label_row or (has_data and not label_row[lbl][1]):
            label_row[lbl] = (r, has_data)

    def vals(r: int) -> dict:
        def blk(m):
            return {k: _num(raw.iat[r, c]) for k, c in m.items()}
        return {"monthly": blk(MON), "cumul": blk(CUM)}

    lines = []
    section = ""
    for lbl, level, kind in STRUCT:
        if kind == "header":
            section = lbl[1:]
            lines.append({"label": section, "level": level, "kind": "header"})
            continue
        r = label_row.get(lbl, (None, False))[0]
        v = vals(r) if r is not None else {"monthly": {}, "cumul": {}}
        if lbl in _INCOME_OVERRIDE:
            pol = "income"
        elif lbl in _COST_OVERRIDE or section in _COST_SECTIONS:
            pol = "cost"
        else:
            pol = "income"
        lines.append(
            {"label": lbl, "level": level, "kind": kind, "values": v, "polarity": pol}
        )

    fte = {}
    for key, lbl in [("eop", "FTE EoP"), ("avg", "FTE Average")]:
        r = label_row.get(lbl, (None, False))[0]
        if r is not None:
            fte[key] = vals(r)

    period_key, _label = _derive_period(metadata)
    return {"period": period_key, "metadata": metadata, "lines": lines, "fte": fte}


def get_value(lines, label: str, block: str, key: str) -> Optional[float]:
    """Helper: read one figure from a parsed ``lines`` list."""
    for ln in lines:
        if ln["label"] == label and ln.get("values"):
            return ln["values"].get(block, {}).get(key)
    return None
