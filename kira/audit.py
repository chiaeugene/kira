"""Append-only audit trail per client.

Two things live here:
  1. Accountability — who approved what, when, and what was posted.
  2. The moat — every human correction is logged as training signal.

Events are JSON lines in client_data/<client>/audit.jsonl.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd


class AuditLog:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "audit.jsonl"

    def _write(self, event: dict) -> None:
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def log_batch(self, source_file: str, result: dict, n_lines: int,
                  total: float, n_corrections: int, issues: dict) -> None:
        self._write({
            "event": "batch_posted",
            "source_file": source_file,
            "mode": result["mode"],
            "invoices": result["invoices"],
            "lines": n_lines,
            "total_rm": round(total, 2),
            "corrections": n_corrections,
            "issues": issues,
            "errors": len(result.get("errors", [])),
            "payload": result.get("payload", ""),
        })

    def log_correction(self, source_row: int, supplier: str, field: str,
                       old: str, new: str, ai_source: str) -> None:
        self._write({
            "event": "correction",
            "source_row": source_row,
            "supplier": supplier,
            "field": field,
            "from": str(old),
            "to": str(new),
            "ai_source": ai_source,
        })

    def read(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame()
        records = [json.loads(line) for line in
                   self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return pd.DataFrame(records)

    def stats(self) -> dict:
        df = self.read()
        if df.empty:
            return {"batches": 0, "lines": 0, "total_rm": 0.0,
                    "corrections": 0, "accuracy": None}
        batches = df[df["event"] == "batch_posted"]
        corrections = df[df["event"] == "correction"]
        lines = int(batches["lines"].sum()) if not batches.empty else 0
        n_corr = len(corrections)
        accuracy = None
        if lines:
            accuracy = round(max(0.0, min(1.0, 1 - n_corr / lines)), 3)
        return {
            "batches": len(batches),
            "lines": lines,
            "total_rm": float(batches["total_rm"].sum()) if not batches.empty else 0.0,
            "corrections": n_corr,
            # share of lines the human did NOT have to touch
            "accuracy": accuracy,
        }
