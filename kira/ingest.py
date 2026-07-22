"""Tolerant ingestion of messy bookkeeper Excel purchase listings.

Design goals:
- No template required. Header row may sit anywhere in the first ~15 rows.
- Column names vary wildly (English / Malay / Chinese / abbreviations).
- Junk rows (totals, blanks, section headers) are dropped.

Output: DataFrame with canonical columns
  date, supplier, description, amount, tax, doc_no, source_row

Entry points:
  parse_purchase_listing(path, sheet)  one sheet
  parse_workbook(path)                 every parseable sheet in a workbook / CSV
If heuristics can't find a layout and the Claude API is available, an LLM
layout-mapping fallback reads the first rows and maps the columns itself.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

# Canonical field -> regex alternatives seen in real bookkeeper files
COLUMN_PATTERNS: dict[str, list[str]] = {
    "date": [r"date", r"tarikh", r"trx\s*date", r"doc\s*date", r"日期"],
    # canonical "supplier" column = the PARTY on the line (supplier OR customer)
    "supplier": [
        r"supplier", r"vendor", r"creditor", r"pembekal", r"payee",
        r"beneficiary", r"company", r"name", r"供应商", r"kedai", r"shop",
        r"customer", r"debtor", r"pelanggan", r"client", r"客户",
        r"received\s*from", r"paid\s*to", r"daripada", r"kepada",
    ],
    "description": [
        r"desc", r"particular", r"detail", r"item", r"remark", r"perkara",
        r"keterangan", r"purpose", r"kerja", r"barang", r"butiran",
        r"备注", r"事项", r"项目",
    ],
    "amount": [
        r"amount", r"amt", r"total", r"jumlah", r"harga", r"rm\b", r"value",
        r"debit", r"dr\b", r"bayar", r"payment", r"金额", r"总额",
    ],
    "tax": [r"tax", r"sst", r"gst", r"cukai", r"税"],
    "credit": [r"credit", r"kredit", r"\bcr\b", r"refund", r"cn\b"],
    "doc_no": [
        r"doc\s*no", r"inv(oice)?\s*(no|#)?", r"ref", r"bill\s*no", r"no\.?\s*inv",
        r"receipt", r"resit", r"单号",
    ],
}

# Document-type hints, strongest signal first (sheet name / title rows).
# The AI refines per line; the human confirms in review. "" = let AI decide.
DOC_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("sale", r"sales|jualan|invoice.?s? issued|sales invoice|销售"),
    ("customer_payment", r"official receipt|receipt.?s? issued|collection|"
                         r"resit rasmi|payment received|terima|收款"),
    ("supplier_payment", r"payment voucher|baucar|bayaran keluar|payment out|"
                         r"paid to|付款"),
    ("purchase", r"purchase|belian|pembelian|expense|perbelanjaan|petty cash|"
                 r"supplier|bill|采购"),
]

_CUSTOMER_HEADER_RE = re.compile(r"customer|debtor|pelanggan|client|客户",
                                 re.IGNORECASE)
_SUPPLIER_HEADER_RE = re.compile(
    r"supplier|vendor|creditor|pembekal|kedai|shop|供应商", re.IGNORECASE)


def detect_doc_type(sheet_name: str, title_texts: list[str],
                    header_texts: list[str]) -> str:
    """Best-effort document-type hint for a sheet. '' = unknown (AI decides)."""
    strong = f"{sheet_name} " + " ".join(title_texts)
    for doc_type, pattern in DOC_TYPE_KEYWORDS:
        if re.search(pattern, strong, re.IGNORECASE):
            return doc_type
    headers = " ".join(str(h) for h in header_texts)
    if _CUSTOMER_HEADER_RE.search(headers):
        return "sale"
    if _SUPPLIER_HEADER_RE.search(headers):
        return "purchase"
    return ""


_AMOUNT_RE = re.compile(r"^-?\s*(rm)?\s*[\d,]+(\.\d{1,2})?\s*$", re.IGNORECASE)
_JUNK_ROW_RE = re.compile(
    r"^(total|subtotal|grand\s*total|jumlah|balance|b/f|c/f)\b", re.IGNORECASE
)


def _match_column(header: str) -> str | None:
    h = str(header).strip().lower()
    if not h or h.startswith("unnamed"):
        return None
    for canon, patterns in COLUMN_PATTERNS.items():
        for p in patterns:
            if re.search(p, h):
                return canon
    return None


def _score_header_row(values: list) -> int:
    return sum(1 for v in values if _match_column(v))


def _find_header_row(raw: pd.DataFrame, scan_rows: int = 15) -> int | None:
    best_row, best_score = None, 0
    for i in range(min(scan_rows, len(raw))):
        score = _score_header_row(list(raw.iloc[i]))
        if score > best_score:
            best_row, best_score = i, score
    return best_row if best_score >= 2 else None


def _clean_amount(v) -> float | None:
    if pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip().replace(",", "")
    s = re.sub(r"(?i)^rm\s*", "", s)
    s = s.replace("(", "-").replace(")", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _clean_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return ""
    return str(v).strip()


def _clean_date(v):
    if pd.isna(v):
        return None
    # Excel serial date numbers (e.g. 46085 = 2026-06-07)
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:
        return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(v))).date()
    ts = pd.to_datetime(v, dayfirst=True, errors="coerce")  # MY convention: d/m/y
    return None if pd.isna(ts) else ts.date()


def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


_LAYOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "header_row": {"type": "integer",
                       "description": "0-based index of the header row, -1 if none"},
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "0-based column index"},
                    "field": {"type": "string",
                              "enum": ["date", "supplier", "description",
                                       "amount", "tax", "doc_no", "ignore"]},
                },
                "required": ["index", "field"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["header_row", "columns"],
    "additionalProperties": False,
}


def _llm_map_layout(raw: pd.DataFrame) -> tuple[int, dict[int, str]] | None:
    """Fallback: ask Claude to identify the header row and column meanings
    when heuristics fail. Returns (header_row, {col_idx: canonical}) or None."""
    if not _llm_available():
        return None
    import anthropic

    preview = raw.head(20).fillna("").astype(str)
    grid = "\n".join(
        f"row {i}: " + " || ".join(preview.iloc[i]) for i in range(len(preview))
    )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2000,
        system=(
            "You are analyzing the first rows of a Malaysian bookkeeper's "
            "spreadsheet (headers may be English/Malay/Chinese or absent). "
            "Identify which row holds the column headers and what each column "
            "means. If there is no header row but the data layout is obvious, "
            "set header_row to the row BEFORE the first data row (-1 if data "
            "starts at row 0) and still map the columns."
        ),
        output_config={"format": {"type": "json_schema", "schema": _LAYOUT_SCHEMA}},
        messages=[{"role": "user", "content": grid}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    mapping = {c["index"]: c["field"] for c in data["columns"]
               if c["field"] != "ignore"}
    if "amount" not in mapping.values():
        return None
    return data["header_row"], mapping


def _read_raw(path: str | Path, sheet: int | str = 0) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p, header=None, dtype=object)
    return pd.read_excel(p, sheet_name=sheet, header=None, dtype=object)


def parse_purchase_listing(path: str | Path, sheet: int | str = 0) -> pd.DataFrame:
    """Parse one messy Excel/CSV sheet into canonical purchase rows."""
    raw = _read_raw(path, sheet)
    header_row = _find_header_row(raw)
    mapping: dict[int, str] = {}

    if header_row is not None:
        headers = list(raw.iloc[header_row])
        used: set[str] = set()
        for idx, h in enumerate(headers):
            canon = _match_column(h)
            if canon and canon not in used:
                mapping[idx] = canon
                used.add(canon)
    else:
        llm_result = _llm_map_layout(raw)
        if llm_result is None:
            raise ValueError(
                f"Could not locate a header row in {path} — heuristics failed and "
                "the AI layout fallback is unavailable (set ANTHROPIC_API_KEY)."
            )
        header_row, mapping = llm_result

    body = raw.iloc[header_row + 1:].reset_index(drop=True)
    rows = []
    declared_totals: list[float] = []
    for i, row in body.iterrows():
        rec = {canon: row.iloc[idx] for idx, canon in mapping.items()}

        # Repeated header rows (books re-print headers at page breaks)
        if _score_header_row(list(row)) >= 2:
            continue

        # TOTAL / subtotal / balance rows: skip, but capture the declared
        # figure so we can reconcile at the end (no silent row loss).
        lead_cells = [str(row.iloc[j]).strip() for j in range(min(3, len(row)))
                      if not pd.isna(row.iloc[j])]
        if any(_JUNK_ROW_RE.match(c) for c in lead_cells):
            declared = _clean_amount(rec.get("amount"))
            if declared:
                declared_totals.append(declared)
            continue

        amount = _clean_amount(rec.get("amount"))
        credit = _clean_amount(rec.get("credit"))
        if (amount is None or amount == 0) and credit:
            amount = -abs(credit)  # credit note / refund column
        if amount is None or amount == 0:
            continue  # not a transaction line
        rows.append(
            {
                "date": _clean_date(rec.get("date")),
                "supplier": _clean_str(rec.get("supplier")),
                "description": _clean_str(rec.get("description")),
                "amount": amount,
                "tax": _clean_amount(rec.get("tax")) or 0.0,
                "doc_no": _clean_str(rec.get("doc_no")),
                "source_row": header_row + 2 + i,  # 1-based Excel row
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"No transaction rows recognized in {path}.")
    # Rows with no date inherit the nearest earlier date (common in real books)
    df["date"] = df["date"].ffill()
    # Merged-cell books: supplier written once for a run of lines below it
    df["supplier"] = df["supplier"].replace("", pd.NA).ffill().fillna("")
    # Document-type hint from the sheet's own words (AI refines per line)
    sheet_label = str(sheet) if not isinstance(sheet, int) else Path(path).stem
    titles = [" ".join(_clean_str(v) for v in raw.iloc[i] if _clean_str(v))
              for i in range(min(max(header_row, 0), 5))]
    header_texts = ([_clean_str(h) for h in raw.iloc[header_row]]
                    if 0 <= header_row < len(raw) else [])
    df["doc_type_hint"] = detect_doc_type(sheet_label, titles, header_texts)
    # Conversion-integrity data for reconciliation (read via df.attrs)
    df.attrs["declared_totals"] = declared_totals
    df.attrs["parsed_total"] = round(float(df["amount"].sum()), 2)
    return df


def _recon_note(df: pd.DataFrame) -> str:
    """Reconcile parsed sum against the book's own TOTAL rows — a mismatch
    means either subtotals-only in the book (common) or a missed row (bad),
    so it is surfaced, never swallowed."""
    declared = df.attrs.get("declared_totals") or []
    if not declared:
        return ""
    parsed = df.attrs["parsed_total"]
    candidates = {round(sum(declared), 2), round(max(declared), 2),
                  round(declared[-1], 2)}
    if any(abs(parsed - c) <= 0.02 for c in candidates):
        return " ✓ ties to the book's own total"
    return (f" ⚠ book declares total(s) {declared} but parsed RM {parsed:,.2f}"
            " — verify no rows were missed (may just be a partial subtotal)")


def parse_workbook(path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """Parse every parseable sheet of a workbook (or a CSV).

    Returns (combined rows, notes). Sheets that don't contain transactions
    are skipped with a note rather than failing the whole file.
    """
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return parse_purchase_listing(p), []

    xl = pd.ExcelFile(p)
    frames, notes = [], []
    for name in xl.sheet_names:
        try:
            part = parse_purchase_listing(p, sheet=name)
            part["source_sheet"] = name
            frames.append(part)
            notes.append(f"'{name}': {len(part)} rows, RM {part.attrs['parsed_total']:,.2f}"
                         + _recon_note(part))
        except ValueError as e:
            notes.append(f"'{name}': skipped ({e})")
    if not frames:
        raise ValueError(
            f"No sheet in {p.name} contained recognizable transactions. "
            + "; ".join(notes)
        )
    return pd.concat(frames, ignore_index=True), notes
