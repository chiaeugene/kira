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
                    "doc_type": {"type": "string",
                                 "enum": ["purchase", "purchase_return", "sale",
                                          "sales_return", "customer_payment",
                                          "supplier_payment", "journal"]},
                    "party": {"type": "string",
                              "description": "The OTHER party on the document "
                                             "(supplier or customer name)"},
                    "description": {"type": "string"},
                    "amount": {"type": "number"},
                    "tax": {"type": "number"},
                    "doc_no": {"type": "string"},
                    "party_tin": {"type": "string", "description": "Tax ID / TIN if printed (e-Invoice)"},
                    "readable": {"type": "string", "enum": ["good", "partial", "poor"]},
                },
                "required": ["date", "doc_type", "party", "description",
                             "amount", "tax", "doc_no", "party_tin", "readable"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["documents"],
    "additionalProperties": False,
}

SYSTEM_PROMPT_TEMPLATE = """You are extracting accounting documents for the Malaysian \
business "{client}" (English / Malay / Chinese, printed or handwritten). One uploaded \
file may contain multiple documents — return one entry per document.

Decide doc_type by DIRECTION relative to "{client}":
- An invoice/bill ISSUED BY someone else TO {client} -> purchase
- An invoice ISSUED BY {client} to its customer -> sale
- A credit note received from a supplier -> purchase_return; issued to a customer -> sales_return
- An official receipt showing {client} RECEIVED money -> customer_payment
- A payment voucher / proof {client} PAID someone -> supplier_payment

'party' is always the OTHER side (the supplier or the customer), never {client}.
Amounts are the TOTAL including tax; 'tax' is the SST amount if itemized, else 0.
Record the party's TIN if printed (needed for LHDN e-Invoice). Mark 'readable'
honestly — 'poor' means a human must check it."""


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def extract_documents(files: list[tuple[str, bytes]],
                      model: str = "claude-opus-4-8",
                      max_tokens: int = 16000,
                      client_name: str = "the business") -> pd.DataFrame:
    """files: list of (filename, raw bytes). Returns canonical rows DataFrame."""
    if not llm_available():
        raise RuntimeError(
            "Document extraction needs the Claude API — set ANTHROPIC_API_KEY."
        )
    import anthropic
    client = anthropic.Anthropic()
    system = SYSTEM_PROMPT_TEMPLATE.format(client=client_name)

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
             "text": f"Extract every accounting document from this file ({name})."},
        ]
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": DOC_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            response = stream.get_final_message()
        text = next(b.text for b in response.content if b.type == "text")
        for d in json.loads(text)["documents"]:
            rows.append({
                "date": pd.to_datetime(d["date"], errors="coerce").date()
                        if d["date"] else None,
                "supplier": d["party"],              # canonical party column
                "description": d["description"],
                "amount": round(float(d["amount"]), 2),
                "tax": round(float(d["tax"]), 2),
                "doc_no": d["doc_no"],
                "doc_type_hint": d["doc_type"],
                "supplier_tin": d["party_tin"],
                "readable": d["readable"],
                "source_row": 1000 + idx,  # synthetic row ref for doc files
                "source_file": name,
            })
    if not rows:
        raise ValueError("No accounting documents recognized in the uploaded files.")
    return pd.DataFrame(rows)
