"""Smoke test for the remote-console layer: exercises kira/api_client.py
against a really-running Kira Cloud (uvicorn on :8600) — the exact same
calls the console makes when KIRA_API_URL is set."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Defaults target a local dev server; set the env vars first to smoke-test a
# real deployment (e.g. Render).
os.environ.setdefault("KIRA_API_URL", "http://localhost:8600")
os.environ.setdefault("KIRA_FIRM_TOKEN", "dev-firm-token-change-me")

from kira.api_client import KiraAPI
from kira.batches import records_to_df, rows_to_records

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "inbox" / "june_purchases_MAJU_JAYA.xlsx"

api = KiraAPI.from_env()

# wait for server
for _ in range(30):
    try:
        h = api.health()
        break
    except Exception:
        time.sleep(0.5)
else:
    raise SystemExit("server did not come up")
print(f"[health] ok={h['ok']} clients={h['clients']} ai={h['ai']}")

clients = api.clients()
assert any(c["name"] == "DEMO_CLIENT" for c in clients)
print(f"[clients] {[(c['name'], c['suppliers'], c['rules']) for c in clients]}")

# upload through the client (console Convert tab, remote mode)
import openpyxl  # noqa: E402
import tempfile  # noqa: E402
wb = openpyxl.load_workbook(SAMPLE)
wb["NOTES"]["B7"] = f"remote-test-{time.time()}"   # unique bytes
tmp = Path(tempfile.mkdtemp()) / "remote_upload.xlsx"
wb.save(tmp)

res = api.upload("DEMO_CLIENT", [(tmp.name, tmp.read_bytes())])
assert res.get("batch_id"), res
bid = res["batch_id"]
print(f"[upload] batch {bid}: {res['lines']} lines, {res['errors']} errors")

# inbox flow (console Inbox tab, remote mode)
pending = api.batches(state="review")
assert any(p["batch_id"] == bid for p in pending)
b = api.batch(bid)
rows_df = records_to_df(b["rows"])

# approve as-received: with AI off the codes are blank -> conflict surfaced;
# with AI on (deployed key) the codes are real -> approve succeeds.
res = api.approve(bid, rows_to_records(rows_df))
if res.get("_conflict"):
    print(f"[approve] dirty conflict surfaced: blank_codes={res['blank_codes']}")
    res = api.reject(bid, "remote smoke test")
    assert res["state"] == "rejected"
    print("[reject] ok")
else:
    assert res["state"] == "approved", res
    n_coded = int((rows_df["account_code"] != "").sum())
    print(f"[approve] AI coded {n_coded}/{len(rows_df)} lines -> approved "
          "(an Agent poll will post it)")

# dashboards
ov = api.overview()
print(f"[overview] queue={ov['queue']}")
hist = api.history("DEMO_CLIENT")
print(f"[history] stats={hist['stats']} rules={len(hist['rules'])}")
agents = api.agents()
print(f"[agents] {list(agents.keys())}")

print("\nREMOTE CONSOLE LAYER OK")
