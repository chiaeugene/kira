"""Client context: chart of accounts, supplier master, tax codes.

Phase-0 loads these from CSV exports placed in the client's data_dir.
Export them from SQL Accounting (or via Firebird ODBC) with columns:

  chart_of_accounts.csv : code, description, type   (type e.g. EXPENSE, COST OF SALES)
  suppliers.csv         : code, name
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
    tax_codes: pd.DataFrame = field(default_factory=pd.DataFrame)

    def expense_accounts(self) -> pd.DataFrame:
        if self.accounts.empty or "type" not in self.accounts.columns:
            return self.accounts
        mask = self.accounts["type"].str.upper().str.contains(
            "EXPENSE|COST|PURCHASE", na=False
        )
        picked = self.accounts[mask]
        return picked if not picked.empty else self.accounts

    def as_prompt_block(self) -> str:
        """Render master data compactly for the classification prompt."""
        buf = io.StringIO()
        buf.write("## Chart of accounts (code | description | type)\n")
        for _, r in self.expense_accounts().iterrows():
            buf.write(f"{r['code']} | {r['description']} | {r.get('type', '')}\n")
        buf.write("\n## Suppliers (code | name)\n")
        for _, r in self.suppliers.iterrows():
            buf.write(f"{r['code']} | {r['name']}\n")
        buf.write("\n## Tax codes (code | description | rate)\n")
        for _, r in self.tax_codes.iterrows():
            buf.write(f"{r['code']} | {r['description']} | {r.get('rate', '')}\n")
        return buf.getvalue()


def _read_csv(path: Path, required: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=required)
    df = pd.read_csv(path, dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} is missing columns: {missing}")
    return df


def load_client_context(name: str, data_dir: str | Path) -> ClientContext:
    d = Path(data_dir)
    return ClientContext(
        name=name,
        accounts=_read_csv(d / "chart_of_accounts.csv", ["code", "description"]),
        suppliers=_read_csv(d / "suppliers.csv", ["code", "name"]),
        tax_codes=_read_csv(d / "tax_codes.csv", ["code", "description"]),
    )
