"""Kira Cloud — the server.

Run:  uvicorn server:app --host 0.0.0.0 --port 8600

Intake (firm token):
  POST /api/clients/{client}/upload      multipart files (xlsx/csv/pdf/images)
  GET  /api/batches?client=&state=
  GET  /api/batches/{bid}
  POST /api/batches/{bid}/approve        body: {"rows": [...edited rows...]}
  POST /api/batches/{bid}/reject         body: {"reason": "..."}
  GET  /api/agents                       agent heartbeats (Connections tab)
  GET  /api/firm/overview

Channel intake (webhook bridges; sender is mapped to a client):
  POST /api/intake/whatsapp?phone=...    client_data/phone_map.yaml
  POST /api/intake/telegram?chat_id=...  client_data/telegram_map.yaml
  (run telegram_bot.py to bridge a real Telegram bot to this endpoint)

Agent protocol (agent token, outbound polling — no inbound ports at the firm):
  POST /api/agent/poll                   heartbeat + next approved batch
  POST /api/agent/report                 {batch_id, ok, posted, errors}

Tokens live in config.yaml under server:. This is pilot-grade auth — replace
with real accounts/TLS before public deployment.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pandas as pd
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile

from kira.batches import BatchStore, records_to_df, source_channel
from kira.classify import classify
from kira.documents import MEDIA_TYPES, extract_documents, llm_available
from kira.filelog import FileLog
from kira.ingest import parse_workbook
from kira.poster import PostedRegistry, _rows_to_invoices
from kira.registry import client_dir, firm_overview, list_clients, open_client
from kira.review import approve_batch, reject_batch
from kira.validate import validate_batch

import os

CONFIG = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
LLM = CONFIG["llm"]
SRV = CONFIG.get("server", {})
# Production overrides (Render/env) — no code edits needed at deploy time
SRV["firm_token"] = os.environ.get("KIRA_FIRM_TOKEN", SRV.get("firm_token"))
SRV["agent_token"] = os.environ.get("KIRA_AGENT_TOKEN", SRV.get("agent_token"))

app = FastAPI(title="Kira Cloud", version="0.3")
store = BatchStore()

EXCEL_EXT = {".xlsx", ".xls", ".csv"}
AGENTS_STATUS = Path("batches") / "agents_status.json"


def _token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip()


def firm_auth(request: Request) -> None:
    if _token(request) != SRV.get("firm_token"):
        raise HTTPException(401, "invalid firm token")


def agent_auth(request: Request) -> None:
    if _token(request) != SRV.get("agent_token"):
        raise HTTPException(401, "invalid agent token")


def _require_client(client: str) -> None:
    if client not in list_clients():
        raise HTTPException(404, f"unknown client '{client}'")


def _map_sender(map_file: str, key: str, channel: str) -> str:
    path = Path("client_data") / map_file
    mapping = (yaml.safe_load(path.read_text(encoding="utf-8"))
               if path.exists() else {}) or {}
    client = mapping.get(key) or mapping.get(str(key))
    if client is None:
        raise HTTPException(404, f"no client mapped to {channel} sender {key}")
    return client


async def _ingest_uploads(client: str, files: list[UploadFile],
                          channel: str) -> tuple[pd.DataFrame, list[str]]:
    filelog = FileLog(client_dir(client))
    frames, notes, docs = [], [], []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        data = await f.read()

        prior = filelog.seen(data)
        if prior:
            notes.append(f"{f.filename}: DUPLICATE FILE — identical content "
                         f"already received as '{prior['file']}' on {prior['ts']} "
                         f"via {prior['channel']}. Skipped.")
            continue

        if ext in EXCEL_EXT:
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(data)
            part, part_notes = parse_workbook(tmp.name)
            part["source_file"] = f.filename
            frames.append(part)
            notes += [f"{f.filename} {n}" for n in part_notes]
            filelog.record(f.filename, data, channel)
        elif ext in MEDIA_TYPES:
            docs.append((f.filename, data))
        else:
            notes.append(f"{f.filename}: unsupported type ({ext or 'no extension'})"
                         " — accepted types: xlsx, xls, csv, pdf, png, jpg, webp."
                         " Skipped.")
    if docs:
        if not llm_available():
            notes.append(f"{len(docs)} document file(s) held — AI extraction "
                         "is off (no ANTHROPIC_API_KEY on the server)")
        else:
            extracted = extract_documents(docs, model=LLM["model"],
                                          max_tokens=LLM["max_tokens"])
            frames.append(extracted)
            notes.append(f"{len(docs)} document file(s) -> {len(extracted)} entries")
            for name, data in docs:
                filelog.record(name, data, channel)
    if not frames:
        raise HTTPException(422, detail={"message": "nothing parseable uploaded",
                                         "notes": notes})
    return pd.concat(frames, ignore_index=True), notes


def _code_and_batch(client: str, raw: pd.DataFrame, notes: list[str],
                    source_files: list[str]) -> dict:
    ctx, rules, _audit = open_client(client)
    registry = PostedRegistry(client_dir(client))
    coded = classify(raw, ctx, rules, model=LLM["model"],
                     batch_size=LLM["batch_size"], max_tokens=LLM["max_tokens"])
    issues = validate_batch(coded, ctx, registry.keys)
    return store.create(client, source_files, coded, issues, notes)


def _summary(batch: dict) -> dict:
    counts = pd.DataFrame(batch["issues"])
    sev = (counts["severity"].value_counts().to_dict()
           if not counts.empty else {})
    return {
        "batch_id": batch["id"], "client": batch["client"],
        "state": batch["state"], "channel": source_channel(batch),
        "lines": len(batch["rows"]), "total_rm": batch["total_rm"],
        "errors": int(sev.get("error", 0)),
        "warnings": int(sev.get("warning", 0)),
        "notes": batch["notes"],
    }


# ------------------------------- intake -------------------------------

@app.post("/api/clients/{client}/upload", dependencies=[Depends(firm_auth)])
async def upload(client: str, files: list[UploadFile]):
    _require_client(client)
    raw, notes = await _ingest_uploads(client, files, "upload")
    batch = _code_and_batch(client, raw, notes, [f.filename for f in files])
    return _summary(batch)


@app.post("/api/intake/whatsapp")
async def whatsapp_intake(file: UploadFile, phone: str = Query(...)):
    client = _map_sender("phone_map.yaml", phone, "WhatsApp")
    raw, notes = await _ingest_uploads(client, [file], "whatsapp")
    batch = _code_and_batch(client, raw, notes, [f"whatsapp:{file.filename}"])
    return _summary(batch)


@app.post("/api/intake/telegram")
async def telegram_intake(file: UploadFile, chat_id: str = Query(...)):
    client = _map_sender("telegram_map.yaml", chat_id, "Telegram")
    raw, notes = await _ingest_uploads(client, [file], "telegram")
    batch = _code_and_batch(client, raw, notes, [f"telegram:{file.filename}"])
    return _summary(batch)


# ---------------------------- review queue ----------------------------

@app.get("/api/batches", dependencies=[Depends(firm_auth)])
def batches(client: str | None = None, state: str | None = None):
    return [_summary(b) for b in store.list(client, state)]


@app.get("/api/batches/{bid}", dependencies=[Depends(firm_auth)])
def batch_detail(bid: str):
    b = store.get(bid)
    if b is None:
        raise HTTPException(404, "no such batch")
    return b


@app.post("/api/batches/{bid}/approve", dependencies=[Depends(firm_auth)])
def approve(bid: str, body: dict):
    b = store.get(bid)
    if b is None:
        raise HTTPException(404, "no such batch")
    if b["state"] != "review":
        raise HTTPException(409, f"batch is {b['state']}, not review")
    rows = records_to_df(body["rows"]) if body.get("rows") else records_to_df(b["rows"])
    ok, info = approve_batch(store, b, rows)
    if not ok:
        raise HTTPException(409, detail=info)
    return _summary(info["batch"])


@app.post("/api/batches/{bid}/reject", dependencies=[Depends(firm_auth)])
def reject(bid: str, body: dict | None = None):
    b = store.get(bid)
    if b is None:
        raise HTTPException(404, "no such batch")
    if b["state"] != "review":
        raise HTTPException(409, f"batch is {b['state']}, not review")
    updated = reject_batch(store, b, (body or {}).get("reason", ""))
    return _summary(updated)


# ----------------------------- Agent protocol -----------------------------

def _record_heartbeat(body: dict) -> None:
    AGENTS_STATUS.parent.mkdir(parents=True, exist_ok=True)
    agents = (json.loads(AGENTS_STATUS.read_text(encoding="utf-8"))
              if AGENTS_STATUS.exists() else {})
    name = body.get("agent_name", "agent")
    agents[name] = {
        "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "clients": body.get("clients", []),
        "modes": body.get("modes", {}),
    }
    AGENTS_STATUS.write_text(json.dumps(agents, indent=2), encoding="utf-8")


@app.post("/api/agent/poll", dependencies=[Depends(agent_auth)])
def agent_poll(body: dict | None = None):
    body = body or {}
    _record_heartbeat(body)
    clients = body.get("clients")
    for b in store.list(state="approved"):
        if clients and b["client"] not in clients:
            continue
        rows = records_to_df(b["rows"])
        batch = store.transition(b["id"], "dispatched",
                                 agent=body.get("agent_name", "agent"))
        return {
            "batch_id": batch["id"],
            "client": batch["client"],
            "total_rm": batch["total_rm"],
            "rows": batch["rows"],
            "invoices": _rows_to_invoices(rows),
        }
    return {"batch_id": None}


@app.post("/api/agent/report", dependencies=[Depends(agent_auth)])
def agent_report(body: dict):
    bid = body["batch_id"]
    b = store.get(bid)
    if b is None:
        raise HTTPException(404, "no such batch")
    if b["state"] != "dispatched":
        raise HTTPException(409, f"batch is {b['state']}, not dispatched")

    ok = bool(body.get("ok"))
    errors = body.get("errors", [])
    _ctx, _rules, audit = open_client(b["client"])
    if ok:
        rows = records_to_df(b["rows"])
        PostedRegistry(client_dir(b["client"])).record(rows)
        state = store.transition(bid, "posted", agent_mode=body.get("mode", "?"))
    else:
        state = store.transition(bid, "failed", error_count=len(errors),
                                 agent_errors=errors)
    audit.log_batch(", ".join(b["source_files"]),
                    {"mode": f"agent:{body.get('mode', '?')}",
                     "invoices": body.get("invoices", 0),
                     "errors": errors, "payload": ""},
                    len(b["rows"]), b["total_rm"],
                    b.get("corrections", 0), {})
    return {"state": state["state"]}


@app.get("/api/agents", dependencies=[Depends(firm_auth)])
def agents_status():
    if not AGENTS_STATUS.exists():
        return {}
    return json.loads(AGENTS_STATUS.read_text(encoding="utf-8"))


@app.get("/api/clients", dependencies=[Depends(firm_auth)])
def clients_list():
    out = []
    for name in list_clients():
        ctx, rules, _a = open_client(name)
        out.append({"name": name, "suppliers": len(ctx.suppliers),
                    "accounts": len(ctx.accounts), "rules": len(rules)})
    return out


def _json_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records (NaN becomes null, numpy types unwrap)."""
    return json.loads(df.to_json(orient="records")) if not df.empty else []


@app.get("/api/clients/{client}/history", dependencies=[Depends(firm_auth)])
def client_history(client: str):
    _require_client(client)
    _ctx, rules, audit = open_client(client)
    log = audit.read()
    batches_log = (_json_records(log[log["event"] == "batch_posted"])
                   if not log.empty else [])
    corrections = (_json_records(log[log["event"] == "correction"])
                   if not log.empty else [])
    rule_rows = []
    for key, entry in rules.rules.items():
        combo, count = max(entry["votes"].items(), key=lambda kv: kv[1])
        sup, acct, tax = combo.split("|")
        rule_rows.append({"supplier": key, "supplier_code": sup,
                          "account_code": acct, "tax_code": tax, "seen": count})
    return {"stats": audit.stats(), "batches": batches_log,
            "corrections": corrections, "rules": rule_rows}


@app.get("/api/firm/overview", dependencies=[Depends(firm_auth)])
def overview():
    rows = firm_overview()
    queue = {s: len(store.list(state=s)) for s in
             ("review", "approved", "dispatched", "posted", "failed", "rejected")}
    return {"clients": rows, "queue": queue}


@app.get("/api/health")
def health():
    return {"ok": True, "clients": len(list_clients()), "ai": llm_available()}
