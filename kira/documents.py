"""Invoice / receipt document intake (PDF, JPG, PNG) via Claude vision.

Turns a pile of scanned or photographed supplier documents into the same
canonical rows the Excel path produces, so everything downstream (classify,
validate, review, post) is shared.

Requires ANTHROPIC_API_KEY. Handles Malay / Chinese / English documents,
handwriting included (best effort, flagged by confidence).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pandas as pd

MEDIA_TYPES = {
    ".pdf": ("document", "application/pdf"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
}

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO date YYYY-MM-DD, '' if unreadable"},
                    "supplier": {"type": "string"},
                    "description": {"type": "string"},
                    "amount": {"type": "number"},
                    "tax": {"type": "number"},
                    "doc_no": {"type": "string"},
                    "supplier_tin": {"type": "string", "description": "Tax ID / TIN if printed (e-Invoice)"},
                    "readable": {"type": "string", "enum": ["good", "partial", "poor"]},
                },
                "required": ["date", "supplier", "description", "amount",
                             "tax", "doc_no", "supplier_tin", "readable"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["documents"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are extracting purchase data from Malaysian supplier invoices \
and receipts (English / Malay / Chinese, printed or handwritten). One uploaded file \
may contain multiple documents (e.g. a PDF of scanned receipts) — return one entry \
per document. Amounts are the TOTAL payable including tax; 'tax' is the SST/service \
tax amount if itemized, else 0. Record the supplier's TIN if printed (needed for \
LHDN e-Invoice). Mark 'readable' honestly — 'poor' means a human must check it."""


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def extract_documents(files: list[tuple[str, bytes]],
                      model: str = "claude-opus-4-8",
                      max_tokens: int = 16000) -> pd.DataFrame:
    """files: list of (filename, raw bytes). Returns canonical rows DataFrame."""
    if not llm_available():
        raise RuntimeError(
            "Document extraction needs the Claude API — set ANTHROPIC_API_KEY."
        )
    import anthropic
    client = anthropic.Anthropic()

    rows = []
    for idx, (name, data) in enumerate(files):
        ext = Path(name).suffix.lower()
        if ext not in MEDIA_TYPES:
            continue
        block_type, media_type = MEDIA_TYPES[ext]
        b64 = base64.standard_b64encode(data).decode()
        content = [
            {"type": block_type,
             "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text",
             "text": f"Extract every purchase document from this file ({name})."},
        ]
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": DOC_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            response = stream.get_final_message()
        text = next(b.text for b in response.content if b.type == "text")
        for d in json.loads(text)["documents"]:
            rows.append({
                "date": pd.to_datetime(d["date"], errors="coerce").date()
                        if d["date"] else None,
                "supplier": d["supplier"],
                "description": d["description"],
                "amount": round(float(d["amount"]), 2),
                "tax": round(float(d["tax"]), 2),
                "doc_no": d["doc_no"],
                "supplier_tin": d["supplier_tin"],
                "readable": d["readable"],
                "source_row": 1000 + idx,  # synthetic row ref for doc files
                "source_file": name,
            })
    if not rows:
        raise ValueError("No purchase documents recognized in the uploaded files.")
    return pd.DataFrame(rows)
