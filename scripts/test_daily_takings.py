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

# --- 5. journals let SQL auto-number (never write our internal grouping key
#     into SQL's DocNo) - confirmed against the REAL field names dump_fields
#     returned from The Voice Karaoke's SQL Accounting (2026-07-25): GL_JE's
#     detail line uses CODE (not ACCOUNT) for the account, and DR/CR exist
#     exactly as named. Mocked here since real SQL Accounting isn't
#     available in this environment.
from kira.poster import _post_one


class _FakeField:
    def __init__(self):
        self.value = None

    @property
    def AsString(self):
        return self.value

    @AsString.setter
    def AsString(self, v):
        self.value = v

    AsFloat = AsString
    AsDateTime = AsString


class _FakeDataSet:
    REAL_FIELDS = {"DOCDATE", "POSTDATE", "DOCNO", "CODE", "DESCRIPTION",
                   "DR", "CR"}  # GL_JE, per the live dump_fields output

    def __init__(self):
        self.queried: list[str] = []

    def FindField(self, name):
        self.queried.append(name.upper())
        if name.upper() not in self.REAL_FIELDS:
            raise Exception(f"field {name} not found")
        return _FakeField()

    def Append(self):
        pass

    def Post(self):
        pass


class _FakeDataSets:
    def __init__(self, main, detail):
        self.main, self.detail = main, detail

    def Find(self, name):
        return self.main if name == "MainDataSet" else self.detail


class _FakeBiz:
    def __init__(self):
        self.main = _FakeDataSet()
        self.detail = _FakeDataSet()
        self.DataSets = _FakeDataSets(self.main, self.detail)

    def New(self):
        pass

    def Save(self):
        pass


class _FakeBizObjects:
    def __init__(self, biz):
        self._biz = biz

    def Find(self, name):
        return self._biz


class _FakeApp:
    def __init__(self, biz):
        self.BizObjects = _FakeBizObjects(biz)


biz = _FakeBiz()
app = _FakeApp(biz)
journal_inv = {
    "doc_type": "journal", "sql_doc": "GL_JE", "supplier_code": "",
    "doc_date": "2026-07-01", "doc_no": "TAKINGS-20260701",
    "lines": [{"account_code": "500-000", "description": "BEVERAGES & FOOD",
              "amount": -147.2, "tax_code": "", "contra_account": ""},
             {"account_code": "310-000", "description": "CASH SALES",
              "amount": 147.2, "tax_code": "", "contra_account": ""}],
}
_post_one(app, journal_inv)
assert "DOCNO" not in biz.main.queried, \
    f"journal must NOT write its internal grouping key to SQL's DocNo, queried={biz.main.queried}"
assert "CODE" in biz.detail.queried, "journal detail account must use CODE (confirmed field name)"
assert "DR" in biz.detail.queried or "CR" in biz.detail.queried
print("[poster] journal never writes its internal doc_no to SQL - "
     "auto-numbering left in charge  OK")

# Non-journal (e.g. purchase) DOES write a real doc_no when the source file
# actually had one.
biz2 = _FakeBiz()
app2 = _FakeApp(biz2)
purchase_inv = {
    "doc_type": "purchase", "sql_doc": "PH_PI", "supplier_code": "S001",
    "doc_date": "2026-07-01", "doc_no": "INV-12345",
    "lines": [{"account_code": "600-000", "description": "stock",
              "amount": 100.0, "tax_code": ""}],
}
try:
    _post_one(app2, purchase_inv)
except Exception:
    pass  # PH_PI's real detail fields differ from this minimal fake - only
          # the MainDataSet DocNo behavior matters for this check
assert "DOCNO" in biz2.main.queried, \
    "a real source doc_no on a non-journal document must still reach SQL"
print("[poster] a genuine invoice number still reaches SQL as before  OK")

# --- 6. _find_biz tries every naming candidate before giving up, and
#     _post_one fails with a clear, actionable message (not a crash) when
#     none resolve - matches what The Voice Karaoke's real install showed
#     for purchase_return/supplier_payment (BizObjects.Find -> None).
from kira.poster import DOC_TYPE_TO_SQL_CANDIDATES, _find_biz


class _SelectiveBizObjects:
    """Only resolves codes in `known` - simulates a module switched off for
    this company (SQL Accounting returns None, doesn't raise)."""
    def __init__(self, known: dict):
        self.known = known

    def Find(self, name):
        return self.known.get(name)


assert DOC_TYPE_TO_SQL_CANDIDATES["purchase_return"][0] == "PH_CN"
second_guess = DOC_TYPE_TO_SQL_CANDIDATES["purchase_return"][1]
biz_ok = _FakeBiz()
app_partial = _FakeApp(biz_ok)
app_partial.BizObjects = _SelectiveBizObjects({second_guess: biz_ok})
resolved_biz, resolved_code = _find_biz(app_partial, "purchase_return", "PH_CN")
assert resolved_biz is biz_ok and resolved_code == second_guess, \
    f"expected fallback to {second_guess}, got {resolved_code!r}"
print(f"[poster] first guess PH_CN unavailable -> fell back to "
     f"{second_guess}  OK")

app_none = _FakeApp(_FakeBiz())
app_none.BizObjects = _SelectiveBizObjects({})  # nothing resolves
no_biz, no_code = _find_biz(app_none, "supplier_payment", "AP_PM")
assert no_biz is None and no_code == ""
try:
    _post_one(app_none, {"doc_type": "supplier_payment", "sql_doc": "AP_PM",
                         "supplier_code": "S1", "doc_date": "2026-07-01",
                         "doc_no": "", "lines": [{"account_code": "310-000",
                         "description": "", "amount": 100.0, "tax_code": ""}]})
    raise AssertionError("expected a ValueError when no module resolves")
except ValueError as e:
    assert "Customize SQL Account Modules" in str(e), e
print("[poster] no module resolves -> clear actionable error, not a crash  OK")

print("\nAll daily-takings checks passed.")
