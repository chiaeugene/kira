"""Generate realistic synthetic test data: a messy bookkeeper Excel and the
client master CSVs. Replace with real exports when available."""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "client_data" / "DEMO_CLIENT"
INBOX = ROOT / "inbox"
DATA.mkdir(parents=True, exist_ok=True)
INBOX.mkdir(exist_ok=True)

# --- Master data (as if exported from the client's SQL company) -------------
pd.DataFrame(
    [
        ("300-A001", "AMPANG HARDWARE SDN BHD"),
        ("300-B002", "BEST OFFICE SUPPLIES"),
        ("300-C003", "CITY PETROL STATION"),
        ("300-K004", "KEDAI RUNCIT AH SENG"),
        ("300-M005", "MAXIS BROADBAND SDN BHD"),
        ("300-T006", "TNB (TENAGA NASIONAL)"),
        ("300-S007", "SYARIKAT PERCETAKAN MAJU"),
    ],
    columns=["code", "name"],
).to_csv(DATA / "suppliers.csv", index=False)

pd.DataFrame(
    [
        ("610-000", "PURCHASES - HARDWARE & TOOLS", "COST OF SALES"),
        ("902-000", "OFFICE SUPPLIES & STATIONERY", "EXPENSE"),
        ("903-000", "PETROL, TOLL & PARKING", "EXPENSE"),
        ("904-000", "TELEPHONE & INTERNET", "EXPENSE"),
        ("905-000", "ELECTRICITY & WATER", "EXPENSE"),
        ("906-000", "PRINTING & ADVERTISING", "EXPENSE"),
        ("907-000", "GENERAL EXPENSES", "EXPENSE"),
        ("908-000", "STAFF REFRESHMENT", "EXPENSE"),
    ],
    columns=["code", "description", "type"],
).to_csv(DATA / "chart_of_accounts.csv", index=False)

pd.DataFrame(
    [
        ("P", "PURCHASE SST 8%", "8"),
        ("PE", "PURCHASE EXEMPTED", "0"),
        ("NR", "NON-SST REGISTERED SUPPLIER", "0"),
    ],
    columns=["code", "description", "rate"],
).to_csv(DATA / "tax_codes.csv", index=False)

# --- Messy purchase listing (deliberately awful, like a real book) ----------
rows = [
    ["SYARIKAT MAJU JAYA - PURCHASE RECORD", None, None, None, None],
    ["Bulan: JUN 2026", None, None, None, None],
    [None, None, None, None, None],
    ["Tarikh", "Kedai / Pembekal", "Perkara", "No. Resit", "Jumlah (RM)"],
    ["3/6/2026", "Ampang Hardware", "cement 5 bag + paint", "INV-2231", "485.50"],
    ["3/6/2026", "kedai ah seng", "water for site workers", None, "38.90"],
    ["5/6/2026", "City Petrol", "minyak lori", "R-88123", "180.00"],
    [None, "Maxis", "internet bulan jun", "MX-99120", "129.00"],
    ["10/6/2026", "TNB", "bil elektrik kedai", "TNB-5567", "412.35"],
    ["12/6/2026", "Best Office Supplies", "A4 paper x10, ink", "BOS-4432", "245.80"],
    ["TOTAL", None, None, None, "1,491.55"],
    [None, None, None, None, None],
    ["15/6/2026", "Percetakan Maju", "print banner promosi", "PM-071", "350.00"],
    ["18/6/2026", "ampang hardware sdn bhd", "wire + plug", "INV-2299", "97.20"],
    ["20/6/2026", "City Petrol Station", "petrol + toll", None, "205.40"],
]
rows2 = [  # second sheet, totally different layout, English headers, junk at top
    [None, None, None, None],
    ["Petty cash JULY", None, None, None],
    ["Date", "Payee", "Purpose", "Amt"],
    ["2/7/2026", "Kedai Ah Seng", "drinking water", "42.50"],
    ["8/7/2026", "City Petrol Station", "diesel lori", "199.00"],
    ["subtotal", None, None, "241.50"],
]

with pd.ExcelWriter(INBOX / "june_purchases_MAJU_JAYA.xlsx") as xw:
    pd.DataFrame(rows).to_excel(xw, sheet_name="JUN", index=False, header=False)
    pd.DataFrame(rows2).to_excel(xw, sheet_name="PETTY CASH", index=False, header=False)
    # a notes sheet that must be skipped gracefully
    pd.DataFrame([["reminder: chase supplier statements"]]).to_excel(
        xw, sheet_name="NOTES", index=False, header=False)

# --- second client so the firm dashboard shows the multi-client story --------
DATA2 = ROOT / "client_data" / "SRI_MURNI_TRADING"
DATA2.mkdir(parents=True, exist_ok=True)
for f in ("suppliers.csv", "chart_of_accounts.csv", "tax_codes.csv"):
    (DATA2 / f).write_bytes((DATA / f).read_bytes())

print(f"Masters  -> {DATA} and {DATA2}")
print(f"Sample   -> {INBOX / 'june_purchases_MAJU_JAYA.xlsx'} (3 sheets)")
