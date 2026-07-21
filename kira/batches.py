"""Batch lifecycle store — the contract between Kira Cloud and the Agent.

States:
  review     coded + validated, waiting for a human to verify in the Inbox
  approved   human approved; queued for the firm's Agent
  dispatched an Agent has picked it up and is posting
  posted     Agent confirmed everything landed in SQL
  failed     Agent reported errors (partial or total)
  rejected   human rejected in review (kept for the audit trail)

One JSON file per batch under batches/. Nothing is deleted; the queue is
also the permanent record of what moved between cloud and SQL.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pandas as pd

STATES = ("review", "approved", "dispatched", "posted", "failed", "rejected")


def source_channel(batch: dict) -> str:
    """Where a batch came from, derived from its source file tags."""
    src = " ".join(batch.get("source_files", []))
    if src.startswith("telegram:") or " telegram:" in src:
        return "telegram"
    if src.startswith("whatsapp:") or " whatsapp:" in src:
        return "whatsapp"
    return "upload"

ROW_COLS = ["date", "supplier", "description", "amount", "tax", "doc_no",
            "supplier_code", "account_code", "tax_code", "confidence",
            "source", "reason", "source_row"]


def rows_to_records(df: pd.DataFrame) -> list[dict]:
    out = df.copy()
    out["date"] = out["date"].astype(str)
    cols = [c for c in ROW_COLS if c in out.columns]
    return json.loads(out[cols].to_json(orient="records"))


def records_to_df(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["amount"] = df["amount"].astype(float)
    df["tax"] = df["tax"].astype(float)
    df["source_row"] = df["source_row"].astype(int)
    for c in ("supplier_code", "account_code", "tax_code", "doc_no"):
        df[c] = df[c].fillna("").astype(str)
    return df


class BatchStore:
    def __init__(self, base: str | Path = "batches"):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, batch_id: str) -> Path:
        return self.base / f"{batch_id}.json"

    def create(self, client: str, source_files: list[str],
               rows: pd.DataFrame, issues: pd.DataFrame,
               notes: list[str]) -> dict:
        batch = {
            "id": f"b_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}",
            "client": client,
            "state": "review",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_files": source_files,
            "notes": notes,
            "rows": rows_to_records(rows),
            "issues": issues.to_dict(orient="records"),
            "total_rm": round(float(rows["amount"].sum()), 2),
            "history": [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "state": "review"}],
        }
        self._save(batch)
        return batch

    def _save(self, batch: dict) -> None:
        self._path(batch["id"]).write_text(
            json.dumps(batch, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")

    def get(self, batch_id: str) -> dict | None:
        p = self._path(batch_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def list(self, client: str | None = None, state: str | None = None) -> list[dict]:
        out = []
        for p in sorted(self.base.glob("b_*.json")):
            b = json.loads(p.read_text(encoding="utf-8"))
            if client and b["client"] != client:
                continue
            if state and b["state"] != state:
                continue
            out.append(b)
        return out

    def transition(self, batch_id: str, new_state: str, **extra) -> dict:
        if new_state not in STATES:
            raise ValueError(f"unknown state {new_state}")
        batch = self.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        batch["state"] = new_state
        batch.update(extra)
        batch["history"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "state": new_state, **{k: v for k, v in extra.items()
                                                        if k in ("agent", "error_count")}})
        self._save(batch)
        return batch

    def update_rows(self, batch_id: str, rows: pd.DataFrame) -> dict:
        batch = self.get(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        batch["rows"] = rows_to_records(rows)
        batch["total_rm"] = round(float(rows["amount"].sum()), 2)
        self._save(batch)
        return batch
