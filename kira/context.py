"""Client context: chart of accounts, supplier master, tax codes.

Phase-0 loads these from CSV exports placed in the client's data_dir.
Export them from SQL Accounting (or via Firebird ODBC) with columns:

  chart_of_accounts.csv : code, description, type   (EXPENSE, COST OF SALES,
                          SALES/INCOME, BANK, CASH, ...)
  suppliers.csv         : code, name                (creditors)
  customers.csv         : code, name                (debtors — for sales &
                          customer payments; optional for purchase-only books)
  tax_codes.csv         : code, description, rate
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class ClientContext:
    name: str
    accounts: pd.DataFrame = field(default_factory=pd.DataFrame)
    suppliers: pd.DataFrame = field(default_factory=pd.DataFrame)
    customers: pd.DataFrame = field(default_factory=pd.DataFrame)
    tax_codes: pd.DataFrame = field(default_factory=pd.DataFrame)

    def _accounts_of(self, pattern: str) -> pd.DataFrame:
        if self.accounts.empty or "type" not in self.accounts.columns:
            return self.accounts
        mask = self.accounts["type"].str.upper().str.contains(pattern, na=False)
        picked = self.accounts[mask]
        return picked if not picked.empty else self.accounts

    def expense_accounts(self) -> pd.DataFrame:
        return self._accounts_of("EXPENSE|COST|PURCHASE")

    def income_accounts(self) -> pd.DataFrame:
        return self._accounts_of("SALES|INCOME|REVENUE")

    def money_accounts(self) -> pd.DataFrame:
        return self._accounts_of("BANK|CASH")

    def as_prompt_block(self) -> str:
        """Render master data compactly for the classification prompt."""
        buf = io.StringIO()
        buf.write("## Chart of accounts (code | description | type)\n")
        for _, r in self.accounts.iterrows():
            buf.write(f"{r['code']} | {r['description']} | {r.get('type', '')}\n")
        buf.write("\n## Suppliers / creditors (code | name)\n")
        for _, r in self.suppliers.iterrows():
            buf.write(f"{r['code']} | {r['name']}\n")
        buf.write("\n## Customers / debtors (code | name)\n")
        for _, r in self.customers.iterrows():
            buf.write(f"{r['code']} | {r['name']}\n")
        buf.write("\n## Tax codes (code | description | rate)\n")
        for _, r in self.tax_codes.iterrows():
            buf.write(f"{r['code']} | {r['description']} | {r.get('rate', '')}\n")
        return buf.getvalue()


# Real-world SQL Accounting exports name their columns all sorts of ways
# ("ACC. CODE", "Company Name", Malay headers...). Map them to ours instead
# of demanding an exact match — a mis-headed masters file must never take
# a client (or the whole console) down.
_HEADER_ALIASES: dict[str, list[str]] = {
    "code": ["code", "acc code", "acc. code", "acc.code", "account code",
             "account no", "account no.", "accno", "acc no", "gl code",
             "tax code", "supplier code", "customer code", "company code",
             "creditor code", "debtor code", "kod", "kod akaun"],
    "description": ["description", "desc", "account description",
                    "account name", "descriptions", "keterangan", "name"],
    "type": ["type", "acc type", "account type", "special type",
             "special account type", "category", "jenis"],
    "name": ["name", "company name", "supplier name", "customer name",
             "creditor name", "debtor name", "attention", "nama",
             "nama syarikat", "description"],
    "rate": ["rate", "tax rate", "rate %", "rate (%)", "percent", "%",
             "kadar", "kadar cukai"],
}

MASTER_COLUMNS: dict[str, tuple[list[str], list[str]]] = {
    # filename -> (required columns, optional columns)
    "chart_of_accounts.csv": (["code", "description"], ["type"]),
    "suppliers.csv": (["code", "name"], []),
    "customers.csv": (["code", "name"], []),
    "tax_codes.csv": (["code", "description"], ["rate"]),
}


def _normalize_headers(df: pd.DataFrame, required: list[str],
                       optional: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Rename recognizable header variants to our names.
    Returns (df, still_missing_required)."""
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    for target in required + optional:
        if target in df.columns:
            continue
        for alias in _HEADER_ALIASES.get(target, []):
            if alias in df.columns and alias not in required + optional:
                df = df.rename(columns={alias: target})
                break
    for target in optional:
        if target not in df.columns:
            df[target] = ""
    return df, [c for c in required if c not in df.columns]


def parse_master_upload(fname: str, content: bytes) -> pd.DataFrame:
    """Parse + normalize an uploaded master file (CSV or Excel export from
    SQL Accounting). Raises ValueError with a human-fixable message if the
    required columns cannot be recognized — checked at UPLOAD time, so a bad
    file is refused up front instead of breaking the client later."""
    required, optional = MASTER_COLUMNS[fname]
    try:
        if content[:4] == b"PK\x03\x04" or content[:4] == b"\xd0\xcf\x11\xe0":
            df = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
        else:
            df = pd.read_csv(io.BytesIO(content), dtype=str).fillna("")
    except Exception as e:
        raise ValueError(f"{fname}: could not read the file ({e}). Export it "
                         "from SQL Accounting as CSV or Excel.")
    df, missing = _normalize_headers(df, required, optional)
    if missing:
        raise ValueError(
            f"{fname}: could not find column(s) {missing} — the file has "
            f"{list(df.columns)}. Expected headers like "
            f"{', '.join(required + optional)} (SQL Accounting's own export "
            "headers are recognized too).")
    df = df[required + optional].astype(str)
    df = df[df[required[0]].str.strip() != ""].reset_index(drop=True)
    return df


def _read_csv(path: Path, required: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=required)
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        # A corrupt master file degrades to "no masters" — the console keeps
        # working (AI just can't code against it) instead of erroring out.
        return pd.DataFrame(columns=required)
    df, missing = _normalize_headers(df, required, [])
    if missing:
        return pd.DataFrame(columns=required)
    return df


def load_client_context(name: str, data_dir: str | Path) -> ClientContext:
    d = Path(data_dir)
    return ClientContext(
        name=name,
        accounts=_read_csv(d / "chart_of_accounts.csv", ["code", "description"]),
        suppliers=_read_csv(d / "suppliers.csv", ["code", "name"]),
        customers=_read_csv(d / "customers.csv", ["code", "name"]),
        tax_codes=_read_csv(d / "tax_codes.csv", ["code", "description"]),
    )
