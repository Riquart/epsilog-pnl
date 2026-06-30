"""Account -> P&L poste mapping.

Loads ``mapping/account_to_poste.csv`` and resolves a GL account to a management
poste using the *longest matching prefix*. This mapping is PROVISIONAL: the postes
are a reclassification of the SAP 212-000 report hierarchy, so the CSV should
ideally be replaced by the exact account->node export from SAP (prompt sections 7 & 9).
"""
from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional, Tuple

_DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "mapping",
    "account_to_poste.csv",
)

PROVISIONAL_BANNER = (
    "⚠️ Rattachement <b>provisoire</b> (par classe comptable PCG). Les postes de "
    "gestion sont une <b>reclassification</b> des comptes : le rattachement exact "
    "vient de la hiérarchie du rapport SAP 212-000 (à fournir en CSV — voir le "
    "prompt §7). Les écarts ci-dessous sont donc normaux à ce stade."
)


def load_mapping(path: Optional[str] = None) -> List[Tuple[str, str]]:
    """Return ``[(prefix, poste), ...]`` sorted by descending prefix length."""
    path = path or _DEFAULT_CSV
    rows: List[Tuple[str, str]] = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            prefix = row[0].strip()
            if not prefix or prefix.startswith("#") or prefix.lower() == "account_prefix":
                continue
            poste = row[1].strip() if len(row) > 1 else ""
            # strip trailing inline comments on the poste cell
            poste = poste.split("#", 1)[0].strip()
            if poste:
                rows.append((prefix, poste))
    rows.sort(key=lambda kv: -len(kv[0]))
    return rows


class Mapper:
    def __init__(self, path: Optional[str] = None):
        self.rules = load_mapping(path)

    def poste_for(self, account: str) -> Optional[str]:
        """Longest-prefix match. Returns None if no rule matches."""
        acc = str(account or "").strip()
        for prefix, poste in self.rules:  # already sorted longest-first
            if acc.startswith(prefix):
                return poste
        return None

    def group_by_poste(
        self, accounts: Dict[str, dict]
    ) -> Tuple[Dict[str, List[Tuple[str, dict]]], List[str]]:
        """Split ``{account: info}`` into ``{poste: [(account, info)]}`` and a list
        of unmapped account numbers."""
        by_poste: Dict[str, List[Tuple[str, dict]]] = {}
        unmapped: List[str] = []
        for acc, info in accounts.items():
            poste = self.poste_for(acc)
            if poste is None:
                poste = "Other expenses"
                unmapped.append(acc)
            by_poste.setdefault(poste, []).append((acc, info))
        return by_poste, unmapped
