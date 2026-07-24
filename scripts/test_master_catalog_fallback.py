"""Regression test for the reverse master feed's catalog fallback.

Field bug: The Voice Karaoke's SQL Account edition doesn't have a GL_MAST
table (our guessed name for chart of accounts) - it errored "Table unknown".
Suppliers/customers/tax codes synced fine because AP_SUPPLIER/AR_CUSTOMER/TAX
happened to be right. This test proves that when a guessed table is wrong,
read_masters() asks Firebird's own system catalog for the real table name
and still gets the data - entirely mocked, no real SQL Accounting needed.

Run:  python scripts/test_master_catalog_fallback.py
"""
from __future__ import annotations

import fnmatch
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeField:
    def __init__(self, value):
        self.Value = value


class FakeDataSet:
    def __init__(self, rows):
        self.rows = rows
        self.idx = 0

    @property
    def Eof(self):
        return self.idx >= len(self.rows)

    def First(self):
        self.idx = 0

    def Next(self):
        self.idx += 1

    def FindField(self, name):
        return FakeField(self.rows[self.idx].get(name.upper()))


class FakeDBManager:
    def __init__(self, tables: dict, catalog: list[str]):
        self.tables = tables  # TABLE_NAME (upper) -> list of row dicts
        self.catalog = catalog  # real table names Firebird "knows about"

    def NewDataSet(self, query: str):
        q = query.upper()
        if "RDB$RELATIONS" in q:
            # Real Firebird filters by the LIKE patterns in the WHERE clause -
            # mimic that instead of returning the whole catalog unfiltered.
            raw_patterns = re.findall(r"LIKE '([^']+)'", query)
            glob_patterns = [p.replace("%", "*") for p in raw_patterns]
            matched = [n for n in self.catalog
                      if any(fnmatch.fnmatchcase(n, gp) for gp in glob_patterns)]
            return FakeDataSet([{"RDB$RELATION_NAME": n} for n in matched])
        m = re.search(r"FROM\s+(\S+)", query, re.IGNORECASE)
        table = m.group(1).upper() if m else None
        if table not in self.tables:
            raise Exception(f"Dynamic SQL Error\nTable unknown\n{table}")
        # Real Firebird also errors on a column that doesn't exist on the
        # table - check that here so a wrong-table false-positive can't slip
        # through the mock the way a real database would never allow.
        wanted_cols = [c.strip().upper() for c in
                      query.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        have_cols = set(self.tables[table][0].keys()) if self.tables[table] else set()
        missing = [c for c in wanted_cols if c not in have_cols]
        if missing:
            raise Exception(f"Dynamic SQL Error\nColumn unknown\n{missing[0]}")
        return FakeDataSet(self.tables[table])


class FakeApp:
    def __init__(self, dbm: FakeDBManager):
        self.DBManager = dbm

    def Login(self, *a, **k):
        pass


def install_fake_sdk(app: FakeApp) -> None:
    fake_pythoncom = types.ModuleType("pythoncom")
    fake_pythoncom.CoInitialize = lambda: None
    sys.modules["pythoncom"] = fake_pythoncom

    fake_win32com = types.ModuleType("win32com")
    fake_client = types.ModuleType("win32com.client")
    fake_client.Dispatch = lambda name: app
    fake_win32com.client = fake_client
    sys.modules["win32com"] = fake_win32com
    sys.modules["win32com.client"] = fake_client


# Our guessed GL_MAST does NOT exist on this fictional install - the real
# chart-of-accounts table is called ACC_GL, which our guess list never had.
dbm = FakeDBManager(
    tables={
        "ACC_GL": [
            {"CODE": "300-000", "DESCRIPTION": "SALES"},
            {"CODE": "600-000", "DESCRIPTION": "COST OF SALES"},
        ],
        "AP_SUPPLIER": [{"CODE": "S001", "COMPANYNAME": "Ampang Hardware"}],
        "AR_CUSTOMER": [{"CODE": "C001", "COMPANYNAME": "Walk-in"}],
        "TAX": [{"CODE": "SR", "DESCRIPTION": "Standard Rated", "RATE": "6"}],
    },
    catalog=["ACC_GL", "AP_SUPPLIER", "AR_CUSTOMER", "TAX", "SOME_OTHER_TABLE"],
)
install_fake_sdk(FakeApp(dbm))

import kira.poster as poster  # noqa: E402

masters, err = poster.read_masters(
    poster.SQLConfig(user="ADMIN", password="ADMIN",
                     dcf_path="C:/fake.DCF", fdb_name="ACC-0001.FDB"))

assert masters, f"expected some masters, got none - err={err}"
assert "chart_of_accounts.csv" in masters, \
    f"catalog fallback should have found ACC_GL - masters={masters.keys()} err={err}"
coa = masters["chart_of_accounts.csv"]
assert len(coa) == 2 and coa[0]["code"] == "300-000", coa
print(f"1. GL_MAST guess failed, catalog fallback found ACC_GL -> "
      f"{len(coa)} account(s)  OK")

assert len(masters["suppliers.csv"]) == 1
assert len(masters["customers.csv"]) == 1
assert len(masters["tax_codes.csv"]) == 1
print("2. suppliers/customers/tax codes read via the first-guess query  OK")

assert "ACC_GL" in err and "catalog" in err, err
print(f"3. success note explains the fallback: {err!r}  OK")

# --- second scenario: catalog fallback finds NOTHING for chart of accounts
dbm2 = FakeDBManager(
    tables={
        "AP_SUPPLIER": [{"CODE": "S001", "COMPANYNAME": "X"}],
        "AR_CUSTOMER": [{"CODE": "C001", "COMPANYNAME": "Y"}],
        "TAX": [{"CODE": "SR", "DESCRIPTION": "Standard", "RATE": "6"}],
    },
    catalog=["AP_SUPPLIER", "AR_CUSTOMER", "TAX"],  # no GL-ish table at all
)
install_fake_sdk(FakeApp(dbm2))
import importlib
importlib.reload(poster)
masters2, err2 = poster.read_masters(
    poster.SQLConfig(user="ADMIN", password="ADMIN",
                     dcf_path="C:/fake.DCF", fdb_name="ACC-0001.FDB"))
assert "chart_of_accounts.csv" not in masters2
assert len(masters2["suppliers.csv"]) == 1
assert "chart_of_accounts.csv" in err2 and "no likely table" in err2, err2
print("4. no GL-ish table anywhere -> reported clearly, other masters still saved  OK")

print("\nAll master-catalog-fallback checks passed.")
