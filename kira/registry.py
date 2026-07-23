"""Multi-client registry — the firm view.

Each client is a folder under client_data/ holding its masters, learned
rules, audit log, and posted-document registry. A bookkeeping firm runs
dozens of these side by side.
"""

from __future__ import annotations

import re
from pathlib import Path

from .audit import AuditLog
from .context import ClientContext, load_client_context
from .rules import RuleStore

CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
MASTER_FILES = ("chart_of_accounts.csv", "suppliers.csv", "customers.csv",
                "tax_codes.csv")
_MASTER_HEADERS = {
    "chart_of_accounts.csv": "code,description,type\n",
    "suppliers.csv": "code,name\n",
    "customers.csv": "code,name\n",
    "tax_codes.csv": "code,description,rate\n",
}


def list_clients(base: str | Path = "client_data") -> list[str]:
    b = Path(base)
    if not b.exists():
        return []
    return sorted(d.name for d in b.iterdir() if d.is_dir())


def client_dir(name: str, base: str | Path = "client_data") -> Path:
    return Path(base) / name


def open_client(name: str, base: str | Path = "client_data"
                ) -> tuple[ClientContext, RuleStore, AuditLog]:
    d = client_dir(name, base)
    return (
        load_client_context(name, d),
        RuleStore(d),
        AuditLog(d),
    )


def create_client(name: str, base: str | Path = "client_data") -> Path:
    """Register a new client: makes it appear in the console's client list
    and in the Agent setup wizard's fetched list. Starts with empty master
    files (correct headers) — upload real ones with save_masters(), or edit
    later."""
    name = name.strip()
    if not CLIENT_NAME_RE.match(name):
        raise ValueError(
            "Client name may only contain letters, numbers, underscores and "
            "hyphens (no spaces or symbols) — this name must also be typed "
            "exactly into the Agent's config on the SQL PC.")
    d = client_dir(name, base)
    if d.exists():
        raise FileExistsError(f"A client named '{name}' already exists.")
    d.mkdir(parents=True)
    for fname, header in _MASTER_HEADERS.items():
        (d / fname).write_text(header, encoding="utf-8")
    return d


def save_masters(name: str, files: dict[str, bytes],
                 base: str | Path = "client_data") -> list[str]:
    """files: {filename: raw_csv_bytes} for any of MASTER_FILES. Overwrites
    that file for the client. Returns the filenames actually saved."""
    d = client_dir(name, base)
    if not d.exists():
        raise FileNotFoundError(
            f"Client '{name}' does not exist — create it first.")
    saved = []
    for fname, content in files.items():
        base_name = Path(fname).name  # defend against a path in the filename
        if base_name not in MASTER_FILES:
            continue
        (d / base_name).write_bytes(content)
        saved.append(base_name)
    return saved


def firm_overview(base: str | Path = "client_data") -> list[dict]:
    """One status row per client for the firm dashboard."""
    rows = []
    for name in list_clients(base):
        ctx, store, audit = open_client(name, base)
        stats = audit.stats()
        rows.append({
            "client": name,
            "suppliers": len(ctx.suppliers),
            "accounts": len(ctx.accounts),
            "learned_rules": len(store.rules),
            "batches_posted": stats["batches"],
            "lines_posted": stats["lines"],
            "total_rm": stats["total_rm"],
            "auto_accuracy": stats["accuracy"],
        })
    return rows
