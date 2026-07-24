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


CUSTOMER_SIDE = ("sale", "sales_return", "customer_payment")
PAYMENT_TYPES = ("customer_payment", "supplier_payment")


def dup_key(supplier_code: str, doc_no: str, amount: float,
            doc_type: str = "purchase", extra: str = "") -> str:
    """extra distinguishes lines that would otherwise collide with no party
    to tell them apart (journal lines split from a daily-takings sheet, see
    kira/ingest.py) — appended only when given, so keys already written to
    a client's posted_registry.json (no 'extra' concept when those were
    recorded) keep matching exactly as before."""
    base = (f"{doc_type or 'purchase'}|{str(supplier_code).strip()}|"
           f"{str(doc_no).strip().upper()}|{float(amount):.2f}")
    return f"{base}|{str(extra).strip().lower()}" if extra else base


@dataclass
class JournalGroups:
    grp_key: pd.Series       # row index -> group id (doc_no, or a solo id)
    balanced: set            # groups with >1 row that net to ~zero
    unbalanced: set          # groups with >1 row that DON'T net to zero
    sizes: pd.Series         # group id -> row count (journal rows only)
    sums: pd.Series          # group id -> signed amount total (journal rows only)


def journal_balanced_groups(df: pd.DataFrame) -> JournalGroups:
    """Group journal rows by doc_no. A blank doc_no never groups (each such
    row is its own solo group) — used by both validate_batch and
    kira/review.py so 'does this row need its own contra_account' can never
    drift between the two call sites."""
    dtp_all = (df["doc_type"].fillna("").astype(str)
              if "doc_type" in df.columns else pd.Series("", index=df.index))
    journal_mask = dtp_all == "journal"
    doc_no_s = (df["doc_no"].fillna("").astype(str).str.strip()
               if "doc_no" in df.columns else pd.Series("", index=df.index))
    grp_key = doc_no_s.where(doc_no_s != "",
                             other=pd.Series([f"__solo_{i}" for i in df.index],
                                             index=df.index))
    sizes = grp_key[journal_mask].value_counts()
    multi_groups = set(sizes[sizes > 1].index)
    sums = df.loc[journal_mask].groupby(grp_key[journal_mask])["amount"].sum()
    balanced = {g for g in multi_groups if abs(sums.get(g, 1.0)) <= 0.02}
    return JournalGroups(grp_key, balanced, multi_groups - balanced, sizes, sums)


def validate_batch(df: pd.DataFrame, ctx: ClientContext,
                   posted_keys: set[str] | None = None) -> pd.DataFrame:
    """Return a DataFrame of issues (may be empty). Rows referenced by source_row."""
    posted_keys = posted_keys or set()
    issues: list[Issue] = []
    today = dt.date.today()

    supplier_codes = set(ctx.suppliers["code"]) if not ctx.suppliers.empty else set()
    customer_codes = set(ctx.customers["code"]) if not ctx.customers.empty else set()
    account_codes = set(ctx.accounts["code"]) if not ctx.accounts.empty else set()
    tax_codes = set(ctx.tax_codes["code"]) if not ctx.tax_codes.empty else set()
    account_types: dict[str, str] = {}
    if not ctx.accounts.empty and "type" in ctx.accounts.columns:
        account_types = {str(r["code"]): str(r.get("type", "")).upper()
                         for _, r in ctx.accounts.iterrows()}
    tax_rates: dict[str, float] = {}
    if not ctx.tax_codes.empty and "rate" in ctx.tax_codes.columns:
        for _, r in ctx.tax_codes.iterrows():
            try:
                tax_rates[r["code"]] = float(r["rate"])
            except (TypeError, ValueError):
                pass

    def rid(row) -> int:
        return int(row["row_id"]) if "row_id" in row and pd.notna(row.get("row_id")) else -1

    # --- journal multi-line groups: many rows sharing one doc_no (e.g. a
    # daily-takings sheet split into revenue/tax/payment-method lines by
    # kira/ingest.py) don't need a contra_account PER ROW — the group just
    # has to net to ~zero, and kira/poster.py posts each line as a single
    # debit or credit instead of pairing every line with its own contra.
    jg = journal_balanced_groups(df)
    flagged_unbalanced: set[str] = set()

    # --- duplicates inside the batch ---
    seen: dict[str, int] = {}
    for _, row in df.iterrows():
        dtp = str(row.get("doc_type", "") or "purchase")
        # Journal lines split from a daily-takings sheet have no party, and
        # different categories can coincidentally share the same amount on
        # the same day (e.g. cash and card both RM162.85) — description is
        # what actually distinguishes them, so it joins the key for those.
        extra = str(row.get("description", "")) if dtp == "journal" else ""
        key = dup_key(row.get("supplier_code", ""), row.get("doc_no", ""),
                      row["amount"], dtp, extra)
        alt = dup_key(row.get("supplier", ""), row.get("date", ""),
                      row["amount"], dtp, extra)
        for k in {key, alt}:
            if k in seen:
                issues.append(Issue(
                    SEV_ERROR, "DUP_IN_BATCH", int(row["source_row"]),
                    f"Looks identical to row {seen[k]} (same supplier/doc/amount).",
                    rid(row)))
                break
        seen.setdefault(key, int(row["source_row"]))
        seen.setdefault(alt, int(row["source_row"]))

    for idx, row in df.iterrows():
        sr = int(row["source_row"])
        rid_ = rid(row)
        amount = float(row["amount"])
        tax = float(row.get("tax", 0) or 0)
        s_code = str(row.get("supplier_code", "")).strip()
        a_code = str(row.get("account_code", "")).strip()
        t_code = str(row.get("tax_code", "")).strip()

        dtp = str(row.get("doc_type", "") or "")

        def add(sev: str, code: str, msg: str) -> None:
            issues.append(Issue(sev, code, sr, msg, rid_))

        # --- the line must know what it is ---
        if not dtp:
            add(SEV_ERROR, "DOC_TYPE_MISSING",
                "Line has no document type — cannot decide which SQL module "
                "it belongs to.")
            dtp = "purchase"

        # --- against posted history ---
        posted_extra = str(row.get("description", "")) if dtp == "journal" else ""
        if dup_key(s_code, row.get("doc_no", ""), amount, dtp, posted_extra) in posted_keys:
            add(SEV_ERROR, "DUP_POSTED", "Already posted previously for this client.")

        # --- party checked against the CORRECT master for the doc type ---
        if dtp in CUSTOMER_SIDE:
            if s_code and customer_codes and s_code not in customer_codes:
                add(SEV_ERROR, "UNKNOWN_CUSTOMER",
                    f"Customer code '{s_code}' is not in the customer master.")
        else:
            if s_code and supplier_codes and s_code not in supplier_codes:
                add(SEV_ERROR, "UNKNOWN_SUPPLIER",
                    f"Supplier code '{s_code}' is not in the master.")

        # --- journals need BOTH sides of the double entry ---
        if dtp == "journal":
            contra = str(row.get("contra_account", "") or "").strip()
            g = jg.grp_key.loc[idx]
            if contra:
                if account_codes and contra not in account_codes:
                    add(SEV_ERROR, "UNKNOWN_ACCOUNT",
                        f"Contra account '{contra}' is not in the chart of "
                        "accounts.")
            elif g in jg.balanced:
                pass  # multi-line day nets to zero as a group — no single
                      # row needs its own contra account (see poster.py)
            elif g in jg.unbalanced:
                if g not in flagged_unbalanced:
                    flagged_unbalanced.add(g)
                    off = abs(jg.sums[g])
                    add(SEV_ERROR, "JOURNAL_GROUP_UNBALANCED",
                        f"This day's {int(jg.sizes[g])} journal line(s) "
                        f"don't net to zero (off by RM {off:,.2f}) and none "
                        "has its own contra account — check for a missing "
                        "or misread column.")
            else:
                add(SEV_ERROR, "JOURNAL_NO_CONTRA",
                    "Journal line has no contra account — the other side of "
                    "the double entry (often the bank/cash account).")

        # --- account checks: exists + is the right KIND for the doc type ---
        if a_code and account_codes and a_code not in account_codes:
            add(SEV_ERROR, "UNKNOWN_ACCOUNT",
                f"Account code '{a_code}' is not in the chart of accounts.")
        elif a_code and account_types.get(a_code):
            a_type = account_types[a_code]
            if dtp == "sale" and any(k in a_type for k in ("EXPENSE", "COST")):
                add(SEV_WARN, "ACCOUNT_TYPE_MISMATCH",
                    f"Sale coded to '{a_code}' ({a_type}) — expected an "
                    "income/sales account.")
            elif dtp == "purchase" and any(k in a_type for k in ("SALES", "INCOME", "REVENUE")):
                add(SEV_WARN, "ACCOUNT_TYPE_MISMATCH",
                    f"Purchase coded to '{a_code}' ({a_type}) — expected an "
                    "expense/cost account.")
            elif dtp in PAYMENT_TYPES and not any(k in a_type for k in ("BANK", "CASH")):
                add(SEV_WARN, "ACCOUNT_TYPE_MISMATCH",
                    f"Payment coded to '{a_code}' ({a_type}) — expected a "
                    "bank or cash account.")

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
