"""Classification: map each purchase row to this client's supplier code,
expense account code, and tax code.

Order of authority per row:
  1. Learned rule (exact normalized supplier match)  -> high confidence
  2. Claude (grounded in the client's real COA/suppliers/tax codes)
  3. Heuristic fallback (fuzzy supplier match) when no API credentials
"""

from __future__ import annotations

import difflib
import json
import os

import pandas as pd

from .context import ClientContext
from .rules import RuleStore, normalize_supplier

SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_id": {"type": "integer"},
                    "supplier_code": {"type": "string"},
                    "account_code": {"type": "string"},
                    "tax_code": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
                "required": [
                    "row_id", "supplier_code", "account_code",
                    "tax_code", "confidence", "reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an expert Malaysian bookkeeper coding purchase transactions \
into SQL Accounting for a specific client. You are given the client's actual chart of \
accounts, supplier master, and tax codes. For each transaction row, choose:
- supplier_code: the best match from the supplier master ("" if no plausible match — a new supplier)
- account_code: the most appropriate expense/purchase account FROM THE CLIENT'S LIST ONLY
- tax_code: the most appropriate tax code from the client's list
- confidence: high (certain), medium (plausible), low (guessing / needs human review)
- reason: one short sentence

Descriptions may be in English, Malay, or Chinese. Never invent codes that are not in \
the provided lists. If unsure, pick your best candidate and mark confidence low."""


def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _classify_batch_llm(client, model: str, max_tokens: int,
                        context_block: str, rows: list[dict]) -> dict[int, dict]:
    rows_text = "\n".join(
        f"row_id={r['row_id']} | date={r['date']} | supplier={r['supplier']} | "
        f"desc={r['description']} | amount={r['amount']} | tax={r['tax']}"
        for r in rows
    )
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT + "\n\n" + context_block,
            "cache_control": {"type": "ephemeral"},
        }],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": "Code these purchase rows:\n" + rows_text}],
    ) as stream:
        response = stream.get_final_message()
    text = next(b.text for b in response.content if b.type == "text")
    return {r["row_id"]: r for r in json.loads(text)["rows"]}


def _fuzzy_supplier(name: str, ctx: ClientContext) -> tuple[str, float]:
    """Heuristic fallback: fuzzy-match supplier name against the master."""
    if ctx.suppliers.empty or not name:
        return "", 0.0
    target = normalize_supplier(name)
    best_code, best_score = "", 0.0
    for _, r in ctx.suppliers.iterrows():
        score = difflib.SequenceMatcher(
            None, target, normalize_supplier(r["name"])
        ).ratio()
        if score > best_score:
            best_code, best_score = r["code"], score
    return (best_code, best_score) if best_score >= 0.75 else ("", best_score)


def classify(df: pd.DataFrame, ctx: ClientContext, store: RuleStore,
             model: str = "claude-opus-4-8", batch_size: int = 20,
             max_tokens: int = 16000) -> pd.DataFrame:
    """Return df with added columns: supplier_code, account_code, tax_code,
    confidence, source, reason."""
    out = df.copy().reset_index(drop=True)
    for col in ("supplier_code", "account_code", "tax_code", "confidence", "source", "reason"):
        out[col] = ""

    # Pass 1 — learned rules
    pending: list[int] = []
    for i, row in out.iterrows():
        rule = store.lookup(row["supplier"])
        if rule:
            out.loc[i, ["supplier_code", "account_code", "tax_code"]] = [
                rule["supplier_code"], rule["account_code"], rule["tax_code"]]
            conf = "high" if rule.get("consistency", 1.0) >= 0.8 else "medium"
            out.loc[i, ["confidence", "source", "reason"]] = [
                conf, "rule",
                f"learned rule (seen {rule['count']}x, "
                f"{rule.get('consistency', 1.0):.0%} consistent)"]
        else:
            pending.append(i)

    if not pending:
        return out

    # Pass 2 — Claude, grounded in this client's masters
    if _llm_available():
        import anthropic
        client = anthropic.Anthropic()
        context_block = ctx.as_prompt_block()
        batch_rows = [
            {"row_id": i, **out.loc[i, ["date", "supplier", "description",
                                        "amount", "tax"]].to_dict()}
            for i in pending
        ]
        for start in range(0, len(batch_rows), batch_size):
            chunk = batch_rows[start:start + batch_size]
            results = _classify_batch_llm(client, model, max_tokens, context_block, chunk)
            for r in chunk:
                res = results.get(r["row_id"])
                if res is None:
                    continue
                i = r["row_id"]
                out.loc[i, ["supplier_code", "account_code", "tax_code"]] = [
                    res["supplier_code"], res["account_code"], res["tax_code"]]
                out.loc[i, ["confidence", "source", "reason"]] = [
                    res["confidence"], "llm", res["reason"]]
        return out

    # Pass 3 — offline heuristic fallback (no API credentials)
    for i in pending:
        code, score = _fuzzy_supplier(out.loc[i, "supplier"], ctx)
        out.loc[i, "supplier_code"] = code
        out.loc[i, ["confidence", "source"]] = ["low", "fallback"]
        out.loc[i, "reason"] = (
            f"offline fallback, fuzzy supplier match {score:.0%} — set "
            "ANTHROPIC_API_KEY for AI coding"
        )
    return out
