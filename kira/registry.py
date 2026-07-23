"""Multi-client registry — the firm view.

Each client is a folder under client_data/ holding its masters, learned
rules, audit log, and posted-document registry. A bookkeeping firm runs
dozens of these side by side.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .audit import AuditLog
from .context import (ClientContext, load_client_context,
                      parse_master_upload)
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


def register_client(name: str, base: str | Path = "client_data",
                    **meta) -> bool:
    """Idempotent create: used by the Agent to auto-discover a client from
    the SQL PC and push it to the cloud. Returns True if newly created,
    False if a client with this name already existed (left untouched — an
    Agent push never overwrites real master data the console already has)."""
    name = name.strip()
    if not CLIENT_NAME_RE.match(name):
        raise ValueError(
            "Client name may only contain letters, numbers, underscores and "
            "hyphens (no spaces or symbols).")
    d = client_dir(name, base)
    if d.exists():
        return False
    create_client(name, base)
    meta_path = d / "kira_meta.json"
    meta_path.write_text(json.dumps({
        "discovered_via": "agent",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **meta,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def client_meta(name: str, base: str | Path = "client_data") -> dict:
    p = client_dir(name, base) / "kira_meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def delete_client(name: str, base: str | Path = "client_data") -> None:
    """Irreversible — removes the client's masters, learned rules, audit
    trail, and posted-document registry. Does not touch batch queue records
    (they just show a client name that no longer resolves)."""
    d = client_dir(name, base)
    if not d.exists():
        raise FileNotFoundError(f"Client '{name}' does not exist.")
    shutil.rmtree(d)


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
        # Validate + normalize NOW (accepts CSV or Excel, recognizes SQL
        # Accounting's own header names) — a bad file is refused with a clear
        # message instead of silently breaking the client's coding later.
        df = parse_master_upload(base_name, content)
        df.to_csv(d / base_name, index=False)
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
