"""Parser of the SAP FI GL detail exports ("Total cost" / "Total cogs").

The GL account is NOT a dedicated column: it appears as group rows in the first
column ("Icône Postes rappr./PNS") formatted ``Compte NNNNNNN``. In these exports the
header sits at the BOTTOM of its group (the detail rows come first, then a subtotal,
then the ``Compte`` row) — so each detail row belongs to the NEXT header *below* it.
We therefore **backward-fill** the account onto the rows above each header. Detail rows
are the ones carrying a ``Type de pièce`` (posting type); header/subtotal rows have
none and must be excluded to avoid double counting.

Verified against May 2026: Σ detail rows of "Total cost" = 915 247,51 € = monthly
Total Costs, and every account's detail sum equals its stated subtotal (61/61).
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd

ACCOUNT_COL = "Icône Postes rappr./PNS"
TYPE_COL = "Type de pièce"
AMOUNT_COL = "Montant en devise interne"
DATE_COL = "Date de la pièce"
TEXT_COL = "Texte"

UNATTRIBUTED = "(sans compte)"  # detail rows with no header below them (rare)
_COMPTE_RE = re.compile(r"\s*Compte\s*([0-9A-Z]+)")

ExcelSource = Union[str, bytes, BytesIO]


def _parse_compte(x) -> Optional[str]:
    if isinstance(x, str):
        m = _COMPTE_RE.match(x)
        return m.group(1) if m else None
    return None


def parse_detail(src: ExcelSource, max_lines_per_account: int = 200) -> Dict[str, dict]:
    """Return ``{account: {"sum": float, "lines": [{date, text, amount}, ...]}}``.

    Amounts are recomputed from detail rows only (posting type present), never from
    the multi-level subtotal/header rows that SAP interleaves in the outline.
    """
    if isinstance(src, (bytes, bytearray)):
        df = pd.read_excel(BytesIO(src), sheet_name=0)
    else:
        df = pd.read_excel(src, sheet_name=0)

    if ACCOUNT_COL not in df.columns or TYPE_COL not in df.columns:
        raise ValueError(
            "Unexpected GL export columns: %s" % (list(df.columns)[:6],)
        )

    df["_hdr"] = df[ACCOUNT_COL].apply(_parse_compte)
    # Trailing-header layout: assign each row to the next "Compte" header below it.
    df["_account"] = df["_hdr"].bfill()

    detail = df[df[TYPE_COL].notna()].copy()

    accounts: Dict[str, dict] = {}
    for _, row in detail.iterrows():
        acc = row["_account"]
        acc = UNATTRIBUTED if pd.isna(acc) else str(acc)
        amt = row.get(AMOUNT_COL)
        if pd.isna(amt):
            continue
        amt = float(amt)
        bucket = accounts.setdefault(acc, {"sum": 0.0, "lines": []})
        bucket["sum"] += amt
        if len(bucket["lines"]) < max_lines_per_account:
            try:
                date_str = str(pd.to_datetime(row.get(DATE_COL)).date())
            except Exception:
                date_str = ""
            text = row.get(TEXT_COL)
            bucket["lines"].append({
                "date": date_str,
                "text": "" if pd.isna(text) else str(text)[:64],
                "amount": round(amt, 2),
            })
    return accounts


def total_detail(accounts: Dict[str, dict]) -> float:
    return round(sum(b["sum"] for b in accounts.values()), 2)


def build_drill(
    cost_accounts: Dict[str, dict],
    mapper,
    monthly_actual_for: "callable",
    note: str = "",
) -> Tuple[Dict[str, dict], List[Dict[str, object]]]:
    """Assemble the per-poste drill-down payload from parsed GL accounts.

    ``monthly_actual_for(poste)`` returns the P&L monthly actual for reconciliation.
    Returns ``(drill, unmapped_accounts)``.
    """
    by_poste, unmapped = mapper.group_by_poste(cost_accounts)
    drill: Dict[str, dict] = {}
    for poste, items in by_poste.items():
        items.sort(key=lambda kv: -abs(kv[1]["sum"]))
        mapped = round(sum(i[1]["sum"] for i in items), 2)
        drill[poste] = {
            "note": note,
            "meta": {"pnl": monthly_actual_for(poste), "mapped": mapped},
            "accounts": [
                {
                    "account": acc,
                    "label": "",
                    "sum": round(info["sum"], 2),
                    "lines": info["lines"],
                }
                for acc, info in items
            ],
        }
    unmapped_detail = [
        {"account": acc, "sum": round(cost_accounts[acc]["sum"], 2)} for acc in unmapped
    ]
    return drill, unmapped_detail
