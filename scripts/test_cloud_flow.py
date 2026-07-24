"""End-to-end test of the product architecture:

upload -> Kira Cloud (code+validate, review) -> dirty approve refused ->
clean approve -> Agent polls (heartbeat recorded) -> posts (dry run) ->
reports -> posted + registry -> identical re-upload refused (duplicate file)
-> changed file with same rows flags DUP_POSTED -> Telegram + WhatsApp intake
-> reject flow -> firm overview -> auth.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "client_data" / "DEMO_CLIENT"
SAMPLE = ROOT / "inbox" / "june_purchases_MAJU_JAYA.xlsx"

# fresh state
for f in ("rules.json", "posted_registry.json", "audit.jsonl", "file_log.json"):
    p = DATA / f
    if p.exists():
        p.unlink()
if (ROOT / "batches").exists():
    shutil.rmtree(ROOT / "batches")

import os
# pin test tokens BEFORE agent import — its load_env() would otherwise pull
# the production .env (setdefault semantics: pre-set env always wins)
os.environ["KIRA_AGENT_TOKEN"] = "dev-agent-token-change-me"
os.environ["KIRA_SERVER_URL"] = "http://testserver"
os.environ["KIRA_FIRM_TOKEN"] = "dev-firm-token-change-me"

import server  # noqa: E402
import agent as agent_mod  # noqa: E402

api = TestClient(server.app)
FIRM = {"Authorization": "Bearer dev-firm-token-change-me"}


def variant(tag: str) -> Path:
    """Same transactions, different bytes — simulates a re-export."""
    wb = load_workbook(SAMPLE)
    wb["NOTES"]["B9"] = tag
    out = Path(tempfile.mkdtemp()) / f"june_reexport_{tag}.xlsx"
    wb.save(out)
    return out


def upload(path: Path, name: str | None = None):
    with path.open("rb") as f:
        return api.post("/api/clients/DEMO_CLIENT/upload", headers=FIRM,
                        files=[("files", (name or path.name, f,
                                          "application/octet-stream"))])


# 0. auth
assert api.get("/api/batches").status_code == 401
assert api.post("/api/agent/poll", headers=FIRM, json={}).status_code == 401
print("[auth] endpoints reject bad tokens  OK")

# 1. upload -> review
summary = upload(SAMPLE).json()
bid = summary["batch_id"]
assert summary["state"] == "review" and summary["lines"] == 11
print(f"[upload] batch {bid}: {summary['lines']} lines "
      f"RM {summary['total_rm']:,.2f} channel={summary['channel']}")

# 2. dirty approve refused
detail = api.get(f"/api/batches/{bid}", headers=FIRM).json()
r = api.post(f"/api/batches/{bid}/approve", headers=FIRM,
             json={"rows": detail["rows"]})
assert r.status_code == 409
print(f"[approve] dirty refused: blank_codes={r.json()['detail']['blank_codes']}  OK")

# 2b. recode: re-runs coding with current masters, keeps batch in review
r = api.post(f"/api/batches/{bid}/recode", headers=FIRM)
assert r.status_code == 200 and r.json()["state"] == "review"
recoded = api.get(f"/api/batches/{bid}", headers=FIRM).json()
assert len(recoded["rows"]) == 11
assert {row["row_id"] for row in recoded["rows"]} == \
       {row["row_id"] for row in detail["rows"]}, "row_ids must survive recode"
n_filled = sum(1 for row in recoded["rows"] if row["supplier_code"])
print(f"[recode] batch re-coded in place: {n_filled}/11 parties matched  OK")
detail = recoded

# 3. code + approve
coding = {
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
for row in detail["rows"]:
    sc, ac, tc = coding[row["supplier"]]
    row.update(supplier_code=sc, account_code=ac, tax_code=tc, confidence="high")
r = api.post(f"/api/batches/{bid}/approve", headers=FIRM,
             json={"rows": detail["rows"]})
assert r.status_code == 200 and r.json()["state"] == "approved"
print("[approve] clean batch approved -> queued")

# 4. agent poll -> post -> report (real agent code, in-process transport)
class TestClientAdapter:
    def post(self, url, headers=None, json=None):
        return api.post(url, headers=headers, json=json)


agent_cfg = agent_mod.load_cfg(str(ROOT / "agent_config.yaml"))
assert agent_mod.poll_once(agent_cfg, client=TestClientAdapter()) == "posted"
assert api.get(f"/api/batches/{bid}", headers=FIRM).json()["state"] == "posted"
print("[agent] polled, posted (dry run), reported")

# 4b. heartbeat visible on the Connections surface
agents = api.get("/api/agents", headers=FIRM).json()
assert "office-pc-1" in agents
assert agents["office-pc-1"]["modes"]["DEMO_CLIENT"] == "dry_run"
print(f"[agents] heartbeat: office-pc-1 last_seen={agents['office-pc-1']['last_seen']}")

# 5. identical re-upload refused as duplicate FILE
r = upload(SAMPLE)
assert r.status_code == 422, r.status_code
notes = r.json()["detail"]["notes"]
assert any("DUPLICATE FILE" in n for n in notes)
print("[dedup-file] identical bytes refused at intake  OK")

# 6. re-EXPORTED file (new bytes, same rows) -> caught by DUP_POSTED instead
dup = upload(variant("v1")).json()
assert dup["errors"] >= 11, dup
print(f"[dedup-lines] re-exported file flags {dup['errors']} DUP_POSTED errors  OK")

# 7. Telegram + WhatsApp intake webhooks map sender -> client
v2 = variant("v2")
with v2.open("rb") as f:
    r = api.post("/api/intake/telegram?chat_id=111111111",
                 files={"file": (v2.name, f, "application/octet-stream")})
assert r.status_code == 200 and r.json()["client"] == "DEMO_CLIENT"
assert r.json()["channel"] == "telegram"
tg_bid = r.json()["batch_id"]
print(f"[telegram] chat 111111111 -> DEMO_CLIENT batch {tg_bid}")

v3 = variant("v3")
with v3.open("rb") as f:
    r = api.post("/api/intake/whatsapp?phone=%2B60123456789",
                 files={"file": (v3.name, f, "application/octet-stream")})
assert r.status_code == 200 and r.json()["channel"] == "whatsapp"
print(f"[whatsapp] +60123456789 -> batch {r.json()['batch_id']}")

# unmapped sender is a clean 404
r = api.post("/api/intake/telegram?chat_id=999",
             files={"file": ("x.xlsx", b"zz", "application/octet-stream")})
assert r.status_code == 404
print("[telegram] unmapped chat_id -> 404  OK")

# 7b. repairs endpoint proposes dropping the duplicate lines
r = api.get(f"/api/batches/{dup['batch_id']}/repairs", headers=FIRM)
assert r.status_code == 200
repair_rows = r.json()
assert any(f["field"] == "__drop__" for f in repair_rows), repair_rows[:2]
print(f"[repairs] endpoint proposes {len(repair_rows)} fix(es) "
      "incl. duplicate removal")

# 7c. agent setup scanner finds accounting DBs and SKIPS payroll ones
# (real field feedback: 17 PAY-xxxx files confused the installer)
import tempfile as _tf
from agent import scan_sql_companies
fake = Path(_tf.mkdtemp()) / "eStream"
(fake / "SQLAccounting" / "Share").mkdir(parents=True)
(fake / "SQLAccounting" / "DB").mkdir()
(fake / "SQL Payroll" / "DB").mkdir(parents=True)
(fake / "SQLAccounting" / "Share" / "Default.DCF").write_text("x")
(fake / "SQLAccounting" / "DB" / "ACC-0001.FDB").write_text("x")
(fake / "SQL Payroll" / "DB" / "PAY-0001.FDB").write_text("x")
(fake / "SQL Payroll" / "DB" / "PAY-0002.FDB").write_text("x")
dcfs, fdbs, n_pay = scan_sql_companies([str(fake)])
assert len(dcfs) == 1 and len(fdbs) == 1 and n_pay == 2, (dcfs, fdbs, n_pay)
assert fdbs[0].name == "ACC-0001.FDB"
print(f"[wizard] scanner found {fdbs[0].name}, skipped {n_pay} payroll DBs")

# 8. reject flow (the Telegram batch)
r = api.post(f"/api/batches/{tg_bid}/reject", headers=FIRM,
             json={"reason": "client sent the wrong month"})
assert r.status_code == 200 and r.json()["state"] == "rejected"
print("[reject] telegram batch rejected, kept in history")

# 8b. add-a-new-client flow: create -> upload masters -> appears in list ->
#     agent wizard's fetch sees it -> duplicate name rejected -> bad name rejected
r = api.post("/api/clients", headers=FIRM, json={"name": "NEW_CO"})
assert r.status_code == 200 and r.json()["created"], r.text
print("[add-client] created NEW_CO")

r = api.post(
    "/api/clients/NEW_CO/masters", headers=FIRM,
    files={"suppliers": ("suppliers.csv", b"code,name\n900-X,Test Supplier\n",
                         "text/csv")},
)
assert r.status_code == 200 and "suppliers.csv" in r.json()["saved"]
print("[add-client] uploaded suppliers.csv")

r = api.get("/api/clients", headers=FIRM)
names = {c["name"] for c in r.json()}
assert "NEW_CO" in names
new_co = next(c for c in r.json() if c["name"] == "NEW_CO")
assert new_co["suppliers"] == 1, new_co
print(f"[add-client] NEW_CO now shows {new_co['suppliers']} supplier(s) in "
      "the client list")

# agent's own fetch helper (agent token, not firm token) sees the same list
fetched = agent_mod.fetch_cloud_clients("http://testserver",
                                        "dev-agent-token-change-me")
# real HTTP needed for agent's helper (it uses plain httpx.get, not the
# TestClient) — confirm the endpoint itself accepts the agent token via
# the FastAPI test client directly instead:
r = api.get("/api/clients", headers={"Authorization": "Bearer dev-agent-token-change-me"})
assert r.status_code == 200, r.status_code
print("[add-client] agent token can read the client list (any_auth)  OK")

r = api.post("/api/clients", headers=FIRM, json={"name": "NEW_CO"})
assert r.status_code == 409, r.status_code
print("[add-client] duplicate name rejected  OK")

r = api.post("/api/clients", headers=FIRM, json={"name": "bad name!"})
assert r.status_code == 422, r.status_code
print("[add-client] invalid characters rejected  OK")

# 8c. Agent-driven discovery: register (agent token, not firm token) creates
#     a brand-new client, then a second register with the SAME name links
#     without touching whatever masters got added in between.
AGENT = {"Authorization": "Bearer dev-agent-token-change-me"}
r = api.post("/api/clients/register", headers=AGENT,
             json={"name": "DISCOVERED_CO", "label": "Discovered Sdn Bhd",
                  "fdb_name": "ACC-7777.FDB", "agent_name": "test-office-pc"})
assert r.status_code == 200 and r.json()["created"] is True, r.text
print("[register] agent token creates a new client via discovery")

# firm token cannot use the agent-only endpoint, and vice versa is blocked too
r = api.post("/api/clients/register", headers=FIRM, json={"name": "X"})
assert r.status_code == 401, "register is agent-only, firm token must be rejected"
print("[register] firm token rejected on the agent-only register endpoint  OK")

r = api.post(f"/api/clients/DISCOVERED_CO/masters", headers=FIRM,
             files={"suppliers": ("suppliers.csv",
                                 b"code,name\n1,Real One\n", "text/csv")})
assert r.status_code == 200

# 8d. reverse master feed: the Agent pushes masters read from SQL itself
r = api.post("/api/clients/DISCOVERED_CO/masters/sync", headers=AGENT, json={
    "agent_name": "test-office-pc",
    "masters": {
        "chart_of_accounts.csv": [
            {"code": "310-000", "description": "CASH", "type": "CASH"},
            {"code": "500-000", "description": "SALES", "type": "SALES"}],
        "customers.csv": [{"code": "300-C01", "name": "Walk-in"}],
    }})
assert r.status_code == 200, r.text
assert r.json()["saved"] == {"chart_of_accounts.csv": 2, "customers.csv": 1}
r = api.post("/api/clients/DISCOVERED_CO/masters/sync", headers=FIRM,
             json={"masters": {}})
assert r.status_code == 401, "masters/sync is agent-only"
disc = next(c for c in api.get("/api/clients", headers=FIRM).json()
            if c["name"] == "DISCOVERED_CO")
assert disc["accounts"] == 2 and disc["customers"] == 1, disc
print("[masters-sync] agent pushed SQL masters -> cloud; firm token rejected  OK")

r = api.post("/api/clients/register", headers=AGENT,
             json={"name": "DISCOVERED_CO", "label": "should be ignored",
                  "fdb_name": "ACC-7777.FDB"})
assert r.status_code == 200 and r.json()["created"] is False
r = api.get("/api/clients", headers=FIRM)
disc = next(c for c in r.json() if c["name"] == "DISCOVERED_CO")
assert disc["suppliers"] == 1, disc  # untouched by the second register
print("[register] re-registering links to the existing client, masters kept")

# 8d. delete endpoint (firm-only, irreversible)
r = api.delete("/api/clients/DISCOVERED_CO", headers=FIRM)
assert r.status_code == 200 and r.json()["deleted"], r.text
r = api.get("/api/clients", headers=FIRM)
assert "DISCOVERED_CO" not in {c["name"] for c in r.json()}
r = api.delete("/api/clients/DISCOVERED_CO", headers=FIRM)
assert r.status_code == 404
print("[delete] client removed via API, repeat delete 404s cleanly")

r = api.delete("/api/clients/NEW_CO", headers=AGENT)
assert r.status_code == 401, "delete must be firm-only, agent token rejected"
print("[delete] agent token rejected on the firm-only delete endpoint  OK")

# 8e. wizard helper: slugify turns an extracted company name into a safe id
from agent import _slugify
assert _slugify("Maju Jaya Enterprise Sdn Bhd") == "MAJU_JAYA_ENTERPRISE_SDN_BHD"
assert _slugify("  weird!! name--123  ") == "WEIRD_NAME_123"
print("[wizard] _slugify produces valid client-name strings")

# 9. firm overview
ov = api.get("/api/firm/overview", headers=FIRM).json()
print(f"[overview] queue: {ov['queue']}")
assert ov["queue"] == {"review": 2, "approved": 0, "dispatched": 0,
                       "posted": 1, "failed": 0, "rejected": 1}

# cleanup: remove the test client folder so repeated runs stay deterministic
import shutil as _shutil
_shutil.rmtree(ROOT / "client_data" / "NEW_CO", ignore_errors=True)

print("\nALL CLOUD FLOW TESTS PASSED")
