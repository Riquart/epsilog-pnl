"""Golden-number tests for the P&L and GL detail parsers (May 2026, Actual reported).

Run: ``python -m pytest`` from the repo root. Requires the sample .xlsx files in
``sample_data/`` (not committed — they are confidential).
"""
import os

import pytest

from app.main import assemble_snapshot
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


def test_account_headers_detected():
    accounts = parse_detail(COST_FILE)
    for acc in ("6000000", "6054700", "6001500FR"):
        assert acc in accounts, f"account header {acc} not detected"
    # Contractors account ties out to the P&L poste
    assert abs(accounts["6054700"]["sum"] - 166_618.5) <= 1.0


def test_every_detail_row_has_account():
    accounts = parse_detail(COST_FILE)
    # No bucket key should be empty/None; the synthetic payroll key is allowed.
    assert all(k for k in accounts)


def test_assemble_snapshot_roundtrip():
    with open(PNL_FILE, "rb") as f:
        pnl_b = f.read()
    with open(COST_FILE, "rb") as f:
        cost_b = f.read()
    snap = assemble_snapshot(pnl_b, cost_b)
    assert snap["period"] == "2026-05"
    assert snap["drill"], "drill-down should be populated when cost file is provided"
    # The drill reconciliation for Contractors should be exact.
    contr = snap["drill"].get("Contractors")
    assert contr and abs(contr["meta"]["mapped"] - contr["meta"]["pnl"]) <= 1.0
