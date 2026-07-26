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

import datetime as _dt
import json
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .clock import stamp

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
    "cash_sale": "SL_CS",   # daily takings per the accountant's ruling
                            # (2026-07-26): NOT GL_JE — auditors treat JVs
                            # as adjustments needing support documents, and
                            # SST only auto-computes on sales documents
    "customer_payment": "AR_PM",
    "supplier_payment": "AP_PM",
    "journal": "GL_JE",
}
HEADER_ONLY_TYPES = {"customer_payment", "supplier_payment"}

# purchase_return and supplier_payment came back unavailable (BizObjects.Find
# returned None) on The Voice Karaoke's install (2026-07-25), despite every
# OTHER code following this exact naming convention correctly (PH_PI, SL_IV,
# SL_CN, AR_PM, GL_JE all matched first try) — that consistency makes a
# naming mistake unlikely; a module simply switched off for this company
# (File > Customize SQL Account Modules) is the more likely cause. These
# extra candidates are tried defensively in case it IS a naming variant.
DOC_TYPE_TO_SQL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "purchase_return": ("PH_CN", "PH_DN", "PH_DEBITNOTE"),
    "supplier_payment": ("AP_PM", "AP_PV", "CB_PV"),
}


def _find_biz(app, doc_type: str, default_sql_doc: str):
    """Resolve a doc_type to its live BizObject, trying every known naming
    candidate before giving up. Returns (biz, resolved_code) — biz is None
    if nothing resolved (module likely disabled for this company, see
    DOC_TYPE_TO_SQL_CANDIDATES)."""
    candidates = DOC_TYPE_TO_SQL_CANDIDATES.get(doc_type, (default_sql_doc,))
    for code in candidates:
        try:
            biz = app.BizObjects.Find(code)
            if biz is not None:
                return biz, code
        except Exception:
            continue
    return None, ""


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
            doc_type: str = "purchase", extra: str = "") -> str:
        """extra distinguishes lines with no party that could otherwise
        collide on the same doc_no+amount (journal lines split from a
        daily-takings sheet, see kira/ingest.py + kira/validate.py's
        dup_key — same rule, kept in sync). Appended only when given, so
        keys already written to posted_registry.json before this existed
        still match exactly."""
        base = (f"{doc_type or 'purchase'}|{str(supplier_code).strip()}|"
               f"{str(doc_no).strip().upper()}|{float(amount):.2f}")
        return f"{base}|{str(extra).strip().lower()}" if extra else base

    def record(self, df: pd.DataFrame) -> None:
        for _, r in df.iterrows():
            dtp = str(r.get("doc_type", "") or "purchase")
            extra = str(r.get("description", "")) if dtp == "journal" else ""
            self.keys.add(self.key(r["supplier_code"], r.get("doc_no", ""),
                                   r["amount"], dtp, extra))
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
    ts = stamp()

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


def _real_field_names(dataset) -> dict[str, str]:
    """UPPER(name) -> actual stored name, for every field on this dataset."""
    return {dataset.Fields.Items(i).FieldName.upper():
            dataset.Fields.Items(i).FieldName
            for i in range(dataset.Fields.Count)}


def _coerce(value, kind: str):
    """Convert to the Python type COM marshals correctly for this field kind.
    Dates especially: a plain 'YYYY-MM-DD' string is NOT a date to the SDK -
    pywin32 converts a real datetime into a proper COM date automatically."""
    if kind == "float":
        return float(value)
    if kind == "date":
        if isinstance(value, _dt.datetime):
            return value
        if isinstance(value, _dt.date):
            return _dt.datetime(value.year, value.month, value.day)
        return _dt.datetime.strptime(str(value)[:10], "%Y-%m-%d")
    return str(value)


def _set_first(dataset, field_names: tuple[str, ...], value, kind="str") -> str:
    """Set the first field that accepts the value; fail loudly - with the
    REAL underlying errors - if none do.

    Two hard-won field lessons (The Voice Karaoke, 2026-07-25) baked in:

    - Set via .Value first, the way the official SDK samples do (and the
      way read_masters() successfully READS on this same machine). The
      typed AsString/AsFloat/AsDateTime setters this used to rely on are
      not dependably settable through the COM wrapper - and dates passed
      as 'YYYY-MM-DD' strings fail type conversion regardless (_coerce
      turns them into real datetimes, which pywin32 marshals as COM dates).

    - NEVER swallow the assignment error and report 'field does not exist'.
      That exact lie cost two live-post attempts: the field was there all
      along, the VALUE ASSIGNMENT was failing, and the blanket
      except/continue rewrote the real error into a misleading one.

    Candidate names still match case-insensitively against the dataset's
    real fields (belt), then FindField is called with the exact stored
    casing (braces).
    """
    real = _real_field_names(dataset)
    coerced = _coerce(value, kind)
    typed_setter = {"float": "AsFloat", "date": "AsDateTime"}.get(kind, "AsString")
    attempts: list[str] = []
    for name in field_names:
        actual = real.get(name.upper())
        if actual is None:
            attempts.append(f"{name}: not a field on this dataset")
            continue
        try:
            f = dataset.FindField(actual)
        except Exception as e:
            attempts.append(f"{actual}: FindField failed: {e}")
            continue
        for setter in ("Value", typed_setter):
            try:
                setattr(f, setter, coerced)
                return actual
            except Exception as e:
                attempts.append(f"{actual}.{setter} = {coerced!r}: {e}")
    raise RuntimeError(
        f"Could not set any of {field_names} — attempts: "
        + "; ".join(attempts)
        + f" (dataset's real fields: {sorted(real.values())})")


def _post_one(app, inv: dict) -> None:
    """Post one grouped document into its SQL module."""
    doc_type, sql_doc = inv["doc_type"], inv["sql_doc"]
    biz, resolved = _find_biz(app, doc_type, sql_doc)
    if biz is None:
        raise ValueError(
            f"'{doc_type}' has no matching SQL module on this install (tried "
            f"{DOC_TYPE_TO_SQL_CANDIDATES.get(doc_type, (sql_doc,))}) — most "
            "likely that module is switched off for this company: check "
            "File > Customize SQL Account Modules in SQL Accounting.")
    biz.New()
    main = biz.DataSets.Find("MainDataSet")

    if doc_type != "journal":
        _set_first(main, ("Code",), inv["supplier_code"])
    _set_first(main, ("DocDate",), inv["doc_date"], kind="date")
    try:
        _set_first(main, ("PostDate",), inv["doc_date"], kind="date")
    except RuntimeError:
        pass
    # Journals: doc_no here is our own internal grouping key (e.g. the
    # synthetic TAKINGS-YYYYMMDD tag a daily-takings sheet gets split under),
    # never a real journal voucher number — SQL auto-numbers these instead,
    # matching how an accountant's manually-keyed entries already look.
    if inv["doc_no"] and doc_type != "journal":
        try:
            _set_first(main, ("DocNo",), inv["doc_no"])
        except RuntimeError:
            pass  # leave auto-numbering in charge
    if doc_type == "journal":
        # Header description, so the voucher list reads like an
        # accountant's own entries ('SALARY - JUN'26') instead of blank.
        try:
            _set_first(main, ("Description",),
                       f"DAILY TAKINGS {inv['doc_date']}")
        except RuntimeError:
            pass

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
    if doc_type == "journal":
        # Two shapes share this one journal document:
        #  - a line WITH contra_account is its own self-balancing pair
        #    (debit account_code / credit contra_account, or the reverse
        #    for a negative amount) — the simple single-line case.
        #  - a line WITHOUT contra_account is one side of a multi-line
        #    group (e.g. a daily-takings sheet split into revenue/tax/
        #    payment lines by kira/ingest.py): posted as a single debit or
        #    credit, relying on the OTHER blank-contra lines in this same
        #    document to balance it. The console's approve gate already
        #    checked this nets to ~zero; re-checked here too, since posting
        #    must never trust an upstream check blindly.
        solo = [l for l in inv["lines"] if not l["contra_account"]]
        if solo:
            off = sum(l["amount"] for l in solo)
            if abs(off) > 0.02:
                raise ValueError(
                    f"journal lines with no contra_account don't net to "
                    f"zero (off by RM {abs(off):,.2f}) — refusing to post "
                    "an unbalanced entry (fix it in the console and "
                    "re-approve)")
        def _journal_line(account: str, description: str, amount: float,
                          debit: bool) -> None:
            """One detail row: amount into DR or CR AND its LOCALDR/LOCALCR
            twin (SQL Account is multi-currency internally; 'local' is the
            MYR side - 2026-07-25 field bug: setting only DR/CR produced
            zero-amount vouchers, headers saved but every figure 0.00).
            Then READ THE AMOUNT BACK - if it didn't stick, refuse to save,
            because a voucher full of zeros in a live ledger is worse than
            a failed batch."""
            detail.Append()
            _set_first(detail, ("Account", "AccNo", "Code"), account)
            _set_first(detail, ("Description",), description)
            names = ("DR", "Debit", "LOCALDR") if debit else \
                    ("CR", "Credit", "LOCALCR")
            actual = _set_first(detail, names[:2], amount, kind="float")
            try:
                _set_first(detail, (names[2],), amount, kind="float")
            except RuntimeError:
                pass  # single-currency installs may not expose the twin
            got = detail.FindField(actual).Value
            if abs(float(got or 0) - amount) > 0.01:
                raise ValueError(
                    f"amount did not stick: wrote {amount} to {actual}, "
                    f"read back {got!r} - refusing to save a zero/garbled "
                    "voucher (field mapping needs adjusting for this SQL "
                    "version)")
            detail.Post()

        for line in inv["lines"]:
            amt = line["amount"]
            _journal_line(line["account_code"], line["description"],
                          abs(amt), debit=amt >= 0)
            if line["contra_account"]:
                _journal_line(line["contra_account"], line["description"],
                              abs(amt), debit=amt < 0)
    else:
        for line in inv["lines"]:
            detail.Append()
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
                doc_types: tuple[str, ...] = tuple(DOC_TYPE_TO_SQL.keys())
                ) -> dict[str, dict[str, list[str]]]:
    """Go-live spike helper: list the actual field names of every module's
    datasets on the live machine, so the mappings above can be confirmed.
    Tries every naming candidate per doc_type (see DOC_TYPE_TO_SQL_CANDIDATES)
    before giving up, same resolution _post_one uses when actually posting.
    Run from the SQL PC with SQL Accounting installed."""
    import win32com.client

    app = win32com.client.Dispatch("SQLAcc.BizApp")
    if not app.IsLogin:
        app.Login(cfg.user, cfg.password, cfg.dcf_path, cfg.fdb_name)
    result: dict[str, dict[str, list[str]]] = {}
    for doc_type in doc_types:
        default = DOC_TYPE_TO_SQL[doc_type]
        biz, resolved = _find_biz(app, doc_type, default)
        sql_doc = f"{doc_type} ({resolved or default})"
        result[sql_doc] = {}
        if biz is None:
            tried = DOC_TYPE_TO_SQL_CANDIDATES.get(doc_type, (default,))
            result[sql_doc]["<unavailable>"] = [
                f"none of {tried} resolved — likely switched off for this "
                "company (File > Customize SQL Account Modules)"]
            continue
        try:
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


# ------------------- master data: SQL -> Kira (reverse feed) -------------------
# The Agent sits on the SQL PC with an SDK login — it can READ the client's
# chart of accounts, suppliers, customers, and tax codes straight out of SQL
# and push them to Kira Cloud. Nobody should export CSVs by hand; the manual
# "Add masters" upload in the console is the fallback, not the main path.
#
# Table/column names follow the official SDK wiki samples (DBManager.NewDataSet
# on GL_MAST / AP_SUPPLIER / AR_CUSTOMER / TAX). Variants are tried in order
# and the first query that works wins — verified for the site's SQL version
# at the same go-live step as posting field names.

MASTER_QUERIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "chart_of_accounts.csv": ((
        # GL_ACC confirmed live on SQL Account ERP Edition 5.2026.1078.893
        # (The Voice Karaoke Sdn Bhd, 2026-07-24) via the catalog fallback -
        # tried first now. GL_MAST (the official SDK wiki's sample name)
        # kept as a fallback guess for older/other editions.
        "SELECT CODE, DESCRIPTION, ACCTYPE FROM GL_ACC",
        "SELECT CODE, DESCRIPTION FROM GL_ACC",
        "SELECT CODE, DESCRIPTION, SPECIALTYPE FROM GL_MAST",
        "SELECT CODE, DESCRIPTION, ACCTYPE FROM GL_MAST",
        "SELECT CODE, DESCRIPTION FROM GL_MAST",
    ), ("code", "description", "type")),
    "suppliers.csv": ((
        "SELECT CODE, COMPANYNAME FROM AP_SUPPLIER",
        "SELECT CODE, NAME FROM AP_SUPPLIER",
    ), ("code", "name")),
    "customers.csv": ((
        "SELECT CODE, COMPANYNAME FROM AR_CUSTOMER",
        "SELECT CODE, NAME FROM AR_CUSTOMER",
    ), ("code", "name")),
    "tax_codes.csv": ((
        "SELECT CODE, DESCRIPTION, RATE FROM TAX",
        "SELECT CODE, DESCRIPTION, TAXRATE FROM TAX",
        "SELECT CODE, DESCRIPTION FROM TAX",
    ), ("code", "description", "rate")),
}

# When every guessed table name in MASTER_QUERIES fails (e.g. this edition
# calls the chart of accounts something other than GL_MAST), fall back to
# asking Firebird's own system catalog for real table names matching these
# patterns, then try a plain CODE/DESCRIPTION select against each.
GL_TABLE_HINTS: dict[str, tuple[str, ...]] = {
    "chart_of_accounts.csv": ("%GL%", "%ACCOUNT%", "%COA%"),
    "suppliers.csv": ("%SUPPLIER%",),
    "customers.csv": ("%CUSTOMER%",),
    "tax_codes.csv": ("%TAX%",),
}


def _query_records(app, query: str, out_cols: tuple[str, ...]) -> list[dict]:
    src_cols = [c.strip() for c in
                query.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
    ds = app.DBManager.NewDataSet(query)
    recs: list[dict] = []
    ds.First()
    while not ds.Eof:
        rec = {}
        for out_c, src_c in zip(out_cols, src_cols):
            try:
                v = ds.FindField(src_c).Value
            except Exception:
                v = ""
            rec[out_c] = "" if v is None else str(v).strip()
        # pad optional columns the query variant didn't include
        for out_c in out_cols[len(src_cols):]:
            rec[out_c] = ""
        if rec[out_cols[0]]:
            recs.append(rec)
        ds.Next()
    return recs


def _find_tables_like(app, patterns: tuple[str, ...]) -> list[str]:
    """Ask Firebird's own system catalog for real table names matching any
    of the given LIKE patterns — used when our guessed table name is wrong,
    instead of guessing again. Never raises; returns [] on any failure."""
    found: list[str] = []
    try:
        where = " OR ".join(f"RDB$RELATION_NAME LIKE '{p}'" for p in patterns)
        ds = app.DBManager.NewDataSet(
            f"SELECT RDB$RELATION_NAME FROM RDB$RELATIONS "
            f"WHERE RDB$SYSTEM_FLAG = 0 AND ({where})")
        ds.First()
        while not ds.Eof:
            name = ds.FindField("RDB$RELATION_NAME").Value
            if name:
                found.append(str(name).strip())
            ds.Next()
    except Exception:
        return []
    return found


def read_masters(cfg: SQLConfig) -> tuple[dict[str, list[dict]], str]:
    """Read this company's master data out of SQL Accounting via the SDK.

    Returns (masters, error). masters maps our CSV filenames to record lists;
    error is "" on success, otherwise a short human-readable reason (SDK
    missing, login rejected, ...). Never raises — master sync is best-effort
    and must not stop the Agent from doing its main job.
    """
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("SQLAcc.BizApp")
        app.Login(cfg.user, cfg.password, cfg.dcf_path, cfg.fdb_name)
    except Exception as e:
        return {}, f"SDK login failed: {e}"
    masters: dict[str, list[dict]] = {}
    problems: list[str] = []
    notes: list[str] = []
    for fname, (queries, out_cols) in MASTER_QUERIES.items():
        for q in queries:
            try:
                masters[fname] = _query_records(app, q, out_cols)
                break
            except Exception as e:
                last = str(e)
        else:
            # None of our guessed table names worked — ask Firebird's own
            # catalog what tables actually exist that look like this one,
            # and try CODE/DESCRIPTION against each real candidate.
            like = GL_TABLE_HINTS.get(fname)
            candidates = _find_tables_like(app, like) if like else []
            for tbl in candidates:
                try:
                    masters[fname] = _query_records(
                        app, f"SELECT CODE, DESCRIPTION FROM {tbl}",
                        ("code", "description"))
                    notes.append(f"{fname}: our guessed table name(s) "
                                f"didn't exist, but found and used '{tbl}' "
                                "via the database catalog")
                    break
                except Exception as e:
                    last = str(e)
            if fname not in masters:
                hint = (f" (catalog also checked, candidates tried: "
                        f"{candidates})" if candidates else
                        " (catalog search found no likely table either)")
                problems.append(f"{fname}: {last}{hint}")
    if not masters:
        return {}, "; ".join(problems)
    parts = (["partial - " + "; ".join(problems)] if problems else []) + notes
    return masters, "; ".join(parts)
