"""harmonize_categories(): same category must post to the same account
within a batch. First live batch (2026-07-25) coded BEER to 500-000 on
some days and 610-P01 (a purchases-series code!) on others - independent
AI chunks each made a locally-plausible but globally-inconsistent pick.

Run:  python scripts/test_harmonize.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from kira.classify import harmonize_categories

df = pd.DataFrame([
    # BEER coded 3 ways across "chunks": 500-000 twice, 610-P01 once
    {"supplier": "", "description": "BEER", "doc_type": "journal",
     "account_code": "500-000", "confidence": "high", "reason": "r1"},
    {"supplier": "", "description": "BEER", "doc_type": "journal",
     "account_code": "610-P01", "confidence": "high", "reason": "r2"},
    {"supplier": "", "description": "BEER", "doc_type": "journal",
     "account_code": "500-000", "confidence": "high", "reason": "r3"},
    # CASH SALES consistent already - untouched
    {"supplier": "", "description": "CASH SALES", "doc_type": "journal",
     "account_code": "320-C01", "confidence": "high", "reason": "r4"},
    # a line WITH a party is never harmonized (real supplier bills can
    # legitimately hit different accounts)
    {"supplier": "Ampang Hardware", "description": "BEER",
     "doc_type": "purchase", "account_code": "610-P01",
     "confidence": "high", "reason": "r5"},
    # blank account codes are never used as votes nor overwritten
    {"supplier": "", "description": "BEER", "doc_type": "journal",
     "account_code": "", "confidence": "low", "reason": "r6"},
])

out = harmonize_categories(df.copy())

beer = out[(out["supplier"] == "") & (out["description"] == "BEER")
           & (out["account_code"] != "")]
assert set(beer["account_code"]) == {"500-000"}, beer
print("1. BEER harmonized to the majority account 500-000  OK")

changed = out.iloc[1]
assert changed["confidence"] == "medium" and "harmonized" in changed["reason"]
assert out.iloc[0]["confidence"] == "high", "majority lines keep confidence"
print("2. overridden line flagged for the reviewer (medium + reason)  OK")

assert out.iloc[3]["account_code"] == "320-C01"
assert out.iloc[4]["account_code"] == "610-P01", \
    "lines WITH a party must never be harmonized"
assert out.iloc[5]["account_code"] == "", "blank codes stay blank"
print("3. consistent lines, party lines, and blanks untouched  OK")

print("\nAll harmonize checks passed.")
