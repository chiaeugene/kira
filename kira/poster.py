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

Multi-module: each line's doc_type routes it to the right SQL document:

  purchase          -> PH_PI  (Purchase Invoice)
  purchase_return   -> PH_CN  (Purchase Returned / credit note)
  sale              -> SL_IV  (Sales Invoice)
  sales_return      -> SL_CN  (Sales Credit Note)
  customer_payment  -> AR_PM  (Customer Payment, header-level)
  supplier_payment  -> AP_PM  (Supplier Payment, header-level)
  journal           -> GL_JE  (Journal Entry, debit acct + contra credit)

Header dataset "MainDataSet", detail dataset "cdsDocDetail" per official
wiki samples. Field names differ slightly across SQL versions — run
dump_fields() once on the live machine to confirm before first real post;
setters try known field-name variants and fail loudly, never silently.
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


DOC_TYPE_TO_SQL = {
    "purchase": "PH_PI",
    "purchase_return": "PH_CN",
    "sale": "SL_IV",
    "sales_return": "SL_CN",
    "customer_payment": "AR_PM",
    "supplier_payment": "AP_PM",
    "journal": "GL_JE",
}
HEADER_ONLY_TYPES = {"customer_payment", "supplier_payment"}


def _rows_to_invoices(df: pd.DataFrame) -> list[dict]:
    """Group approved rows into one SQL document per
    (doc_type, party code, date, doc_no)."""
    work = df.copy()
    if "doc_type" not in work.columns:
        work["doc_type"] = "purchase"
    work["doc_type"] = work["doc_type"].replace("", "purchase")
    invoices = []
    grouped = work.groupby(["doc_type", "supplier_code", "date", "doc_no"],
                           dropna=False, sort=False)
    for (doc_type, supplier_code, date, doc_no), g in grouped:
        invoices.append({
            "doc_type": str(doc_type),
            "sql_doc": DOC_TYPE_TO_SQL.get(str(doc_type), "PH_PI"),
            "supplier_code": str(supplier_code),   # party: creditor OR debtor
            "doc_date": str(date),
            "doc_no": str(doc_no) if doc_no else "",
            "lines": [
                {
                    "account_code": str(r["account_code"]),
                    "description": str(r["description"]) or str(r["supplier"]),
                    "amount": float(r["amount"]),
                    "tax_code": str(r["tax_code"]),
                    "tax_amount": float(r.get("tax", 0.0) or 0.0),
                    "contra_account": str(r.get("contra_account", "") or ""),
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
    def key(supplier_code: str, doc_no: str, amount: float,
            doc_type: str = "purchase") -> str:
        return (f"{doc_type or 'purchase'}|{str(supplier_code).strip()}|"
                f"{str(doc_no).strip().upper()}|{float(amount):.2f}")

    def record(self, df: pd.DataFrame) -> None:
        for _, r in df.iterrows():
            self.keys.add(self.key(r["supplier_code"], r.get("doc_no", ""),
                                   r["amount"],
                                   str(r.get("doc_type", "") or "purchase")))
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
                _post_one(app, inv)
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


def _set_first(dataset, field_names: tuple[str, ...], value, kind="str") -> str:
    """Set the first field name that exists; fail loudly if none do."""
    for name in field_names:
        try:
            f = dataset.FindField(name)
            if kind == "float":
                f.AsFloat = float(value)
            elif kind == "date":
                f.AsDateTime = value
            else:
                f.AsString = str(value)
            return name
        except Exception:
            continue
    raise RuntimeError(
        f"None of the fields {field_names} exist on this dataset — run "
        "dump_fields() on this SQL version and adjust the mapping.")


def _post_one(app, inv: dict) -> None:
    """Post one grouped document into its SQL module."""
    doc_type, sql_doc = inv["doc_type"], inv["sql_doc"]
    biz = app.BizObjects.Find(sql_doc)
    biz.New()
    main = biz.DataSets.Find("MainDataSet")

    if doc_type != "journal":
        _set_first(main, ("Code",), inv["supplier_code"])
    _set_first(main, ("DocDate",), inv["doc_date"], kind="date")
    try:
        _set_first(main, ("PostDate",), inv["doc_date"], kind="date")
    except RuntimeError:
        pass
    if inv["doc_no"]:
        try:
            _set_first(main, ("DocNo",), inv["doc_no"])
        except RuntimeError:
            pass  # leave auto-numbering in charge

    if doc_type in HEADER_ONLY_TYPES:
        # Payments: header-level amount + which bank/cash it moved through.
        total = sum(line["amount"] for line in inv["lines"])
        method = next((line["account_code"] for line in inv["lines"]
                       if line["account_code"]), "")
        if method:
            try:
                _set_first(main, ("PaymentMethod", "BankCharge2Method",
                                  "Method"), method)
            except RuntimeError:
                pass
        _set_first(main, ("DocAmt", "Amount", "PaymentAmt"), total, kind="float")
        try:
            desc = inv["lines"][0]["description"]
            if desc:
                _set_first(main, ("Description",), desc)
        except RuntimeError:
            pass
        biz.Save()
        return

    detail = biz.DataSets.Find("cdsDocDetail")
    for line in inv["lines"]:
        detail.Append()
        if doc_type == "journal":
            # debit the account, credit the contra (or vice versa if negative)
            if not line["contra_account"]:
                raise ValueError(
                    "journal line has no contra_account — refusing to post "
                    "an unbalanced entry (fix it in the console and re-approve)")
            amt = line["amount"]
            _set_first(detail, ("Account", "AccNo", "Code"), line["account_code"])
            _set_first(detail, ("Description",), line["description"])
            _set_first(detail, ("DR", "Debit") if amt >= 0 else ("CR", "Credit"),
                       abs(amt), kind="float")
            detail.Post()
            if line["contra_account"]:
                detail.Append()
                _set_first(detail, ("Account", "AccNo", "Code"),
                           line["contra_account"])
                _set_first(detail, ("Description",), line["description"])
                _set_first(detail, ("CR", "Credit") if amt >= 0 else ("DR", "Debit"),
                           abs(amt), kind="float")
                detail.Post()
            continue

        # invoice-style documents (PH_PI, PH_CN, SL_IV, SL_CN)
        _set_first(detail, ("Account", "ItemCode"), line["account_code"])
        _set_first(detail, ("Description",), line["description"])
        _set_first(detail, ("UnitPrice", "Amount"), line["amount"], kind="float")
        try:
            _set_first(detail, ("Qty",), 1, kind="float")
        except RuntimeError:
            pass
        if line["tax_code"]:
            try:
                _set_first(detail, ("Tax", "TaxType"), line["tax_code"])
            except RuntimeError:
                pass
        detail.Post()

    biz.Save()


def dump_fields(cfg: SQLConfig,
                sql_docs: tuple[str, ...] = tuple(DOC_TYPE_TO_SQL.values())
                ) -> dict[str, dict[str, list[str]]]:
    """Go-live spike helper: list the actual field names of every module's
    datasets on the live machine, so the mappings above can be confirmed.
    Run from the SQL PC with SQL Accounting installed."""
    import win32com.client

    app = win32com.client.Dispatch("SQLAcc.BizApp")
    if not app.IsLogin:
        app.Login(cfg.user, cfg.password, cfg.dcf_path, cfg.fdb_name)
    result: dict[str, dict[str, list[str]]] = {}
    for sql_doc in sql_docs:
        result[sql_doc] = {}
        try:
            biz = app.BizObjects.Find(sql_doc)
            biz.New()
            for ds_name in ("MainDataSet", "cdsDocDetail"):
                try:
                    ds = biz.DataSets.Find(ds_name)
                    result[sql_doc][ds_name] = [
                        ds.Fields.Items(i).FieldName
                        for i in range(ds.Fields.Count)]
                except Exception as e:
                    result[sql_doc][ds_name] = [f"<unavailable: {e}>"]
        except Exception as e:
            result[sql_doc]["<error>"] = [str(e)]
    return result
