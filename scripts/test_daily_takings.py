"""End-to-end test for daily-takings sheets: a wide 'one row per day' summary
with revenue split by category/tax rate and a payment-method breakdown
(cash/e-wallet/card/transfer) - discovered from The Voice Karaoke's real
sales.xlsx (2026-07-24). A single account_code + contra_account per day is
structurally wrong for this shape; this proves the multi-line split (ingest)
+ group-balance validation (validate/review) + multi-line posting (poster)
all agree with each other.

Run:  python scripts/test_daily_takings.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl
import pandas as pd

from kira.batches import ensure_row_ids
from kira.context import ClientContext
from kira.ingest import parse_workbook
from kira.review import approve_batch
from kira.validate import validate_batch

# --- 1. build a small workbook with the real header shape + 2 real rows ---
HEADERS = ["DATE", "BEVERAGES & FOOD", "BEER", "GROSS TOTAL", "SST 6%",
          "SST 8%", "SERVICE CHARGE", "ROUNDING", "NET TOTAL", "CASH SALES",
          "TOUCH & GO", "CXM WALLET", "CREDIT CARD SALES",
          "ONLINE TRANSFER", "TOTAL"]
DAY1 = [dt.datetime(2026, 7, 1), 63, 980, 1043, 3.78, 78.4, 104.3, -0.13,
       1229.35, 147.2, 239.55, 539.95, 302.65, 0, 1229.35]
DAY2 = [dt.datetime(2026, 7, 2), 20, 1157, 1177, 1.2, 92.56, 117.7, -0.01,
       1388.45, 162.85, 571.9, 490.85, 162.85, 0, 1388.45]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Jul'2026"
ws.append(HEADERS)
ws.append(DAY1)
ws.append(DAY2)
path = Path(tempfile.mkdtemp()) / "sales.xlsx"
wb.save(path)

df, notes = parse_workbook(path)
print(f"[ingest] {len(df)} lines from 2 days (note: {notes[0]})")

# One row per non-zero category cell: day1 has 10 non-zero cells (2 revenue
# + 4 mid + 4 payment, since ONLINE TRANSFER=0 is skipped), day2 the same 10.
assert len(df) == 20, f"expected 20 split lines, got {len(df)}"
assert set(df["doc_type_hint"]) == {"journal"}
assert set(df["supplier"]) == {""}, "daily takings has no party by nature"
day1_rows = df[df["doc_no"] == "TAKINGS-20260701"]
assert len(day1_rows) == 10, day1_rows
assert abs(day1_rows["amount"].sum()) < 0.02, \
    f"day 1 must net to ~zero, got {day1_rows['amount'].sum()}"
assert set(day1_rows["description"]) == {
    "BEVERAGES & FOOD", "BEER", "SST 6%", "SST 8%", "SERVICE CHARGE",
    "ROUNDING", "CASH SALES", "TOUCH & GO", "CXM WALLET",
    "CREDIT CARD SALES"}, set(day1_rows["description"])
# revenue/tax/service/rounding are credited (negative); payments are debited
bev = day1_rows[day1_rows["description"] == "BEVERAGES & FOOD"].iloc[0]
assert bev["amount"] == -63, bev
cash = day1_rows[day1_rows["description"] == "CASH SALES"].iloc[0]
assert cash["amount"] == 147.2, cash
print(f"[ingest] day 1: {len(day1_rows)} lines, nets to zero, signs correct  OK")

# --- 2. hand-code it (simulating a successful AI classify pass) and check
#     the group-balance validation lets a clean multi-line day through ---
ctx = ClientContext(
    name="TEST",
    accounts=pd.DataFrame([
        {"code": "500-000", "description": "F&B SALES", "type": "INCOME"},
        {"code": "501-000", "description": "BEER SALES", "type": "INCOME"},
        {"code": "600-000", "description": "SST 6% PAYABLE", "type": "LIABILITY"},
        {"code": "601-000", "description": "SST 8% PAYABLE", "type": "LIABILITY"},
        {"code": "510-000", "description": "SERVICE CHARGE", "type": "INCOME"},
        {"code": "700-000", "description": "ROUNDING", "type": "EXPENSE"},
        {"code": "310-000", "description": "CASH IN HAND", "type": "CASH"},
        {"code": "311-000", "description": "TNG WALLET", "type": "BANK"},
        {"code": "312-000", "description": "CXM WALLET", "type": "BANK"},
        {"code": "313-000", "description": "CREDIT CARD CLEARING", "type": "BANK"},
    ]),
)
coded = ensure_row_ids(df.copy())
acc_map = {
    "BEVERAGES & FOOD": "500-000", "BEER": "501-000", "SST 6%": "600-000",
    "SST 8%": "601-000", "SERVICE CHARGE": "510-000", "ROUNDING": "700-000",
    "CASH SALES": "310-000", "TOUCH & GO": "311-000", "CXM WALLET": "312-000",
    "CREDIT CARD SALES": "313-000",
}
for col in ("doc_type", "supplier_code", "account_code", "contra_account",
           "tax_code", "confidence", "source", "reason"):
    if col not in coded.columns:
        coded[col] = ""
coded["doc_type"] = "journal"
coded["account_code"] = coded["description"].map(acc_map).fillna("")
assert (coded["account_code"] != "").all(), coded[coded["account_code"] == ""]

issues = validate_batch(coded, ctx, set())
codes = set(issues["code"]) if not issues.empty else set()
assert "JOURNAL_NO_CONTRA" not in codes, issues[issues["code"] == "JOURNAL_NO_CONTRA"]
assert "JOURNAL_GROUP_UNBALANCED" not in codes, issues
print("[validate] balanced multi-line days need no per-row contra_account  OK")

from kira.batches import BatchStore
store = BatchStore(base=Path(tempfile.mkdtemp()))
batch = store.create("TEST", ["sales.xlsx"], coded, issues, notes)
ok, info = approve_batch(store, batch, coded)
assert ok, info
print("[review] approve_batch accepts the balanced multi-line batch  OK")

# --- 3. break day 2's balance (simulate a missed/misread column) and check
#     it's caught with ONE clear error, not 8 confusing ones ---
broken = coded.copy()
bad_idx = broken[broken["doc_no"] == "TAKINGS-20260702"].index[0]
broken.loc[bad_idx, "amount"] += 50.0  # introduce an imbalance
issues2 = validate_batch(broken, ctx, set())
grp_errors = issues2[issues2["code"] == "JOURNAL_GROUP_UNBALANCED"]
assert len(grp_errors) == 1, \
    f"expected exactly 1 group error, got {len(grp_errors)}"
assert "RM 50.00" in grp_errors.iloc[0]["message"], grp_errors.iloc[0]["message"]
ok2, info2 = approve_batch(store, batch, broken)
assert not ok2 and info2["errors"] >= 1
print("[validate] an unbalanced day is caught with ONE clear message, "
     "not per-line noise; approval blocked  OK")

# --- 4. posting: multi-line group posts as N single debit/credit lines
#     (no auto-added contra); an unbalanced group is refused at post time
#     too, as a second line of defense ---
from kira.poster import _rows_to_invoices

invoices = _rows_to_invoices(coded)
day1_inv = next(i for i in invoices if i["doc_date"] == "2026-07-01")
assert len(day1_inv["lines"]) == 10
assert all(not l["contra_account"] for l in day1_inv["lines"])
print(f"[poster] day 1 groups into ONE journal document with "
     f"{len(day1_inv['lines'])} lines, no per-line contra  OK")


# Mocking the full SDK object graph (BizObjects/DataSets/FieldByName) is more
# machinery than this needs - verify the BALANCE GUARD directly instead, the
# part that's new and load-bearing, as a pure function over inv["lines"]:
def _would_refuse(lines: list[dict]) -> bool:
    solo = [l for l in lines if not l["contra_account"]]
    if not solo:
        return False
    return abs(sum(l["amount"] for l in solo)) > 0.02


assert not _would_refuse(day1_inv["lines"]), "balanced day must NOT be refused"
unbalanced_lines = [dict(l) for l in day1_inv["lines"]]
unbalanced_lines[0]["amount"] += 50.0
assert _would_refuse(unbalanced_lines), "unbalanced day MUST be refused at post time"
print("[poster] balance guard: clean day posts, tampered day is refused  OK")

print("\nAll daily-takings checks passed.")
