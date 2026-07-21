"""Torture tests for the conversion path — the nastiest Excel habits real
bookkeepers have. Each case builds a file in a temp dir and asserts exact
extraction: right rows, right amounts, nothing silently dropped or invented."""

import datetime as dt
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kira.ingest import parse_purchase_listing, parse_workbook

TMP = Path(tempfile.mkdtemp(prefix="kira_torture_"))
passed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed
    assert cond, f"FAILED: {name} {detail}"
    passed += 1
    print(f"  ok  {name}")


def make(name: str, rows: list[list]) -> Path:
    p = TMP / name
    pd.DataFrame(rows).to_excel(p, index=False, header=False)
    return p


# 1. Excel serial dates + numeric amounts stored as numbers
serial = dt.date(2026, 6, 7)
serial_num = (pd.Timestamp(serial) - pd.Timestamp("1899-12-30")).days
p = make("serial_dates.xlsx", [
    ["Date", "Supplier", "Description", "Amount"],
    [serial_num, "Alpha Trading", "goods", 100.50],
    [serial_num + 1, "Beta Sdn Bhd", "more goods", 200.25],
])
df = parse_purchase_listing(p)
check("serial dates decoded", df.iloc[0]["date"] == serial, str(df.iloc[0]["date"]))
check("numeric amounts kept", abs(df["amount"].sum() - 300.75) < 0.001)

# 2. Repeated header rows mid-sheet (page-break style)
p = make("repeated_headers.xlsx", [
    ["Tarikh", "Pembekal", "Perkara", "Jumlah"],
    ["1/6/2026", "Kedai A", "barang", "50.00"],
    ["Tarikh", "Pembekal", "Perkara", "Jumlah"],   # printed again
    ["2/6/2026", "Kedai B", "barang lagi", "60.00"],
])
df = parse_purchase_listing(p)
check("repeated headers dropped", len(df) == 2, f"got {len(df)}")

# 3. Credit/kredit column -> negative amounts (credit notes)
p = make("credit_column.xlsx", [
    ["Date", "Supplier", "Detail", "Debit", "Kredit"],
    ["3/6/2026", "Gamma Supplies", "purchase", "500.00", None],
    ["4/6/2026", "Gamma Supplies", "return damaged goods", None, "80.00"],
])
df = parse_purchase_listing(p)
check("debit row positive", df.iloc[0]["amount"] == 500.00)
check("kredit row negative", df.iloc[1]["amount"] == -80.00, str(df.iloc[1]["amount"]))

# 4. Merged-cell supplier (name written once for a run of lines)
p = make("merged_supplier.xlsx", [
    ["Date", "Supplier", "Item", "Amount"],
    ["5/6/2026", "Delta Hardware", "cement", "120.00"],
    ["5/6/2026", None, "sand", "45.00"],
    ["5/6/2026", None, "wire", "30.00"],
    ["6/6/2026", "Epsilon Mart", "drinks", "25.00"],
])
df = parse_purchase_listing(p)
check("merged supplier filled down",
      list(df["supplier"]) == ["Delta Hardware", "Delta Hardware",
                               "Delta Hardware", "Epsilon Mart"])

# 5. TOTAL rows skipped AND reconciled (integrity check)
p = make("with_total.xlsx", [
    ["Date", "Supplier", "Desc", "Amount"],
    ["7/6/2026", "Zeta", "x", "10.00"],
    ["8/6/2026", "Eta", "y", "15.50"],
    ["TOTAL", None, None, "25.50"],
])
df = parse_purchase_listing(p)
check("total row excluded from lines", len(df) == 2)
check("declared total captured", df.attrs["declared_totals"] == [25.50])
check("parsed ties to declared", abs(df.attrs["parsed_total"] - 25.50) < 0.001)

# 6. Total row that does NOT tie (a missed-row situation must be surfaced)
p = make("bad_total.xlsx", [
    ["Date", "Supplier", "Desc", "Amount"],
    ["7/6/2026", "Zeta", "x", "10.00"],
    ["TOTAL", None, None, "99.99"],
])
combined, notes = parse_workbook(p)
check("total mismatch surfaced in notes",
      any("verify no rows were missed" in n for n in notes), str(notes))

# 7. RM prefixes, thousand commas, parentheses negatives, whitespace headers
p = make("dirty_amounts.xlsx", [
    ["  Date ", " Supplier Name", "Description ", " Total (RM) "],
    ["9/6/2026", "Theta Trading", "supplies", "RM 1,234.56"],
    ["10/6/2026", "Theta Trading", "adjustment", "(50.00)"],
])
df = parse_purchase_listing(p)
check("RM + commas cleaned", df.iloc[0]["amount"] == 1234.56)
check("parentheses negative", df.iloc[1]["amount"] == -50.00)

# 8. Header not on first row + fully blank columns between data
p = make("offset_blankcols.xlsx", [
    [None, None, None, None, None, None],
    ["CV JAYA - REKOD BELIAN", None, None, None, None, None],
    [None, "Tarikh", None, "Kedai", "Barang", "Jumlah RM"],
    [None, "11/6/2026", None, "Iota Motor", "servis lori", "380.00"],
])
df = parse_purchase_listing(p)
check("offset header + blank cols", len(df) == 1 and df.iloc[0]["amount"] == 380.00)

# 9. CSV input path
csv_p = TMP / "book.csv"
pd.DataFrame([
    ["Date", "Supplier", "Desc", "Amount"],
    ["12/6/2026", "Kappa Ent", "misc", "77.70"],
]).to_csv(csv_p, index=False, header=False)
df, _ = parse_workbook(csv_p)
check("csv parsed", len(df) == 1 and df.iloc[0]["amount"] == 77.70)

# 10. Multi-sheet workbook: order preserved, sheets tagged
p = TMP / "multi.xlsx"
with pd.ExcelWriter(p) as xw:
    pd.DataFrame([["Date", "Supplier", "Desc", "Amount"],
                  ["1/6/2026", "S1", "a", "10.00"]]).to_excel(
        xw, sheet_name="A", index=False, header=False)
    pd.DataFrame([["Date", "Supplier", "Desc", "Amount"],
                  ["2/6/2026", "S2", "b", "20.00"]]).to_excel(
        xw, sheet_name="B", index=False, header=False)
df, notes = parse_workbook(p)
check("multi-sheet combined", len(df) == 2 and set(df["source_sheet"]) == {"A", "B"})

print(f"\nALL {passed} TORTURE CHECKS PASSED")
