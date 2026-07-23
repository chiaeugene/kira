"""Suggested repairs — for every validation issue Kira can, it proposes a
concrete fix. The human still approves; Kira just does the finding.

propose_fixes(rows, issues, ctx)  -> DataFrame of proposals
apply_fixes(rows, fixes)          -> repaired rows (pure function, used by the
                                     console in both local and remote mode)

Proposal fields:
  row_id (targeting), source_row (display), issue, field, current, proposed, reason
A field value of "__drop__" means "remove this line" (duplicates).

Targeting uses the batch-unique row_id, never source_row — source_row repeats
across sheets and files.
"""

from __future__ import annotations

import datetime as dt
from difflib import SequenceMatcher

import pandas as pd

from .context import ClientContext
from .rules import normalize_supplier


def _best_match(target: str, candidates: list[tuple[str, str]],
                floor: float = 0.55) -> tuple[str, str, float] | None:
    """candidates: (code, comparable_text). Returns (code, text, score)."""
    best = None
    for code, text in candidates:
        score = SequenceMatcher(None, target, text).ratio()
        if best is None or score > best[2]:
            best = (code, text, score)
    return best if best and best[2] >= floor else None


def propose_fixes(rows: pd.DataFrame, issues: pd.DataFrame,
                  ctx: ClientContext) -> pd.DataFrame:
    fixes: list[dict] = []
    if rows.empty:
        return pd.DataFrame(fixes)

    def rid_of(r) -> int:
        return int(r["row_id"]) if "row_id" in r and pd.notna(r.get("row_id")) else -1

    by_rid = {rid_of(r): r for _, r in rows.iterrows()}
    suppliers = [(str(r["code"]), normalize_supplier(r["name"]))
                 for _, r in ctx.suppliers.iterrows()] if not ctx.suppliers.empty else []
    customers = [(str(r["code"]), normalize_supplier(r["name"]))
                 for _, r in ctx.customers.iterrows()] if not ctx.customers.empty else []

    def party_master_for(row) -> list[tuple[str, str]]:
        if str(row.get("doc_type", "")) in ("sale", "sales_return",
                                            "customer_payment"):
            return customers
        return suppliers
    accounts = [(str(r["code"]), str(r["code"]))
                for _, r in ctx.accounts.iterrows()] if not ctx.accounts.empty else []
    taxes = [(str(r["code"]), str(r["code"]))
             for _, r in ctx.tax_codes.iterrows()] if not ctx.tax_codes.empty else []
    tax_rates: dict[str, float] = {}
    if not ctx.tax_codes.empty and "rate" in ctx.tax_codes.columns:
        for _, r in ctx.tax_codes.iterrows():
            try:
                tax_rates[str(r["code"])] = float(r["rate"])
            except (TypeError, ValueError):
                pass

    def add(rid: int, sr: int, issue: str, field: str, current, proposed,
            reason: str):
        fixes.append({"row_id": rid, "source_row": sr, "issue": issue,
                      "field": field, "current": str(current),
                      "proposed": str(proposed), "reason": reason})

    seen_drops: set[int] = set()
    for _, iss in (issues.iterrows() if not issues.empty else []):
        rid = int(iss["row_id"]) if "row_id" in iss and pd.notna(iss.get("row_id")) else -1
        row = by_rid.get(rid)
        if row is None:
            continue
        sr = int(iss["source_row"])
        code = iss["code"]

        if code in ("DUP_IN_BATCH", "DUP_POSTED") and rid not in seen_drops:
            seen_drops.add(rid)
            add(rid, sr, code, "__drop__", "(line kept)", "remove line",
                "Identical to an entry already in this batch or already posted.")

        elif code in ("UNKNOWN_SUPPLIER", "UNKNOWN_CUSTOMER"):
            master = party_master_for(row)
            m = _best_match(normalize_supplier(row["supplier"]), master) \
                if master else None
            if m:
                kind = "customer" if code == "UNKNOWN_CUSTOMER" else "supplier"
                add(rid, sr, code, "supplier_code", row["supplier_code"], m[0],
                    f"'{row['supplier']}' looks like {kind} {m[0]} "
                    f"({m[2]:.0%} name match).")

        elif code == "UNKNOWN_ACCOUNT" and accounts:
            m = _best_match(str(row["account_code"]), accounts, floor=0.6)
            if m:
                add(rid, sr, code, "account_code", row["account_code"], m[0],
                    f"Code not in the chart of accounts; closest is {m[0]}.")

        elif code == "JOURNAL_NO_CONTRA":
            money = ctx.money_accounts()
            if not money.empty:
                m_code = str(money.iloc[0]["code"])
                m_desc = str(money.iloc[0].get("description", ""))
                add(rid, sr, code, "contra_account", "", m_code,
                    f"Journal needs a balancing side — defaulted to {m_code} "
                    f"{m_desc} (change it if the money moved elsewhere).")

        elif code == "UNKNOWN_TAX" and taxes:
            m = _best_match(str(row["tax_code"]), taxes, floor=0.5)
            if m:
                add(rid, sr, code, "tax_code", row["tax_code"], m[0],
                    f"Tax code not in the list; closest is {m[0]}.")

        elif code == "TAX_EXCEEDS_AMOUNT":
            add(rid, sr, code, "tax", row["tax"], 0.0,
                "Tax cannot exceed the amount — likely a column mix-up; "
                "cleared for manual check.")

        elif code == "TAX_RATE_MISMATCH":
            rate = tax_rates.get(str(row["tax_code"]))
            if rate is not None:
                if rate == 0:
                    add(rid, sr, code, "tax", row["tax"], 0.0,
                        f"{row['tax_code']} is a 0% code — tax cleared.")
                else:
                    expected = round(float(row["amount"]) * rate / 100.0, 2)
                    add(rid, sr, code, "tax", row["tax"], expected,
                        f"Recomputed at {rate:.0f}% of {float(row['amount']):.2f}.")

        elif code == "DATE_FUTURE":
            d = row["date"]
            if isinstance(d, dt.date):
                today = dt.date.today()
                candidates = []
                if d.day <= 12:  # day/month swapped is the classic typo
                    try:
                        candidates.append(dt.date(d.year, d.day, d.month))
                    except ValueError:
                        pass
                try:
                    candidates.append(d.replace(year=today.year))
                    candidates.append(d.replace(year=today.year - 1))
                except ValueError:
                    pass
                fix = next((c for c in candidates if c <= today), None)
                if fix:
                    add(rid, sr, code, "date", d, fix,
                        "Date is in the future — likely a swapped day/month "
                        "or wrong year.")

    # blanks (no issue row, but the batch can't post without codes)
    for rid, row in by_rid.items():
        if str(row.get("doc_type", "")) == "journal":
            continue  # journals have no party — nothing to propose
        if str(row.get("supplier_code", "")).strip() == "":
            master = party_master_for(row)
            if not master:
                continue
            m = _best_match(normalize_supplier(row["supplier"]), master)
            if m:
                add(rid, int(row["source_row"]), "BLANK_PARTY",
                    "supplier_code", "", m[0],
                    f"'{row['supplier']}' matches {m[0]} ({m[2]:.0%}).")

    return pd.DataFrame(fixes)


def apply_fixes(rows: pd.DataFrame, fixes: pd.DataFrame) -> pd.DataFrame:
    """Apply every proposal, targeting lines by row_id. Pure function."""
    out = rows.copy()
    if fixes.empty or "row_id" not in out.columns:
        return out
    drop_ids = set(fixes[fixes["field"] == "__drop__"]["row_id"].astype(int))
    if drop_ids:
        out = out[~out["row_id"].astype(int).isin(drop_ids)].reset_index(drop=True)
    for _, f in fixes[fixes["field"] != "__drop__"].iterrows():
        mask = out["row_id"].astype(int) == int(f["row_id"])
        field = f["field"]
        value = f["proposed"]
        if field == "tax":
            value = float(value)
        elif field == "date":
            value = pd.to_datetime(value).date()
        out.loc[mask, field] = value
    return out
