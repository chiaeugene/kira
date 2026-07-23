"""End-to-end tests: parse (multi-sheet) -> classify -> validate -> approve ->
learn -> post -> dedup guard -> flywheel -> majority-vote rules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kira.classify import classify
from kira.context import load_client_context
from kira.ingest import parse_workbook
from kira.poster import PostedRegistry, SQLConfig, post_batch
from kira.rules import RuleStore
from kira.validate import summarize, validate_batch

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "client_data" / "DEMO_CLIENT"
SAMPLE = ROOT / "inbox" / "june_purchases_MAJU_JAYA.xlsx"

# fresh state for a deterministic test
for f in ("rules.json", "posted_registry.json", "audit.jsonl"):
    p = DATA / f
    if p.exists():
        p.unlink()

ctx = load_client_context("DEMO_CLIENT", DATA)
store = RuleStore(DATA)
registry = PostedRegistry(DATA)

# 1. Parse the whole messy workbook (3 sheets: JUN, PETTY CASH, NOTES)
from kira.batches import ensure_row_ids
df, notes = parse_workbook(SAMPLE)
df = ensure_row_ids(df)
print(f"[parse] {len(df)} rows from sheets: {notes}")
assert len(df) == 11, f"expected 11 rows (9 JUN + 2 petty cash), got {len(df)}"
assert any("NOTES" in n and "skipped" in n for n in notes), "NOTES sheet should be skipped"
assert abs(df["amount"].sum() - 2385.65) < 0.01, df["amount"].sum()

# 2. Classify (offline fallback without API key)
coded = classify(df, ctx, store)
print(f"[classify] sources: {coded['source'].value_counts().to_dict()}")

# 3. Simulate bookkeeper coding (what the UI edit step does)
approved_coding = {
    "Ampang Hardware": ("300-A001", "610-000", "NR"),
    "kedai ah seng": ("300-K004", "908-000", "NR"),
    "Kedai Ah Seng": ("300-K004", "908-000", "NR"),
    "City Petrol": ("300-C003", "903-000", "NR"),
    "City Petrol Station": ("300-C003", "903-000", "NR"),
    "Maxis": ("300-M005", "904-000", "P"),
    "TNB": ("300-T006", "905-000", "PE"),
    "Best Office Supplies": ("300-B002", "902-000", "P"),
    "Percetakan Maju": ("300-S007", "906-000", "P"),
    "ampang hardware sdn bhd": ("300-A001", "610-000", "NR"),
}
for i, row in coded.iterrows():
    sc, ac, tc = approved_coding[row["supplier"]]
    coded.loc[i, ["supplier_code", "account_code", "tax_code"]] = [sc, ac, tc]

# 4. Validate — clean batch should have no errors
issues = validate_batch(coded, ctx, registry.keys)
counts = summarize(issues)
print(f"[validate] clean batch: {counts}")
assert counts["error"] == 0, issues[issues.severity == "error"]

# 4b. Validation catches garbage: bad codes, dup rows, absurd tax, future date
import datetime as dt
bad = coded.copy()
bad.loc[0, "account_code"] = "999-XXX"          # unknown account
bad.loc[1, "tax"] = bad.loc[1, "amount"] + 10   # tax > amount
bad.loc[2, "date"] = dt.date.today() + dt.timedelta(days=90)  # future
dup = bad.iloc[[3]].copy()                       # duplicate of row 3
bad = __import__("pandas").concat([bad, dup], ignore_index=True)
issues_bad = validate_batch(bad, ctx, registry.keys)
codes_found = set(issues_bad["code"])
print(f"[validate] dirty batch issues: {sorted(codes_found)}")
for expected in ("UNKNOWN_ACCOUNT", "TAX_EXCEEDS_AMOUNT", "DATE_FUTURE", "DUP_IN_BATCH"):
    assert expected in codes_found, f"missing check: {expected}"

# 4c. Suggested repairs: Kira proposes concrete fixes for the dirty batch
# (re-id after the concat above — real ingestion always ids after combining)
from kira.repairs import apply_fixes, propose_fixes
bad2 = ensure_row_ids(bad.copy())
bad2.loc[0, "supplier_code"] = "300-ZZZZ"   # unknown supplier w/ fuzzy match
issues_bad2 = validate_batch(bad2, ctx, registry.keys)
fixes = propose_fixes(bad2, issues_bad2, ctx)
fixed_fields = set(fixes["field"])
print(f"[repairs] {len(fixes)} proposals covering fields: {sorted(fixed_fields)}")
assert "__drop__" in fixed_fields          # duplicate line removal
assert "tax" in fixed_fields               # impossible tax cleared
assert "date" in fixed_fields              # future date corrected
sup_fix = fixes[(fixes["field"] == "supplier_code")
                & (fixes["row_id"] == int(bad2.loc[0, "row_id"]))]
assert not sup_fix.empty and sup_fix.iloc[0]["proposed"] == "300-A001", sup_fix
repaired = apply_fixes(bad2, fixes)
issues_after = validate_batch(repaired, ctx, registry.keys)
after = summarize(issues_after)
before = summarize(issues_bad2)
print(f"[repairs] errors before={before['error']} after={after['error']}")
assert after["error"] < before["error"]
assert len(repaired) == len(bad2) - 1      # duplicate dropped

# 5. Learn + post (dry run, recorded in registry)
for _, r in coded.iterrows():
    store.learn(r["supplier"], r["supplier_code"], r["account_code"], r["tax_code"])
store.save()
result = post_batch(coded, SQLConfig(dry_run=True), out_dir=ROOT / "posted",
                    registry=registry)
print(f"[post] {result['mode']}: {result['invoices']} invoices, {result['lines']} lines")

# 6. Dedup guard: same batch again must trigger DUP_POSTED on every line
registry2 = PostedRegistry(DATA)
issues_rerun = validate_batch(coded, ctx, registry2.keys)
n_dup = int((issues_rerun["code"] == "DUP_POSTED").sum())
print(f"[dedup] re-submitting the same batch flags {n_dup}/{len(coded)} lines")
assert n_dup == len(coded), "dedup guard failed"

# 7. Flywheel: fresh parse should auto-code 100% from learned rules
coded2 = classify(parse_workbook(SAMPLE)[0], ctx, RuleStore(DATA))
rule_hits = int((coded2["source"] == "rule").sum())
print(f"[flywheel] second pass: {rule_hits}/{len(coded2)} auto-coded from rules")
assert rule_hits == len(coded2)

# 8. Majority-vote rules: one odd correction must not flip an established rule
store3 = RuleStore(DATA)
store3.learn("Ampang Hardware", "300-A001", "907-000", "NR")  # the odd one out
rule = store3.lookup("ampang hardware sdn bhd")
print(f"[rules] majority coding after odd correction: {rule['account_code']} "
      f"({rule['consistency']:.0%} consistent)")
assert rule["account_code"] == "610-000", "majority vote failed"
assert rule["consistency"] < 1.0

# 9. MULTI-MODULE: sales + receipts workbook -> correct doc types end-to-end
SALES = ROOT / "inbox" / "june_sales_MAJU_JAYA.xlsx"
sdf, snotes = parse_workbook(SALES)
sdf = ensure_row_ids(sdf)
print(f"[multi] parsed sales file: {len(sdf)} rows; hints: "
      f"{sdf.groupby('source_sheet')['doc_type_hint'].first().to_dict()}")
assert set(sdf[sdf["source_sheet"] == "SALES JUN"]["doc_type_hint"]) == {"sale"}
assert set(sdf[sdf["source_sheet"] == "RESIT"]["doc_type_hint"]) == {"customer_payment"}

scoded = classify(sdf, ctx, RuleStore(DATA))   # offline: doc_type <- hint
assert list(scoded["doc_type"]) == list(scoded["doc_type_hint"])
print("[multi] fallback classify keeps hinted doc types  OK")

# bookkeeper codes parties (customers!) + accounts (income / bank)
sales_coding = {
    "Delima Construction": ("400-D001",),
    "En. Rahman": ("400-E002",),
    "Fong Brothers": ("400-F003",),
    "Gemilang Cafe": ("400-G004",),
}
for i, row in scoded.iterrows():
    scoded.loc[i, "supplier_code"] = sales_coding[row["supplier"]][0]
    if row["doc_type"] == "sale":
        scoded.loc[i, "account_code"] = "500-000"     # income account
    else:
        scoded.loc[i, "account_code"] = "310-001"     # bank account
    scoded.loc[i, "tax_code"] = "NR"

issues_s = validate_batch(scoded, ctx, registry.keys)
counts_s = summarize(issues_s)
assert counts_s["error"] == 0, issues_s[issues_s.severity == "error"]
print(f"[multi] clean sales batch validates: {counts_s}")

# wrong-master and wrong-account-kind must be caught
bad_s = scoded.copy()
bad_s.loc[0, "supplier_code"] = "300-A001"    # supplier code on a SALE
bad_s.loc[1, "account_code"] = "902-000"      # expense account on a SALE
issues_bad_s = validate_batch(bad_s, ctx, registry.keys)
codes_s = set(issues_bad_s["code"])
assert "UNKNOWN_CUSTOMER" in codes_s, codes_s
assert "ACCOUNT_TYPE_MISMATCH" in codes_s, codes_s
print("[multi] wrong master + wrong account kind caught  OK")

# doc_type missing must block
no_type = scoded.copy()
no_type.loc[0, "doc_type"] = ""
assert "DOC_TYPE_MISSING" in set(validate_batch(no_type, ctx, registry.keys)["code"])
print("[multi] missing doc_type blocked  OK")

# posting groups into the right SQL modules
result_s = post_batch(scoded, SQLConfig(dry_run=True), out_dir=ROOT / "posted",
                      registry=registry)
import json as _json
payload = _json.loads(Path(result_s["payload"]).read_text(encoding="utf-8"))
sql_docs = {inv["sql_doc"] for inv in payload["invoices"]}
assert sql_docs == {"SL_IV", "AR_PM"}, sql_docs
print(f"[multi] dry-run payload routes to modules: {sorted(sql_docs)}  OK")

# doc_type-scoped rules: same party name learns separately per doc type
store_s = RuleStore(DATA)
for _, r in scoded.iterrows():
    store_s.learn(r["supplier"], r["supplier_code"], r["account_code"],
                  r["tax_code"], r["doc_type"])
store_s.save()
rule_sale = store_s.lookup("Delima Construction", "sale")
rule_pay = store_s.lookup("Delima Construction", "customer_payment")
assert rule_sale["account_code"] == "500-000"
assert rule_pay["account_code"] == "310-001"
print("[multi] doc_type-scoped rules learned (sale->500-000, payment->310-001)")

# 10. Add-a-new-client registry functions (local mode path)
from kira.registry import create_client, save_masters
import shutil as _shutil
_shutil.rmtree(ROOT / "client_data" / "TEST_NEW_CO", ignore_errors=True)

d = create_client("TEST_NEW_CO")
assert (d / "suppliers.csv").read_text(encoding="utf-8") == "code,name\n"
print("[registry] create_client makes empty master files with headers")

try:
    create_client("TEST_NEW_CO")
    raise AssertionError("expected FileExistsError")
except FileExistsError:
    print("[registry] duplicate client name rejected  OK")

try:
    create_client("bad name!")
    raise AssertionError("expected ValueError")
except ValueError:
    print("[registry] invalid client name rejected  OK")

saved = save_masters("TEST_NEW_CO", {
    "suppliers.csv": b"code,name\n900-Z,Zeta Supplies\n",
    "../evil.csv": b"code,name\nhack,hack\n",   # must be ignored, not written
})
assert saved == ["suppliers.csv"]
assert not (ROOT / "client_data" / "evil.csv").exists()
ctx2 = load_client_context("TEST_NEW_CO", ROOT / "client_data" / "TEST_NEW_CO")
assert len(ctx2.suppliers) == 1 and ctx2.suppliers.iloc[0]["code"] == "900-Z"
print("[registry] save_masters overwrites the right file, blocks path traversal")

_shutil.rmtree(ROOT / "client_data" / "TEST_NEW_CO", ignore_errors=True)

print("\nALL PIPELINE TESTS PASSED")
