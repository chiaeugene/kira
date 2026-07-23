"""Regression tests for the second field dead-end and its neighbors:

1. Journal lines have no party — the approve gate must NOT demand one
   (it used to, making journal batches impossible to approve ever).
2. Journal lines DO need a contra account: validation flags it, repairs
   propose the client's bank/cash account, the poster refuses one-sided
   entries outright.
3. Master uploads: SQL Accounting's own export headers ("ACC. CODE",
   "COMPANY NAME"...) and Excel files are accepted and normalized;
   garbage is refused at upload with a clear message; a corrupt file on
   disk degrades to empty masters instead of crashing every endpoint.
4. Deleting a client purges its batches — no orphans in the Inbox.

Run:  python scripts/test_journal_masters.py
"""
from __future__ import annotations

import datetime as dt
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from kira.batches import BatchStore, ensure_row_ids
from kira.context import ClientContext, parse_master_upload, _read_csv
from kira.registry import create_client, delete_client, save_masters
from kira.repairs import apply_fixes, propose_fixes
from kira.review import approve_batch
from kira.validate import validate_batch


def ctx_with_masters() -> ClientContext:
    return ClientContext(
        name="T",
        accounts=pd.DataFrame([
            {"code": "310-000", "description": "CASH IN HAND", "type": "CASH"},
            {"code": "500-000", "description": "SALES", "type": "SALES"},
            {"code": "610-000", "description": "PURCHASES", "type": "EXPENSE"},
        ]),
        suppliers=pd.DataFrame([{"code": "400-A01", "name": "Ampang Hardware"}]),
        customers=pd.DataFrame(columns=["code", "name"]),
        tax_codes=pd.DataFrame(columns=["code", "description", "rate"]),
    )


def journal_rows(contra: str) -> pd.DataFrame:
    today = dt.date.today()
    return ensure_row_ids(pd.DataFrame([{
        "source_row": i + 1, "date": today, "supplier": "",
        "description": f"daily takings {i}", "amount": 100.0 + i, "tax": 0.0,
        "doc_no": "", "doc_type": "journal", "supplier_code": "",
        "account_code": "500-000", "contra_account": contra,
        "tax_code": "", "confidence": "high", "source": "test", "reason": "",
        "source_file": "t.xlsx",
    } for i in range(3)]))


ctx = ctx_with_masters()

# --- 1+2. validation: contra missing -> error; contra set -> clean
issues = validate_batch(journal_rows(""), ctx)
assert (issues["code"] == "JOURNAL_NO_CONTRA").sum() == 3, issues
issues_ok = validate_batch(journal_rows("310-000"), ctx)
assert issues_ok[issues_ok["severity"] == "error"].empty, issues_ok
bad = validate_batch(journal_rows("999-XXX"), ctx)
assert (bad["code"] == "UNKNOWN_ACCOUNT").sum() == 3
print("1. journal validation: no-contra flagged, valid contra clean    OK")

# --- 2b. repairs propose the money account, apply fills it
rows = journal_rows("")
fixes = propose_fixes(rows, validate_batch(rows, ctx), ctx)
contra_fixes = fixes[fixes["field"] == "contra_account"]
assert len(contra_fixes) == 3 and set(contra_fixes["proposed"]) == {"310-000"}
repaired = apply_fixes(rows, fixes)
assert (repaired["contra_account"] == "310-000").all()
assert validate_batch(repaired, ctx)[lambda d: d["severity"] == "error"].empty
print("2. repairs: contra defaulted to cash account, applies cleanly   OK")

# --- 3. approve gate via a real (temp) client: journal batch with contra
# and NO party must approve; without contra must be blocked.
tmp_base = Path(tempfile.mkdtemp(prefix="kira_jm_"))
import kira.review as review_mod
import kira.registry as registry_mod
create_client("JM_CO", base=tmp_base)
save_masters("JM_CO", {
    "chart_of_accounts.csv":
        b"ACC. CODE,DESCRIPTION,SPECIAL TYPE\n310-000,CASH IN HAND,CASH\n"
        b"500-000,SALES,SALES\n",
}, base=tmp_base)
_orig_open, _orig_dir = review_mod.open_client, review_mod.client_dir
review_mod.open_client = lambda name: registry_mod.open_client(name, tmp_base)
review_mod.client_dir = lambda name: registry_mod.client_dir(name, tmp_base)
try:
    bs = BatchStore(tmp_base / "batches")
    b = bs.create("JM_CO", ["t.xlsx"], journal_rows("310-000"),
                  validate_batch(journal_rows("310-000"), ctx), [])
    ok, info = approve_batch(bs, b, journal_rows("310-000"))
    assert ok, info
    b2 = bs.create("JM_CO", ["t2.xlsx"], journal_rows(""),
                   validate_batch(journal_rows(""), ctx), [])
    ok2, info2 = approve_batch(bs, b2, journal_rows(""))
    assert not ok2 and info2["blank_codes"] == 3
finally:
    review_mod.open_client, review_mod.client_dir = _orig_open, _orig_dir
print("3. approve gate: partyless journal approves WITH contra only    OK")

# --- 4. master upload normalization: SQL-style headers + Excel + garbage
df = parse_master_upload(
    "suppliers.csv",
    b"Company Code,Company Name\n400-A01,Ampang Hardware Sdn Bhd\n")
assert list(df.columns) == ["code", "name"] and len(df) == 1

buf = io.BytesIO()
pd.DataFrame({"ACC CODE": ["500-000"], "DESCRIPTION": ["SALES"],
              "ACC TYPE": ["SALES"]}).to_excel(buf, index=False)
df = parse_master_upload("chart_of_accounts.csv", buf.getvalue())
assert list(df.columns) == ["code", "description", "type"] and len(df) == 1

try:
    parse_master_upload("suppliers.csv", b"foo,bar\n1,2\n")
    raise AssertionError("garbage headers should be refused")
except ValueError as e:
    assert "could not find" in str(e)
print("4. masters: SQL headers + Excel accepted, garbage refused       OK")

# --- 4b. corrupt file on disk degrades to empty, never raises
p = tmp_base / "broken.csv"
p.write_bytes(b"\x00\x01\x02 not a csv at all")
out = _read_csv(p, ["code", "name"])
assert out.empty and list(out.columns) == ["code", "name"]
print("5. corrupt master on disk -> empty masters, no crash            OK")

# --- 5. purge on delete
bs = BatchStore(tmp_base / "batches2")
bs.create("GONE_CO", ["x.xlsx"], journal_rows("310-000"),
          validate_batch(journal_rows("310-000"), ctx), [])
bs.create("KEEP_CO", ["y.xlsx"], journal_rows("310-000"),
          validate_batch(journal_rows("310-000"), ctx), [])
assert bs.purge_client("GONE_CO") == 1
assert [b["client"] for b in bs.list()] == ["KEEP_CO"]
delete_client("JM_CO", base=tmp_base)
print("6. deleting a client purges its batches, others untouched       OK")

print("\nAll journal + masters regression checks passed.")
