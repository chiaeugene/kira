"""Multi-client registry — the firm view.

Each client is a folder under client_data/ holding its masters, learned
rules, audit log, and posted-document registry. A bookkeeping firm runs
dozens of these side by side.
"""

from __future__ import annotations

from pathlib import Path

from .audit import AuditLog
from .context import ClientContext, load_client_context
from .rules import RuleStore


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
