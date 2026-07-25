"""Partial-posting protection: when the Agent posts SOME documents and then
the batch fails, the succeeded documents must enter the duplicate guard -
otherwise re-approving the failed batch double-posts them into a live
ledger (the worst failure mode this system can have).

Simulates the Agent's report directly against the API:
upload -> code -> approve -> poll (dispatch) -> report ok=False with a
'posted' subset -> a re-upload of the same rows must flag DUP_POSTED for
exactly the subset that went in, and nothing else.

Run:  python scripts/test_partial_post.py
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "client_data" / "DEMO_CLIENT"
SAMPLE = ROOT / "inbox" / "june_purchases_MAJU_JAYA.xlsx"

for f in ("rules.json", "posted_registry.json", "audit.jsonl", "file_log.json"):
    p = DATA / f
    if p.exists():
        p.unlink()
if (ROOT / "batches").exists():
    shutil.rmtree(ROOT / "batches")

import os
os.environ["KIRA_AGENT_TOKEN"] = "dev-agent-token-change-me"
os.environ["KIRA_SERVER_URL"] = "http://testserver"
os.environ["KIRA_FIRM_TOKEN"] = "dev-firm-token-change-me"

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

api = TestClient(server.app)
FIRM = {"Authorization": "Bearer dev-firm-token-change-me"}
AGENT = {"Authorization": "Bearer dev-agent-token-change-me"}

CODING = {
    "Ampang Hardware": ("300-A001", "610-000", "NR"),
    "ampang hardware sdn bhd": ("300-A001", "610-000", "NR"),
    "kedai ah seng": ("300-K004", "908-000", "NR"),
    "Kedai Ah Seng": ("300-K004", "908-000", "NR"),
    "City Petrol": ("300-C003", "903-000", "NR"),
    "City Petrol Station": ("300-C003", "903-000", "NR"),
    "Maxis": ("300-M005", "904-000", "P"),
    "TNB": ("300-T006", "905-000", "PE"),
    "Best Office Supplies": ("300-B002", "902-000", "P"),
    "Percetakan Maju": ("300-S007", "906-000", "P"),
}

with SAMPLE.open("rb") as f:
    r = api.post("/api/clients/DEMO_CLIENT/upload", headers=FIRM,
                 files=[("files", (SAMPLE.name, f, "application/octet-stream"))])
bid = r.json()["batch_id"]
detail = api.get(f"/api/batches/{bid}", headers=FIRM).json()
for row in detail["rows"]:
    sc, ac, tc = CODING[row["supplier"]]
    row.update(supplier_code=sc, account_code=ac, tax_code=tc)
r = api.post(f"/api/batches/{bid}/approve", headers=FIRM,
             json={"rows": detail["rows"]})
assert r.status_code == 200, r.text

job = api.post("/api/agent/poll", headers=AGENT, json={
    "agent_name": "partial-test", "clients": ["DEMO_CLIENT"],
    "modes": {"DEMO_CLIENT": "live"}}).json()
assert job["batch_id"] == bid

# the Agent "posted" only the first row's document, then the batch failed
first = detail["rows"][0]
pair = (first["supplier_code"], first["doc_no"])
n_pair_rows = sum(1 for x in detail["rows"]
                  if (x["supplier_code"], x["doc_no"]) == pair)
r = api.post("/api/agent/report", headers=AGENT, json={
    "batch_id": bid, "ok": False, "mode": "live", "invoices": 1,
    "errors": ["simulated crash after first document"],
    "posted": [{"supplier_code": pair[0], "doc_no": pair[1]}]})
assert r.status_code == 200 and r.json()["state"] == "failed"
print(f"[report] batch failed AFTER posting {pair} "
      f"({n_pair_rows} row(s) of that document)")

# re-upload the same rows (new bytes): ONLY the posted document may be
# flagged as already posted - the other rows must stay clean to redo.
import tempfile  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
wb = load_workbook(SAMPLE)
wb["NOTES"]["B9"] = "partial-redo"
redo = Path(tempfile.mkdtemp()) / "redo.xlsx"
wb.save(redo)
with redo.open("rb") as f:
    r = api.post("/api/clients/DEMO_CLIENT/upload", headers=FIRM,
                 files=[("files", (redo.name, f, "application/octet-stream"))])
redo_bid = r.json()["batch_id"]
redo_detail = api.get(f"/api/batches/{redo_bid}", headers=FIRM).json()
dup_rows = [i for i in redo_detail["issues"] if i["code"] == "DUP_POSTED"]
assert len(dup_rows) == n_pair_rows, \
    f"expected exactly {n_pair_rows} DUP_POSTED (the posted document), " \
    f"got {len(dup_rows)}: {dup_rows}"
print(f"[dup-guard] redo flags exactly the {n_pair_rows} already-posted "
      "row(s); the rest stay postable  OK")

print("\nPartial-posting protection verified.")
