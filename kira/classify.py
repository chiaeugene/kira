"""Classification: decide what each line IS (doc_type) and where it goes —
party code, account code, tax code — against this client's own masters.

Document types:
  purchase          supplier bill / expense        -> SQL Purchase Invoice
  purchase_return   credit note from a supplier    -> SQL Purchase Return
  sale              invoice issued to a customer   -> SQL Sales Invoice
  sales_return      credit note to a customer      -> SQL Sales Credit Note
  customer_payment  money received from a customer -> SQL Customer Payment
  supplier_payment  money paid to a supplier       -> SQL Supplier Payment
  journal           everything else (adjustments)  -> SQL Journal Entry

Order of authority per row:
  1. Learned rule (doc_type + normalized party)  -> high confidence
  2. Claude (grounded in the client's masters + per-sheet doc-type hints)
  3. Heuristic fallback (hint doc_type + fuzzy party) when no API credentials

Column semantics: `supplier` holds the PARTY NAME (supplier or customer);
`supplier_code` holds the PARTY CODE from the matching master.
`account_code` is the other side of the entry: expense/COGS account for
purchases, income account for sales, bank/cash account for payments.
"""

from __future__ import annotations

import difflib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .context import ClientContext
from .rules import RuleStore, normalize_supplier

DOC_TYPES = ["purchase", "purchase_return", "sale", "sales_return",
             "cash_sale", "customer_payment", "supplier_payment", "journal"]

SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_id": {"type": "integer"},
                    "doc_type": {"type": "string", "enum": DOC_TYPES},
                    "party_code": {"type": "string"},
                    "account_code": {"type": "string"},
                    "contra_account": {"type": "string"},
                    "tax_code": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
                "required": ["row_id", "doc_type", "party_code", "account_code",
                             "contra_account", "tax_code", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an expert Malaysian bookkeeper sorting a client's mixed \
records into SQL Accounting. For each transaction line decide:

1. doc_type — what this line IS:
   - purchase: a supplier bill or expense the business incurred
   - purchase_return: goods returned to a supplier / supplier credit note
   - sale: an invoice the business issued to its customer
   - sales_return: goods returned by a customer / credit note issued
   - cash_sale: daily walk-in takings (POS/outlet summary) — one Cash Sales
     document per day. Lines with a positive amount are revenue categories
     or charges; lines with a NEGATIVE amount are how the money was
     collected (e-wallet, card, bank transfer) and must be coded to that
     payment method's BANK/CASH-type asset account. Cash itself is never a
     line — it is the document's residual total.
   - customer_payment: money RECEIVED from a customer (receipt, collection)
   - supplier_payment: money PAID to a supplier (payment voucher)
   - journal: adjustments that fit none of the above
   A doc_type_hint from the sheet's own title/headers is given when available —
   follow it unless the line clearly contradicts it.

2. party_code — the code of the party FROM THE CORRECT MASTER:
   suppliers/creditors for purchase, purchase_return, supplier_payment;
   customers/debtors for sale, sales_return, customer_payment.
   For cash_sale: the walk-in / cash-sales customer code from the customer
   master (there is usually exactly one, named like "CASH SALES").
   "" if no plausible match (a new party).

3. account_code — the other side of the entry, FROM THE CHART OF ACCOUNTS ONLY:
   expense/cost account for purchases; income/sales account for sales;
   BANK or CASH account for payments (which account the money moved through);
   the adjustment account for journals.

4. contra_account — ONLY for a STANDALONE journal line (no other line shares
   its doc_no): the balancing side of the double entry, from the chart of
   accounts (daily takings/cash sales usually debit a BANK or CASH account
   and credit an income account, so account_code = income account and
   contra_account = bank/cash, or vice versa as the sheet implies).
   When SEVERAL journal lines share the same doc_no, they are already a
   pre-split multi-line entry (e.g. one day's takings broken into revenue,
   tax, and each payment method as separate lines) — leave contra_account
   "" for every line in that group; they balance each other as a group, not
   pairwise. Still pick the correct account_code for each one individually.
   "" for every non-journal line.

5. tax_code — from the client's tax code list ("" if none applies).

6. confidence: high (certain) / medium (plausible) / low (needs human review).
7. reason: one short sentence.

Descriptions may be in English, Malay, or Chinese. Never invent codes. When
unsure of doc_type, prefer the hint; if there is no hint, mark confidence low."""


def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _classify_batch_llm(client, model: str, max_tokens: int,
                        context_block: str, rows: list[dict]) -> dict[int, dict]:
    rows_text = "\n".join(
        f"row_id={r['row_id']} | hint={r.get('doc_type_hint', '') or 'none'} | "
        f"doc_no={r.get('doc_no', '') or 'none'} | "
        f"date={r['date']} | party={r['supplier']} | desc={r['description']} | "
        f"amount={r['amount']} | tax={r['tax']}"
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
        messages=[{"role": "user",
                   "content": "Sort and code these lines:\n" + rows_text}],
    ) as stream:
        response = stream.get_final_message()
    text = next(b.text for b in response.content if b.type == "text")
    return {r["row_id"]: r for r in json.loads(text)["rows"]}


def _party_master(ctx: ClientContext, doc_type: str) -> pd.DataFrame:
    if doc_type in ("sale", "sales_return", "cash_sale", "customer_payment"):
        return ctx.customers
    return ctx.suppliers


def _fuzzy_party(name: str, master: pd.DataFrame) -> tuple[str, float]:
    if master is None or master.empty or not name:
        return "", 0.0
    target = normalize_supplier(name)
    best_code, best_score = "", 0.0
    for _, r in master.iterrows():
        score = difflib.SequenceMatcher(
            None, target, normalize_supplier(r["name"])).ratio()
        if score > best_score:
            best_code, best_score = r["code"], score
    return (best_code, best_score) if best_score >= 0.75 else ("", best_score)


def harmonize_categories(out: pd.DataFrame) -> pd.DataFrame:
    """Same category, same account — within one batch.

    Party-less category lines (daily-takings splits: 'BEER', 'CASH SALES',
    'SST 6%'...) are classified in independent 20-row chunks, and the AI
    can legitimately waver between plausible accounts across chunks — the
    first live batch coded BEER to 500-000 on some days and 610-P01 on
    others (2026-07-25). An accountant would never do that: one category
    posts to one account. Majority vote per (doc_type, DESCRIPTION) among
    party-less lines; overridden lines are marked so the reviewer can see
    what changed and confidence drops to medium (a human should glance).
    """
    mask = ((out["supplier"].astype(str).str.strip() == "")
            & (out["description"].astype(str).str.strip() != "")
            & (out["account_code"].astype(str).str.strip() != ""))
    if not mask.any():
        return out
    key = (out.loc[mask, "doc_type"].astype(str) + "|"
           + out.loc[mask, "description"].astype(str).str.strip().str.upper())
    for k in key.unique():
        idxs = key[key == k].index
        codes = out.loc[idxs, "account_code"].astype(str)
        if codes.nunique() <= 1:
            continue
        winner = codes.mode().iloc[0]
        losers = [i for i in idxs if out.loc[i, "account_code"] != winner]
        for i in losers:
            was = out.loc[i, "account_code"]
            out.loc[i, "account_code"] = winner
            out.loc[i, "confidence"] = "medium"
            out.loc[i, "reason"] = (
                f"harmonized: AI chose {was} here but {winner} for the same "
                f"category elsewhere in this batch — majority wins, please "
                "confirm")
    return out


def classify(df: pd.DataFrame, ctx: ClientContext, store: RuleStore,
             model: str = "claude-opus-4-8", batch_size: int = 20,
             max_tokens: int = 16000) -> pd.DataFrame:
    """Return df with added columns: doc_type, supplier_code (= party code),
    account_code, tax_code, confidence, source, reason."""
    out = df.copy().reset_index(drop=True)
    if "doc_type_hint" not in out.columns:
        out["doc_type_hint"] = ""
    for col in ("doc_type", "supplier_code", "account_code", "contra_account",
                "tax_code", "confidence", "source", "reason"):
        out[col] = ""

    # Pass 1 — learned rules (need a known doc_type to pick the rule space)
    pending: list[int] = []
    for i, row in out.iterrows():
        hint = str(row.get("doc_type_hint", "") or "")
        rule = store.lookup(row["supplier"], hint) if hint else None
        if rule:
            out.loc[i, ["doc_type", "supplier_code", "account_code",
                        "tax_code"]] = [hint, rule["supplier_code"],
                                        rule["account_code"], rule["tax_code"]]
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
        cols = ["date", "supplier", "description", "amount", "tax",
                "doc_type_hint", "doc_no"]
        batch_rows = [{"row_id": i, **out.loc[i, cols].to_dict()}
                      for i in pending]
        chunks = [batch_rows[start:start + batch_size]
                 for start in range(0, len(batch_rows), batch_size)]

        # Chunks are independent (each is its own Claude call) - run them
        # concurrently instead of one after another. A large batch (e.g. a
        # month of daily-takings lines, ~10 chunks) run sequentially can
        # take long enough to hit a hosting platform's request timeout even
        # though the server itself stays healthy throughout (2026-07-25
        # field bug: a 502 on a 19-day batch, nothing server-side actually
        # failed - it just took too long). Results are collected here and
        # applied to `out` only after every chunk finishes, on this one
        # thread, so there's no concurrent-write risk on the DataFrame.
        all_results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=min(5, len(chunks)) or 1) as pool:
            futures = [pool.submit(_classify_batch_llm, client, model,
                                   max_tokens, context_block, chunk)
                      for chunk in chunks]
            for f in futures:
                all_results.update(f.result())

        for i in pending:
            res = all_results.get(i)
            if res is None:
                continue
            out.loc[i, ["doc_type", "supplier_code", "account_code",
                        "contra_account", "tax_code"]] = [
                res["doc_type"], res["party_code"], res["account_code"],
                res.get("contra_account", ""), res["tax_code"]]
            out.loc[i, ["confidence", "source", "reason"]] = [
                res["confidence"], "llm", res["reason"]]
        return harmonize_categories(out)

    # Pass 3 — offline heuristic fallback (no API credentials)
    for i in pending:
        hint = str(out.loc[i, "doc_type_hint"] or "") or "purchase"
        out.loc[i, "doc_type"] = hint
        if hint == "cash_sale":
            # party is the walk-in cash customer, not a name on the line
            cust = ctx.customers
            code = ""
            if cust is not None and not cust.empty:
                cash_like = cust[cust["name"].str.upper()
                                 .str.contains("CASH", na=False)]
                code = str((cash_like if not cash_like.empty else cust)
                           .iloc[0]["code"])
            out.loc[i, "supplier_code"] = code
            out.loc[i, ["confidence", "source"]] = ["low", "fallback"]
            out.loc[i, "reason"] = ("offline fallback (cash_sale), walk-in "
                                    "customer from master — set "
                                    "ANTHROPIC_API_KEY for AI coding")
            continue
        code, score = _fuzzy_party(out.loc[i, "supplier"],
                                   _party_master(ctx, hint))
        out.loc[i, "supplier_code"] = code
        out.loc[i, ["confidence", "source"]] = ["low", "fallback"]
        out.loc[i, "reason"] = (
            f"offline fallback ({hint}), fuzzy party match {score:.0%} — set "
            "ANTHROPIC_API_KEY for AI coding")
    return out
