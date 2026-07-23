"""Shared approve / reject logic for batches in review.

One implementation used by BOTH the console Inbox and the Cloud API, so the
rules (validation gate, correction logging, learning) can never drift apart.
"""

from __future__ import annotations

import pandas as pd

from .audit import AuditLog  # noqa: F401  (type reference)
from .batches import BatchStore
from .poster import PostedRegistry
from .registry import client_dir, open_client
from .validate import summarize, validate_batch


def approve_batch(store: BatchStore, batch: dict,
                  rows: pd.DataFrame) -> tuple[bool, dict]:
    """Validate + learn + queue a review-state batch for the Agent.

    Returns (ok, info). On failure info carries the issues; nothing is
    learned or transitioned — the batch stays in review.
    """
    client = batch["client"]
    ctx, rules, audit = open_client(client)
    registry = PostedRegistry(client_dir(client))

    issues = validate_batch(rows, ctx, registry.keys)
    counts = summarize(issues)
    # Journal entries have no supplier/customer by nature — demanding a party
    # code on them made journal batches impossible to approve (field bug).
    # They need the contra side of the double entry instead.
    dtp = (rows["doc_type"].fillna("").astype(str)
           if "doc_type" in rows.columns else pd.Series("", index=rows.index))
    is_journal = dtp == "journal"
    contra = (rows["contra_account"].fillna("").astype(str).str.strip()
              if "contra_account" in rows.columns
              else pd.Series("", index=rows.index))
    blank = rows[(~is_journal & (rows["supplier_code"] == ""))
                 | (rows["account_code"] == "")
                 | (is_journal & (contra == ""))]
    if counts["error"] > 0 or not blank.empty:
        return False, {
            "message": "batch not clean — fix and re-approve",
            "errors": counts["error"],
            "blank_codes": len(blank),
            "issues": issues.to_dict(orient="records"),
        }

    # log corrections vs the AI's original coding, then learn everything
    n_corr = 0
    def key(rec) -> int:
        return int(rec.get("row_id", rec["source_row"]) if isinstance(rec, dict)
                   else rec.get("row_id", rec["source_row"]))
    orig = {key(r): r for r in batch["rows"]}
    for _, r in rows.iterrows():
        o = orig.get(key(r))
        if o:
            for f in ("doc_type", "supplier_code", "account_code", "tax_code"):
                if str(o.get(f, "")) != str(r[f]):
                    n_corr += 1
                    audit.log_correction(int(r["source_row"]), str(r["supplier"]),
                                         f, o.get(f, ""), r[f],
                                         str(r.get("source", "")))
        if str(r["supplier"]).strip():  # partyless journals: nothing to key on
            rules.learn(r["supplier"], r["supplier_code"], r["account_code"],
                        r["tax_code"], str(r.get("doc_type", "") or "purchase"))
    rules.save()

    store.update_rows(batch["id"], rows)
    updated = store.transition(batch["id"], "approved", corrections=n_corr)
    return True, {"batch": updated, "corrections": n_corr}


def reject_batch(store: BatchStore, batch: dict, reason: str) -> dict:
    """Reject a review-state batch (kept in the queue for the audit trail)."""
    _ctx, _rules, audit = open_client(batch["client"])
    updated = store.transition(batch["id"], "rejected", reject_reason=reason)
    audit.log_batch(", ".join(batch["source_files"]),
                    {"mode": "rejected", "invoices": 0, "errors": [],
                     "payload": reason},
                    len(batch["rows"]), batch["total_rm"], 0, {})
    return updated
