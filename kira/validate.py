"""Pre-posting validation & reconciliation engine.

This is the trust layer: every batch is checked BEFORE it can be approved.
Competing OCR tools hand you a CSV and wish you luck; Kira refuses to let
obviously-wrong entries reach the ledger.

Checks:
  DUP_IN_BATCH        same doc_no (or supplier+date+amount) appears twice in this batch
  DUP_POSTED          line matches something already posted for this client
  UNKNOWN_SUPPLIER    supplier_code not in the client's supplier master
  UNKNOWN_ACCOUNT     account_code not in the client's chart of accounts
  UNKNOWN_TAX         tax_code not in the client's tax code list
  TAX_EXCEEDS_AMOUNT  tax >= amount
  TAX_RATE_MISMATCH   tax amount inconsistent with the tax code's rate
  DATE_FUTURE         date more than 7 days in the future
  DATE_STALE          date more than 18 months old
  NEGATIVE_AMOUNT     negative line (possible credit note — wrong module)
  MISSING_DOC_NO      no document number (info only)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from .context import ClientContext

SEV_ERROR, SEV_WARN, SEV_INFO = "error", "warning", "info"


@dataclass
class Issue:
    severity: str
    code: str
    source_row: int   # where it sits in the original file (display)
    message: str
    row_id: int = -1  # batch-unique line id (targeting); -1 if unavailable


def dup_key(supplier_code: str, doc_no: str, amount: float) -> str:
    return f"{str(supplier_code).strip()}|{str(doc_no).strip().upper()}|{float(amount):.2f}"


def validate_batch(df: pd.DataFrame, ctx: ClientContext,
                   posted_keys: set[str] | None = None) -> pd.DataFrame:
    """Return a DataFrame of issues (may be empty). Rows referenced by source_row."""
    posted_keys = posted_keys or set()
    issues: list[Issue] = []
    today = dt.date.today()

    supplier_codes = set(ctx.suppliers["code"]) if not ctx.suppliers.empty else set()
    account_codes = set(ctx.accounts["code"]) if not ctx.accounts.empty else set()
    tax_codes = set(ctx.tax_codes["code"]) if not ctx.tax_codes.empty else set()
    tax_rates: dict[str, float] = {}
    if not ctx.tax_codes.empty and "rate" in ctx.tax_codes.columns:
        for _, r in ctx.tax_codes.iterrows():
            try:
                tax_rates[r["code"]] = float(r["rate"])
            except (TypeError, ValueError):
                pass

    def rid(row) -> int:
        return int(row["row_id"]) if "row_id" in row and pd.notna(row.get("row_id")) else -1

    # --- duplicates inside the batch ---
    seen: dict[str, int] = {}
    for _, row in df.iterrows():
        key = dup_key(row.get("supplier_code", ""), row.get("doc_no", ""), row["amount"])
        alt = dup_key(row.get("supplier", ""), row.get("date", ""), row["amount"])
        for k in {key, alt}:
            if k in seen:
                issues.append(Issue(
                    SEV_ERROR, "DUP_IN_BATCH", int(row["source_row"]),
                    f"Looks identical to row {seen[k]} (same supplier/doc/amount).",
                    rid(row)))
                break
        seen.setdefault(key, int(row["source_row"]))
        seen.setdefault(alt, int(row["source_row"]))

    for _, row in df.iterrows():
        sr = int(row["source_row"])
        rid_ = rid(row)
        amount = float(row["amount"])
        tax = float(row.get("tax", 0) or 0)
        s_code = str(row.get("supplier_code", "")).strip()
        a_code = str(row.get("account_code", "")).strip()
        t_code = str(row.get("tax_code", "")).strip()

        def add(sev: str, code: str, msg: str) -> None:
            issues.append(Issue(sev, code, sr, msg, rid_))

        # --- against posted history ---
        if dup_key(s_code, row.get("doc_no", ""), amount) in posted_keys:
            add(SEV_ERROR, "DUP_POSTED", "Already posted previously for this client.")

        # --- master data checks ---
        if s_code and supplier_codes and s_code not in supplier_codes:
            add(SEV_ERROR, "UNKNOWN_SUPPLIER",
                f"Supplier code '{s_code}' is not in the master.")
        if a_code and account_codes and a_code not in account_codes:
            add(SEV_ERROR, "UNKNOWN_ACCOUNT",
                f"Account code '{a_code}' is not in the chart of accounts.")
        if t_code and tax_codes and t_code not in tax_codes:
            add(SEV_WARN, "UNKNOWN_TAX",
                f"Tax code '{t_code}' is not in the tax code list.")

        # --- amount / tax sanity ---
        if amount < 0:
            add(SEV_WARN, "NEGATIVE_AMOUNT", "Negative amount — is this a credit note?")
        if tax and abs(tax) >= abs(amount):
            add(SEV_ERROR, "TAX_EXCEEDS_AMOUNT",
                f"Tax {tax:.2f} >= amount {amount:.2f}.")
        elif tax and t_code in tax_rates and tax_rates[t_code] > 0:
            rate = tax_rates[t_code]
            exp_exclusive = amount * rate / 100.0
            exp_inclusive = amount * rate / (100.0 + rate)
            if min(abs(tax - exp_exclusive), abs(tax - exp_inclusive)) > max(1.0, 0.05 * abs(amount)):
                add(SEV_WARN, "TAX_RATE_MISMATCH",
                    f"Tax {tax:.2f} doesn't match {t_code} @ {rate:.0f}% "
                    f"(expected ≈{exp_exclusive:.2f}).")
        elif tax and t_code in tax_rates and tax_rates[t_code] == 0:
            add(SEV_WARN, "TAX_RATE_MISMATCH",
                f"Tax {tax:.2f} recorded but {t_code} is a 0% code.")

        # --- date sanity ---
        d = row.get("date")
        if isinstance(d, dt.date):
            if d > today + dt.timedelta(days=7):
                add(SEV_ERROR, "DATE_FUTURE", f"Date {d} is in the future.")
            elif d < today - dt.timedelta(days=548):
                add(SEV_WARN, "DATE_STALE", f"Date {d} is over 18 months old.")

        # --- completeness ---
        if not str(row.get("doc_no", "")).strip():
            add(SEV_INFO, "MISSING_DOC_NO", "No document number — SQL will auto-number.")

    return pd.DataFrame([i.__dict__ for i in issues],
                        columns=["severity", "code", "source_row", "message", "row_id"])


def summarize(issues: pd.DataFrame) -> dict:
    if issues.empty:
        return {"error": 0, "warning": 0, "info": 0}
    counts = issues["severity"].value_counts().to_dict()
    return {k: int(counts.get(k, 0)) for k in ("error", "warning", "info")}
