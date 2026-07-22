"""One-command conversion: bookkeeper file(s) -> SQL Accounting.

  python convert.py inbox\\june.xlsx --client DEMO_CLIENT
      parse + code + validate, write a review CSV, post nothing

  python convert.py inbox\\june.xlsx --client DEMO_CLIENT --post
      same, then post immediately IF zero validation errors and zero
      low-confidence lines (otherwise it refuses and points at the review CSV)

  python convert.py --from-review review_20260721.csv --client DEMO_CLIENT --post
      post a review CSV after the bookkeeper edited/approved it in Excel

Posting obeys config.yaml (dry_run true/false). Exit code 0 = success,
1 = needs human review, 2 = hard failure.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import yaml

from kira.audit import AuditLog
from kira.classify import classify
from kira.documents import MEDIA_TYPES, extract_documents, llm_available
from kira.ingest import parse_workbook
from kira.poster import PostedRegistry, SQLConfig, post_batch
from kira.registry import client_dir, open_client
from kira.validate import summarize, validate_batch

REVIEW_COLS = ["row_id", "doc_type", "date", "supplier", "description",
               "amount", "tax", "doc_no", "supplier_code", "account_code",
               "tax_code", "contra_account", "confidence", "source", "reason",
               "doc_type_hint", "source_row"]


def load_cfg() -> dict:
    return yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))


def ingest_files(paths: list[Path], llm_cfg: dict,
                 client_name: str = "the business") -> tuple[pd.DataFrame, list[str]]:
    frames, notes = [], []
    docs: list[tuple[str, bytes]] = []
    for p in paths:
        ext = p.suffix.lower()
        if ext in {".xlsx", ".xls", ".csv"}:
            part, part_notes = parse_workbook(p)
            part["source_file"] = p.name
            frames.append(part)
            notes += [f"{p.name} {n}" for n in part_notes]
        elif ext in MEDIA_TYPES:
            docs.append((p.name, p.read_bytes()))
        else:
            notes.append(f"{p.name}: unsupported type, skipped")
    if docs:
        if not llm_available():
            raise SystemExit("PDF/image files need ANTHROPIC_API_KEY set.")
        extracted = extract_documents(docs, model=llm_cfg["model"],
                                      max_tokens=llm_cfg["max_tokens"],
                                      client_name=client_name)
        frames.append(extracted)
        notes.append(f"{len(docs)} document file(s) -> {len(extracted)} entries")
    if not frames:
        raise SystemExit("Nothing parseable given.")
    from kira.batches import ensure_row_ids
    return ensure_row_ids(pd.concat(frames, ignore_index=True)), notes


def main() -> int:
    ap = argparse.ArgumentParser(description="Kira converter")
    ap.add_argument("files", nargs="*", help="Excel/CSV/PDF/image files")
    ap.add_argument("--client", required=True)
    ap.add_argument("--post", action="store_true",
                    help="post if fully clean (else just write the review CSV)")
    ap.add_argument("--from-review", metavar="CSV",
                    help="post a previously written & edited review CSV")
    args = ap.parse_args()

    cfg = load_cfg()
    sql = SQLConfig(**cfg["sql"])
    ctx, store, audit = open_client(args.client)
    registry = PostedRegistry(client_dir(args.client))

    if args.from_review:
        coded = pd.read_csv(args.from_review, dtype=str).fillna("")
        coded["amount"] = coded["amount"].astype(float)
        coded["tax"] = coded["tax"].astype(float)
        coded["date"] = pd.to_datetime(coded["date"]).dt.date
        coded["source_row"] = coded["source_row"].astype(int)
        notes = [f"loaded review file {args.from_review}"]
    else:
        if not args.files:
            ap.error("give input files or --from-review")
        paths = [Path(f) for f in args.files]
        raw, notes = ingest_files(paths, cfg["llm"], client_name=args.client)
        print("\n".join(f"  {n}" for n in notes))
        coded = classify(raw, ctx, store, model=cfg["llm"]["model"],
                         batch_size=cfg["llm"]["batch_size"],
                         max_tokens=cfg["llm"]["max_tokens"])

    issues = validate_batch(coded, ctx, registry.keys)
    counts = summarize(issues)
    n_low = int((coded["confidence"] == "low").sum())
    total = float(coded["amount"].sum())
    print(f"\n  {len(coded)} lines | RM {total:,.2f} | "
          f"errors={counts['error']} warnings={counts['warning']} "
          f"low-confidence={n_low}")
    if not issues.empty:
        for _, i in issues.iterrows():
            print(f"    [{i.severity}] row {i.source_row} {i.code}: {i.message}")

    clean = counts["error"] == 0 and n_low == 0 and \
        (coded["supplier_code"] != "").all() and (coded["account_code"] != "").all()

    if not (args.post and clean):
        review_path = Path(f"review_{args.client}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        cols = [c for c in REVIEW_COLS if c in coded.columns]
        coded[cols].to_csv(review_path, index=False)
        if args.post:
            print(f"\n  NOT POSTED — needs review. Fix and re-run with "
                  f"--from-review {review_path}")
            return 1
        print(f"\n  Review file written: {review_path}")
        return 0

    # fully clean -> learn + post
    for _, r in coded.iterrows():
        store.learn(r["supplier"], r["supplier_code"], r["account_code"],
                    r["tax_code"], str(r.get("doc_type", "") or "purchase"))
    store.save()
    result = post_batch(coded, sql, registry=registry)
    audit.log_batch(", ".join(args.files or [args.from_review]), result,
                    len(coded), total, 0, counts)
    print(f"\n  {result['mode'].upper()}: {result['invoices']} invoice(s), "
          f"{result['lines']} line(s), control total RM {total:,.2f}")
    if result.get("errors"):
        print(f"  {len(result['errors'])} invoice(s) FAILED — see {result['payload']}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
