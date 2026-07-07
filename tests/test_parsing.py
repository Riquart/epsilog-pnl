"""Golden-number tests for the P&L and GL detail parsers (May 2026, Actual reported).

Run: ``python -m pytest`` from the repo root. Requires the sample .xlsx files in
``sample_data/`` (not committed — they are confidential).
"""
import os

import pytest

from app.main import _prepare_snapshot, assemble_snapshot
from app.parsing.detail import parse_detail, total_detail
from app.parsing.pnl import get_value, parse_pnl

ROOT = os.path.dirname(os.path.dirname(__file__))
SAMPLE = os.path.join(ROOT, "sample_data")
PNL_FILE = os.path.join(SAMPLE, "May 2026 - P&L- Epsilog.xlsx")
COST_FILE = os.path.join(SAMPLE, "May 2026 - Total cost - Epsilog.xlsx")
COGS_FILE = os.path.join(SAMPLE, "May 2026 - Total cogs - Epsilog.xlsx")

pytestmark = pytest.mark.skipif(
    not os.path.exists(PNL_FILE), reason="sample data not present"
)

TOL = 1.0  # ±1 €

# (label, YTD/cumul, monthly)
GOLDEN = [
    ("Total Revenues", 7_716_771, 1_515_386),
    ("Total COGS", 584_305, 50_426),
    ("Gross Profit Total", 7_132_466, 1_464_961),
    ("Total Costs", 4_735_266, 915_248),
    ("EBITDA after Central IC", 2_583_634, 589_535),
    ("EBIT", 1_248_763, 341_647),
    ("Net result", 385_990, -238_727),
    ("Personnel expenses", 2_716_720, 514_822),
]


@pytest.fixture(scope="module")
def pnl():
    return parse_pnl(PNL_FILE)


def test_period_and_entity(pnl):
    assert pnl["period"] == "2026-05"
    assert "EPSILOG" in pnl["metadata"]["entity"]


@pytest.mark.parametrize("label,ytd,mon", GOLDEN)
def test_golden_values(pnl, label, ytd, mon):
    assert abs(get_value(pnl["lines"], label, "cumul", "actual") - ytd) <= TOL
    assert abs(get_value(pnl["lines"], label, "monthly", "actual") - mon) <= TOL


def test_fte_eop(pnl):
    assert round(pnl["fte"]["eop"]["monthly"]["actual"]) == 113


def test_total_costs_identity(pnl):
    g = lambda l: get_value(pnl["lines"], l, "monthly", "actual")  # noqa: E731
    total = g("Total Costs")
    parts = g("Personnel expenses") + g("Outsourcing") + g("Contractors") + g("Other OPEX")
    assert abs(total - parts) <= TOL


def test_ebitda_identity(pnl):
    g = lambda l: get_value(pnl["lines"], l, "monthly", "actual")  # noqa: E731
    ebitda = g("EBITDA after Central IC")
    bridge = (
        g("Total Revenues") - g("Total COGS") - g("Total Costs")
        + g("Capitalized in-house services") + g("Other income")
    )
    assert abs(ebitda - bridge) <= TOL


def test_cost_detail_reconciliation():
    accounts = parse_detail(COST_FILE)
    assert abs(total_detail(accounts) - 915_247.51) <= 0.01


def test_account_headers_detected_and_backfilled():
    accounts = parse_detail(COST_FILE)
    for acc in ("6000000", "6054700", "6001500FR"):
        assert acc in accounts, f"account header {acc} not detected"
    # Trailing-header layout: the leading payroll block backfills onto CACG 6000000.
    assert abs(accounts["6000000"]["sum"] - 298_244.5) <= 1.0


def test_every_detail_row_has_account():
    accounts = parse_detail(COST_FILE)
    assert all(k for k in accounts)  # no empty/None bucket keys


def test_poste_reconciliation_after_backfill():
    """With backward-fill + the official mapping, most postes tie out to the P&L
    to the euro (this was masked by the earlier forward-fill bug)."""
    with open(PNL_FILE, "rb") as f:
        pnl_b = f.read()
    with open(COST_FILE, "rb") as f:
        cost_b = f.read()
    served = _prepare_snapshot(assemble_snapshot(pnl_b, cost_b))

    def poste_sum(p):
        return sum(a["sum"] for a in served["drill"].get(p, {}).get("accounts", []))

    assert abs(poste_sum("Personnel expenses") - 514_822) <= 1
    assert abs(poste_sum("Contractors") - 166_618) <= 1
    assert abs(poste_sum("ICT") - 23_343) <= 1
    assert abs(poste_sum("Marketing") - 14_938) <= 1


def test_assemble_snapshot_roundtrip():
    with open(PNL_FILE, "rb") as f:
        pnl_b = f.read()
    with open(COST_FILE, "rb") as f:
        cost_b = f.read()
    snap = assemble_snapshot(pnl_b, cost_b)
    assert snap["period"] == "2026-05"
    assert snap["accounts"], "raw GL accounts should be stored in the snapshot"
    served = _prepare_snapshot(snap)
    assert served["drill"], "drill-down should resolve from the current mapping"
    # Every cost account maps via the official correspondence table (0 unmapped).
    assert served["unmapped_accounts"] == []


def test_cogs_reading_cards_under_hardware():
    """The 'Reading Card …' postings sit above the `Compte 5050500` header, so they
    backfill onto 5050500 → COGS Hardware — not onto Personnel."""
    with open(PNL_FILE, "rb") as f:
        pnl_b = f.read()
    with open(COST_FILE, "rb") as f:
        cost_b = f.read()
    with open(COGS_FILE, "rb") as f:
        cogs_b = f.read()
    served = _prepare_snapshot(assemble_snapshot(pnl_b, cost_b, cogs_b))
    hw = served["drill"].get("COGS Hardware", {})
    hw_accts = {a["account"] for a in hw.get("accounts", [])}
    assert "5050500" in hw_accts
    reading = [
        l for a in hw.get("accounts", []) for l in a["lines"]
        if "Reading Card" in (l.get("text") or "")
    ]
    assert reading, "reading-card postings should be attached to COGS Hardware"
    # And never under Personnel.
    for a in served["drill"].get("Personnel expenses", {}).get("accounts", []):
        assert not any("Reading Card" in (l.get("text") or "") for l in a["lines"])


def test_opex_mapping_reconciles_to_total_costs():
    """The official mapping must tie the sum of all mapped OPEX accounts to the
    P&L monthly Total Costs (915 247,51 €). Per-poste gaps are expected
    (management reclassification), but the grand total must reconcile."""
    with open(PNL_FILE, "rb") as f:
        pnl_b = f.read()
    with open(COST_FILE, "rb") as f:
        cost_b = f.read()
    served = _prepare_snapshot(assemble_snapshot(pnl_b, cost_b))
    total_mapped = sum(
        acc["sum"] for poste in served["drill"].values() for acc in poste["accounts"]
    )
    assert abs(total_mapped - 915_247.51) <= 0.01
