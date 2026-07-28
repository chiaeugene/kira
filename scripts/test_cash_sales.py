"""End-to-end test for the Cash Sales route - the accountant's ruling
(2026-07-26, on video): daily takings post as ONE Sales > Cash Sales
document per day, never GL journal. Her worked example for 01.07.2026 is
the ground truth this test encodes:

  500-000  SALES (F&B)        63.00  SV 6%   -> SQL computes 3.78
  500-000  SALES (BEER)      980.00  SV 8%   -> SQL computes 78.40
  500-002  ROUDING            (0.13)
  500-001  SERVICE CHARGE    104.30
  305-C01  CXM WALLET       (539.95)  <- negative payment lines
  305-P01  CREDIT CARD      (302.65)
  305-D01  TOUCH & GO       (239.55)
  ------------------------------------------
  Net Total = 147.20 = the sheet's CASH SALES column (the residual)

Run:  python scripts/test_cash_sales.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl
import pandas as pd

from kira.batches import ensure_row_ids, records_to_df, rows_to_records
from kira.classify import classify
from kira.context import ClientContext
from kira.ingest import parse_workbook
from kira.rules import RuleStore
from kira.validate import validate_batch

HEADERS = ["DATE", "BEVERAGES & FOOD", "BEER", "GROSS TOTAL", "SST 6%",
          "SST 8%", "SERVICE CHARGE", "ROUDING", "NET TOTAL", "CASH SALES",
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

# --- 1. ingest: cash-sale rows, correct signs, tax pairing, control ---
df, notes = parse_workbook(path)
assert set(df["doc_type_hint"]) == {"cash_sale"}, set(df["doc_type_hint"])
day1 = df[df["doc_no"] == "TAKINGS-20260701"]
# 7 lines: F&B, BEER, SERVICE CHARGE, ROUDING, TnG, CXM, CC (no SST lines,
# no cash line, zero ONLINE TRANSFER skipped)
assert len(day1) == 7, day1[["description", "amount"]]
by_desc = {r["description"]: r for _, r in day1.iterrows()}
assert by_desc["BEVERAGES & FOOD"]["amount"] == 63.0
assert by_desc["BEVERAGES & FOOD"]["tax"] == 3.78, "SST 6% pairs with F&B"
assert by_desc["BEVERAGES & FOOD"]["tax_rate"] == 6.0
assert by_desc["BEER"]["amount"] == 980.0
assert by_desc["BEER"]["tax"] == 78.4, "SST 8% pairs with BEER"
assert by_desc["BEER"]["tax_rate"] == 8.0
assert by_desc["ROUDING"]["amount"] == -0.13, "sheet sign kept"
assert by_desc["SERVICE CHARGE"]["amount"] == 104.3
for pay in ("TOUCH & GO", "CXM WALLET", "CREDIT CARD SALES"):
    assert by_desc[pay]["amount"] < 0, f"{pay} must be a negative line"
assert "SST 6%" not in by_desc and "SST 8%" not in by_desc, \
    "SST is never a line - the tax code computes it"
assert (day1["control_total"] == 147.2).all(), "cash column is the control"
total1 = float((day1["amount"] + day1["tax"]).sum())
assert abs(total1 - 147.2) <= 0.02, \
    f"lines+tax must equal the cash column, got {total1}"
print(f"[ingest] day 1 -> 7 cash-sale lines, tax paired by arithmetic, "
      f"lines+tax = {total1:.2f} = CASH column  OK")
assert "ties to the book's own total" in notes[0], notes

# --- 2. classify (offline fallback): walk-in customer from the master ---
ctx = ClientContext(
    name="TEST",
    customers=pd.DataFrame([{"code": "300-C0001", "name": "CASH SALES"}]),
    accounts=pd.DataFrame([
        {"code": "500-000", "description": "CASH SALES", "type": "SALES"},
        {"code": "500-001", "description": "SERVICES CHARGES", "type": "SALES"},
        {"code": "500-002", "description": "ROUDING ADJUSTMENT", "type": "SALES"},
        {"code": "305-C01", "description": "CXM WALLET", "type": "BANK"},
        {"code": "305-P01", "description": "PBB CREDIT CARD", "type": "BANK"},
        {"code": "305-D01", "description": "DUIT NOW", "type": "BANK"},
    ]),
    tax_codes=pd.DataFrame([{"code": "SV", "description": "Service Tax",
                             "rate": "6"}]),
)
store = RuleStore.__new__(RuleStore)
store.rules = {}
store.lookup = lambda *a, **k: None
coded = classify(ensure_row_ids(df.copy()), ctx, store)
assert (coded["doc_type"] == "cash_sale").all()
assert (coded["supplier_code"] == "300-C0001").all(), \
    "every line carries the walk-in customer"
print("[classify] fallback picks the walk-in customer 300-C0001  OK")

# --- 3. validate: clean batch passes; a misread column is caught ---
acc_map = {"BEVERAGES & FOOD": "500-000", "BEER": "500-000",
           "SERVICE CHARGE": "500-001", "ROUDING": "500-002",
           "TOUCH & GO": "305-D01", "CXM WALLET": "305-C01",
           "CREDIT CARD SALES": "305-P01"}
coded["account_code"] = coded["description"].map(acc_map)
coded.loc[coded["tax_rate"] > 0, "tax_code"] = "SV"
issues = validate_batch(coded, ctx, set())
errs = issues[issues["severity"] == "error"] if not issues.empty else issues
assert errs.empty, errs
print("[validate] her template validates clean (no NEGATIVE_AMOUNT noise, "
      "control matches)  OK")

broken = coded.copy()
bad_ix = broken[(broken["doc_no"] == "TAKINGS-20260701")
                & (broken["description"] == "BEER")].index[0]
broken.loc[bad_ix, "amount"] = 890.0  # misread a digit
issues2 = validate_batch(broken, ctx, set())
codes2 = set(issues2["code"]) if not issues2.empty else set()
assert "CASH_SALE_CONTROL_MISMATCH" in codes2, codes2
print("[validate] a misread column fails the cash-control check  OK")

# --- 4. round-trip through batch records (tax_rate/control survive JSON) --
rt = records_to_df(rows_to_records(coded))
assert (rt[rt["doc_no"] == "TAKINGS-20260701"]["control_total"] == 147.2).all()
assert rt[rt["description"] == "BEER"]["tax_rate"].iloc[0] == 8.0
print("[batches] tax_rate + control_total survive the JSON round-trip  OK")

# --- 5. poster: one SL_CS doc per day, DocNo left to SQL, tax rate set,
#        amounts verified by read-back (fake modeled on the REAL SL_CS
#        field dump from The Voice's install, 2026-07-26) ---
from kira.poster import _post_one, _rows_to_invoices

invoices = _rows_to_invoices(coded)
cs = [i for i in invoices if i["doc_type"] == "cash_sale"]
assert len(cs) == 2 and all(i["sql_doc"] == "SL_CS" for i in cs)
day1_inv = next(i for i in cs if i["doc_date"] == "2026-07-01")
assert len(day1_inv["lines"]) == 7
assert abs(sum(l["amount"] + l["tax_amount"] for l in day1_inv["lines"])
           - 147.2) <= 0.02, "document total must be the day's cash"


class _FakeField:
    def __init__(self):
        self.stored = None

    @property
    def Value(self):
        return self.stored

    @Value.setter
    def Value(self, v):
        self.stored = v

    def _refuse(self, v):
        raise Exception("typed As* setters not settable via COM wrapper")

    AsString = property(lambda self: self.stored, _refuse)
    AsFloat = property(lambda self: self.stored, _refuse)
    AsDateTime = property(lambda self: self.stored, _refuse)


class _FakeFieldMeta:
    def __init__(self, name):
        self.FieldName = name


class _FakeFields:
    def __init__(self, names):
        self._names = names

    @property
    def Count(self):
        return len(self._names)

    def Items(self, i):
        return _FakeFieldMeta(self._names[i])


class _FakeDataSet:
    """Mirrors the REAL SL_CS behaviors that burned the first live run
    (2026-07-28): Append() carries the previous line's TAX/TAXRATE onto the
    new line (seen pre-filled 'SV 8%' in the accountant's video), and Post()
    computes TAXAMT from the tax code's rate whenever TAX is set."""

    TAX_CODE_RATES = {"SV": None}  # None = honour the line's TAXRATE

    def __init__(self, names):
        self.names = names
        self.Fields = _FakeFields(names)
        self.fields = {n: _FakeField() for n in names}
        self.posted_rows: list[dict] = []
        self.queried: list[str] = []

    def FindField(self, name):
        self.queried.append(name)
        if name not in self.names:
            raise Exception(f"field {name} not found")
        return self.fields[name]

    def Append(self):
        prev_tax = (self.fields.get("TAX").stored
                    if "TAX" in self.fields else None)
        prev_rate = (self.fields.get("TAXRATE").stored
                     if "TAXRATE" in self.fields else None)
        self.fields = {n: _FakeField() for n in self.names}
        # SQL Accounting's inheritance: the new line starts with the
        # previous line's tax — the exact trap the poster must clear.
        if "TAX" in self.fields and prev_tax:
            self.fields["TAX"].stored = prev_tax
            self.fields["TAXRATE"].stored = prev_rate

    def Post(self):
        if "TAXAMT" in self.fields:
            code = self.fields.get("TAX").stored if "TAX" in self.fields else None
            if code:
                fixed = self.TAX_CODE_RATES.get(code, 0.0)
                rate = (self.fields["TAXRATE"].stored or 0.0
                        if fixed is None else fixed)
                amt = self.fields.get("UNITPRICE").stored or 0.0
                self.fields["TAXAMT"].stored = round(amt * rate / 100, 2)
            else:
                self.fields["TAXAMT"].stored = 0.0
        self.posted_rows.append({n: f.stored for n, f in self.fields.items()
                                 if f.stored is not None})


# exact field names from the live SL_CS dump (subset that matters)
MAIN = ("DOCKEY", "DOCNO", "DOCDATE", "POSTDATE", "TAXDATE", "CODE",
        "DESCRIPTION", "DOCAMT", "P_PAYMENTMETHOD", "P_AMOUNT")
DETAIL = ("DTLKEY", "SEQ", "ACCOUNT", "DESCRIPTION", "QTY", "UNITPRICE",
          "AMOUNT", "TAX", "TAXRATE", "TAXAMT")


class _FakeDataSets:
    def __init__(self, main, det):
        self.main, self.det = main, det

    def Find(self, name):
        return self.main if name == "MainDataSet" else self.det


class _FakeBiz:
    def __init__(self):
        self.main = _FakeDataSet(MAIN)
        self.detail = _FakeDataSet(DETAIL)
        self.DataSets = _FakeDataSets(self.main, self.detail)
        self.saved = False

    def New(self):
        pass

    def Save(self):
        self.saved = True


class _FakeBizObjects:
    def __init__(self, biz):
        self._biz = biz

    def Find(self, name):
        return self._biz if name == "SL_CS" else None


class _FakeApp:
    def __init__(self, biz):
        self.BizObjects = _FakeBizObjects(biz)


biz = _FakeBiz()
_post_one(_FakeApp(biz), day1_inv)
assert biz.saved
assert "DOCNO" not in biz.main.queried, \
    "cash sale must let SQL auto-number (CS 2607-xxxx), never TAKINGS- tags"
assert biz.main.fields["CODE"].stored == "300-C0001"
assert isinstance(biz.main.fields["DOCDATE"].stored, dt.datetime)
rows = biz.detail.posted_rows
assert len(rows) == 7, len(rows)
assert isinstance(biz.main.fields["TAXDATE"].stored, dt.datetime), \
    "tax date must be set explicitly (unset -> closed-SST-period default)"
beer = next(r for r in rows if r.get("UNITPRICE") == 980.0)
assert beer["TAX"] == "SV" and beer["TAXRATE"] == 8.0
assert beer["TAXAMT"] == 78.4, "SQL's computed SST must match the book"
# THE 2026-07-28 KILLER: lines after BEER inherit 'SV 8%' via Append -
# the poster must clear it, or every payment line carries phantom tax.
for r in rows:
    if (r.get("UNITPRICE") or 0) < 0:
        assert not r.get("TAX"), f"payment line inherited tax: {r}"
        assert not r.get("TAXAMT"), f"phantom tax computed: {r}"
total_incl = sum((r.get("UNITPRICE") or 0) + (r.get("TAXAMT") or 0)
                 for r in rows)
assert abs(total_incl - 147.2) <= 0.02, \
    f"document total must be the day's cash, got {total_incl}"
print("[poster] SL_CS: 7 lines, phantom tax CLEARED on inherited lines, "
      f"TAXDATE set, doc total {total_incl:.2f} = cash  OK")

# --- 6. wrong tax code -> SQL computes a different SST than the book ->
#        refuse BEFORE save (the ST-vs-SV class of failure) ---
biz2 = _FakeBiz()
biz2.detail.TAX_CODE_RATES = {"SV": None, "ST10": 10.0}
bad_inv = {**day1_inv, "lines": [dict(l) for l in day1_inv["lines"]]}
for l in bad_inv["lines"]:
    if l["tax_code"]:
        l["tax_code"] = "ST10"  # a 10% code against the book's 6%/8%
try:
    _post_one(_FakeApp(biz2), bad_inv)
    raise AssertionError("wrong tax code must be refused before save")
except ValueError as e:
    assert "tax mismatch" in str(e), e
assert not biz2.saved, "nothing may be saved on a tax mismatch"
print("[poster] wrong tax code -> computed SST != book -> refused, "
      "nothing saved  OK")

# --- 7. description-keyed learning: the accountant's corrections stick ---
from kira.batches import BatchStore
from kira.review import approve_batch
from kira.rules import RuleStore as RealRuleStore

rules_dir = Path(tempfile.mkdtemp())
real_rules = RealRuleStore(rules_dir)
import kira.review as review_mod
_orig_open = review_mod.open_client


class _A:
    def log_correction(self, *a, **k):
        pass

    def log_batch(self, *a, **k):
        pass


review_mod.open_client = lambda name, base="client_data": (ctx, real_rules, _A())
try:
    bstore = BatchStore(base=Path(tempfile.mkdtemp()))
    b = bstore.create("TEST", ["sales.xlsx"], coded, validate_batch(coded, ctx, set()), [])
    ok, info = approve_batch(bstore, b, coded)
    assert ok, info
finally:
    review_mod.open_client = _orig_open

fresh = classify(ensure_row_ids(df.copy()), ctx, real_rules)
beer_fresh = fresh[fresh["description"] == "BEER"].iloc[0]
assert beer_fresh["source"] == "rule", beer_fresh
assert beer_fresh["account_code"] == "500-000"
assert beer_fresh["supplier_code"] == "300-C0001"
print("[learning] approved categories become description-keyed rules - "
      "next month codes itself from her choices  OK")

print("\nAll cash-sales checks passed.")
