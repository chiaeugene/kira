"""Post approved Purchase Invoices into SQL Accounting via the official SDK.

The SDK is the free OLE/COM interface ("SDK Live", class SQLAcc.BizApp)
documented at https://wiki.sql.com.my/wiki/SDK_Live. It requires SQL
Accounting to be INSTALLED AND RUNNING on this machine, logged into the
target company database (use a BACKUP copy until trusted).

Modes:
  dry_run=True  -> writes the exact payloads to posted/batch_<ts>.json (any machine)
  dry_run=False -> posts for real via COM (SQL machine only)

Login signature per official wiki:
  ComServer.Login(user, password, dcf_path, fdb_name)
Doc type "PH_PI", header dataset "MainDataSet", detail dataset "cdsDocDetail".
Detail field names differ slightly across SQL versions — run dump_fields()
once on the live machine to confirm before first real post.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass
class SQLConfig:
    dry_run: bool = True
    user: str = "ADMIN"
    password: str = "ADMIN"
    dcf_path: str = ""
    fdb_name: str = ""


def _rows_to_invoices(df: pd.DataFrame) -> list[dict]:
    """Group approved rows into one Purchase Invoice per (supplier_code, date, doc_no)."""
    invoices = []
    grouped = df.groupby(["supplier_code", "date", "doc_no"], dropna=False, sort=False)
    for (supplier_code, date, doc_no), g in grouped:
        invoices.append({
            "supplier_code": str(supplier_code),
            "doc_date": str(date),
            "doc_no": str(doc_no) if doc_no else "",
            "lines": [
                {
                    "account_code": str(r["account_code"]),
                    "description": str(r["description"]) or str(r["supplier"]),
                    "amount": float(r["amount"]),
                    "tax_code": str(r["tax_code"]),
                    "tax_amount": float(r.get("tax", 0.0) or 0.0),
                }
                for _, r in g.iterrows()
            ],
        })
    return invoices


class PostedRegistry:
    """Local record of everything already posted for a client — the guard
    against double-posting the same document twice."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "posted_registry.json"
        self.keys: set[str] = set()
        if self.path.exists():
            self.keys = set(json.loads(self.path.read_text(encoding="utf-8")))

    @staticmethod
    def key(supplier_code: str, doc_no: str, amount: float) -> str:
        return f"{str(supplier_code).strip()}|{str(doc_no).strip().upper()}|{float(amount):.2f}"

    def record(self, df: pd.DataFrame) -> None:
        for _, r in df.iterrows():
            self.keys.add(self.key(r["supplier_code"], r.get("doc_no", ""), r["amount"]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.keys)), encoding="utf-8")


def post_batch(df: pd.DataFrame, cfg: SQLConfig,
               out_dir: str | Path = "posted",
               registry: "PostedRegistry | None" = None) -> dict:
    """Post approved rows. Returns a result summary dict.

    If a registry is given, successfully posted lines are recorded so future
    batches can be checked for duplicates. Dry runs also record (so repeated
    demo runs surface the DUP_POSTED check) — delete posted_registry.json to reset.
    """
    invoices = _rows_to_invoices(df)
    ts = time.strftime("%Y%m%d_%H%M%S")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload_path = out / f"batch_{ts}.json"
    payload_path.write_text(
        json.dumps({"invoices": invoices, "dry_run": cfg.dry_run}, indent=2,
                   ensure_ascii=False),
        encoding="utf-8",
    )

    if cfg.dry_run:
        if registry is not None:
            registry.record(df)
        return {
            "mode": "dry_run",
            "invoices": len(invoices),
            "lines": sum(len(i["lines"]) for i in invoices),
            "payload": str(payload_path),
            "posted": [],
            "errors": [],
        }

    result = _post_via_com(invoices, cfg, payload_path)
    if registry is not None and result["posted"]:
        posted_keys = {(i["supplier_code"], i["doc_no"]) for i in result["posted"]}
        ok = df[df.apply(lambda r: (str(r["supplier_code"]), str(r["doc_no"] or ""))
                         in posted_keys, axis=1)]
        registry.record(ok)
    return result


def _post_via_com(invoices: list[dict], cfg: SQLConfig, payload_path: Path) -> dict:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    posted, errors = [], []
    try:
        app = win32com.client.Dispatch("SQLAcc.BizApp")
        if not app.IsLogin:
            app.Login(cfg.user, cfg.password, cfg.dcf_path, cfg.fdb_name)

        for inv in invoices:
            try:
                biz = app.BizObjects.Find("PH_PI")
                biz.New()
                main = biz.DataSets.Find("MainDataSet")
                main.FindField("Code").AsString = inv["supplier_code"]
                main.FindField("DocDate").AsDateTime = inv["doc_date"]
                main.FindField("PostDate").AsDateTime = inv["doc_date"]
                if inv["doc_no"]:
                    try:
                        main.FindField("DocNo").AsString = inv["doc_no"]
                    except Exception:
                        pass  # leave auto-numbering in charge

                detail = biz.DataSets.Find("cdsDocDetail")
                for line in inv["lines"]:
                    detail.Append()
                    _set_detail_account(detail, line["account_code"])
                    detail.FindField("Description").AsString = line["description"]
                    detail.FindField("UnitPrice").AsFloat = line["amount"]
                    detail.FindField("Qty").AsFloat = 1
                    if line["tax_code"]:
                        try:
                            detail.FindField("Tax").AsString = line["tax_code"]
                        except Exception:
                            pass
                    detail.Post()

                biz.Save()
                posted.append(inv)
            except Exception as e:  # keep going; report per-invoice failures
                errors.append({"invoice": inv, "error": str(e)})
    finally:
        pythoncom.CoUninitialize()

    return {
        "mode": "live",
        "invoices": len(invoices),
        "lines": sum(len(i["lines"]) for i in invoices),
        "payload": str(payload_path),
        "posted": posted,
        "errors": errors,
    }


def _set_detail_account(detail, account_code: str) -> None:
    """PH_PI detail lines can be item-based or GL-account-based depending on
    setup. Try the GL 'Account' field first; fall back to ItemCode."""
    for field_name in ("Account", "ItemCode"):
        try:
            detail.FindField(field_name).AsString = account_code
            return
        except Exception:
            continue
    raise RuntimeError(
        "Neither 'Account' nor 'ItemCode' field accepted on cdsDocDetail — "
        "run dump_fields() to inspect the real field names on this SQL version."
    )


def dump_fields(cfg: SQLConfig) -> dict[str, list[str]]:
    """Week-1 spike helper: list actual field names of PH_PI datasets on the
    live machine. Run from the SQL machine with SQL Accounting open."""
    import win32com.client

    app = win32com.client.Dispatch("SQLAcc.BizApp")
    if not app.IsLogin:
        app.Login(cfg.user, cfg.password, cfg.dcf_path, cfg.fdb_name)
    biz = app.BizObjects.Find("PH_PI")
    biz.New()
    result = {}
    for ds_name in ("MainDataSet", "cdsDocDetail"):
        ds = biz.DataSets.Find(ds_name)
        result[ds_name] = [ds.Fields.Items(i).FieldName
                           for i in range(ds.Fields.Count)]
    return result
