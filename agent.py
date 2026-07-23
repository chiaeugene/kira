"""Kira Agent — runs on the PC where SQL Accounting is installed.

Outbound-only: polls Kira Cloud for approved batches, posts them into SQL
via the free official SDK, reports the result back. No inbound ports, so it
works behind any office router. Install once, forget it exists.

Setup is company-first, not name-first: --setup scans this PC for SQL
company files, and for each one you pick, the wizard best-effort reads its
name and PUSHES it to Kira Cloud (POST /api/clients/register) — creating the
client there automatically. You never type a client name into the console
by hand just to make it match; you only ever pick from what's on this PC.

Run:   python agent.py            (continuous; also what KiraAgent.exe runs)
       python agent.py --once     (single poll — tests / task scheduler)

The console window IS the local dashboard: it shows a startup summary and a
live line for everything the Agent does. The same lines are appended to
kira_agent.log next to the program — the permanent local trail.

Config: agent_config.yaml (same folder), overridable via .env / env vars
KIRA_SERVER_URL and KIRA_AGENT_TOKEN.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

from kira.batches import records_to_df
from kira.envfile import load_env
from kira.poster import SQLConfig, post_batch

load_env()  # .env can carry KIRA_SERVER_URL / KIRA_AGENT_TOKEN

log = logging.getLogger("kira.agent")


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    filed = logging.FileHandler("kira_agent.log", encoding="utf-8")
    filed.setFormatter(fmt)
    log.addHandler(out)
    log.addHandler(filed)


def load_cfg(path: str = "agent_config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cfg["server_url"] = os.environ.get("KIRA_SERVER_URL", cfg["server_url"])
    cfg["agent_token"] = os.environ.get("KIRA_AGENT_TOKEN", cfg["agent_token"])
    return cfg


def banner(cfg: dict) -> None:
    log.info("=" * 62)
    log.info("KIRA AGENT - %s", cfg.get("agent_name", "agent"))
    log.info("Cloud:   %s", cfg["server_url"])
    log.info("Polling: every %ss", cfg.get("poll_seconds", 30))
    for name, c in cfg["clients"].items():
        mode = "DRY RUN (simulate only)" if c.get("dry_run", True) else "LIVE POSTING"
        log.info("Serves:  %-24s -> %s  [%s]", name, c.get("fdb_name", "?"), mode)
    log.info("Log trail: %s", Path("kira_agent.log").resolve())

    cloud_clients = fetch_cloud_clients(cfg["server_url"], cfg["agent_token"])
    if cloud_clients:
        known = {c["name"] for c in cloud_clients}
        unknown = set(cfg["clients"]) - known
        if unknown:
            log.error("Configured client(s) not found in Kira Cloud: %s",
                      ", ".join(sorted(unknown)))
            log.error("  Check spelling matches the console exactly — "
                      "batches for these will never be found.")

    log.info("Keep this window open. Close it to stop the Agent.")
    log.info("=" * 62)


def poll_once(cfg: dict, client: httpx.Client | None = None) -> str:
    """One poll cycle. Returns 'idle', 'posted', or 'failed'."""
    http = client or httpx.Client(base_url=cfg["server_url"], timeout=60)
    headers = {"Authorization": f"Bearer {cfg['agent_token']}"}

    r = http.post("/api/agent/poll", headers=headers, json={
        "agent_name": cfg.get("agent_name", "agent"),
        "clients": list(cfg["clients"].keys()),
        "modes": {c: ("dry_run" if v.get("dry_run", True) else "live")
                  for c, v in cfg["clients"].items()},
    })
    r.raise_for_status()
    job = r.json()
    if not job.get("batch_id"):
        return "idle"

    bid, client_name = job["batch_id"], job["client"]
    log.info("BATCH RECEIVED  %s | client %s | %s lines | RM %s",
             bid, client_name, len(job["rows"]), f"{job['total_rm']:,.2f}")

    sql_cfg = SQLConfig(**cfg["clients"][client_name])
    try:
        df = records_to_df(job["rows"])
        result = post_batch(df, sql_cfg, out_dir="posted")
        ok = len(result.get("errors", [])) == 0
        for inv in result.get("posted", []):
            log.info("  posted invoice: %s | %s | %s line(s)",
                     inv["supplier_code"], inv["doc_no"] or "(auto no.)",
                     len(inv["lines"]))
        for e in result.get("errors", []):
            log.error("  INVOICE FAILED: %s", e)
    except Exception as e:  # report failures, never swallow them
        result = {"mode": "exception", "invoices": 0, "errors": [str(e)]}
        ok = False
        log.error("  BATCH ERROR: %s", e)

    rep = http.post("/api/agent/report", headers=headers, json={
        "batch_id": bid,
        "ok": ok,
        "mode": result["mode"],
        "invoices": result.get("invoices", 0),
        "errors": [str(e) for e in result.get("errors", [])],
    })
    rep.raise_for_status()
    log.info("BATCH %s  %s | %s invoice(s) | mode %s",
             "POSTED" if ok else "FAILED", bid,
             result.get("invoices", 0), result["mode"])
    return "posted" if ok else "failed"


# --------------------------- setup wizard ---------------------------

SCAN_ROOTS = [r"C:\eStream", r"D:\eStream", r"C:\SQLAccounting",
              r"C:\estream", r"C:\Program Files (x86)\eStream"]


def scan_sql_companies(roots: list[str] | None = None
                       ) -> tuple[list[Path], list[Path]]:
    """Find SQL Accounting DCF files and company .FDB databases on this PC."""
    dcfs: list[Path] = []
    fdbs: list[Path] = []
    for root in (roots or SCAN_ROOTS):
        r = Path(root)
        if not r.exists():
            continue
        try:
            dcfs += list(r.rglob("*.DCF"))
            fdbs += list(r.rglob("*.FDB"))
        except (PermissionError, OSError):
            continue
    return sorted(set(dcfs)), sorted(set(fdbs))


def _slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return (s or "CLIENT").upper()


def try_extract_company_label(user: str, password: str, dcf_path: str,
                              fdb_name: str) -> str | None:
    """Best-effort: log into this company via the SDK and read its name.

    The exact property name is unconfirmed until dump_fields()/a live-machine
    check verifies it for this SQL version — several candidates are tried,
    and any failure (SDK missing, wrong creds, unknown property) is silent.
    Falls back to the FDB filename when this returns None.
    """
    try:
        import win32com.client
        app = win32com.client.Dispatch("SQLAcc.BizApp")
        app.Login(user, password, dcf_path, fdb_name)
        for attr in ("CompanyName", "CoyName", "CompanyFullName", "CoName"):
            try:
                val = getattr(app, attr)
                if val:
                    return str(val)
            except Exception:
                continue
    except Exception:
        pass
    return None


def register_client_on_cloud(server: str, token: str, name: str,
                             label: str = "", fdb_name: str = "",
                             agent_name: str = "") -> dict | None:
    """Push a locally-discovered company to Kira Cloud. Idempotent — creates
    the client if new, or confirms a link if that name already exists
    (never overwrites real master data the console already has)."""
    try:
        r = httpx.post(f"{server}/api/clients/register",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"name": name, "label": label,
                             "fdb_name": fdb_name, "agent_name": agent_name},
                       timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Could not register with Kira Cloud: {e}")
        return None


def fetch_cloud_clients(server: str, token: str) -> list[dict]:
    """The Agent's own view of Kira Cloud's client list — read-only, uses
    the agent token (server accepts either token on this endpoint)."""
    try:
        r = httpx.get(f"{server}/api/clients",
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  (Could not fetch the client list from Kira Cloud: {e})")
        return []


def setup_wizard(config_path: str = "agent_config.yaml") -> bool:
    """Interactive first-run setup: scan for company files, map them to Kira
    clients, write agent_config.yaml. Returns True when a config was written."""
    print()
    print("KIRA AGENT SETUP")
    print("-" * 50)
    server = os.environ.get("KIRA_SERVER_URL", "")
    token = os.environ.get("KIRA_AGENT_TOKEN", "")
    if not server:
        server = input("Kira Cloud URL (e.g. https://kira-cloud.onrender.com): ").strip()
    else:
        print(f"Kira Cloud URL:  {server}   (from .env)")
    if not token:
        token = input("Agent token (from your Kira administrator): ").strip()
    else:
        print("Agent token:     (from .env)")
    agent_name = input("Name this PC (e.g. office-pc-1): ").strip() or "office-pc-1"

    print("\nFetching the client list from Kira Cloud...")
    cloud_clients = fetch_cloud_clients(server, token)
    known_names = {c["name"] for c in cloud_clients}
    if cloud_clients:
        print("Clients already set up in Kira Cloud:")
        for c in cloud_clients:
            print(f"  - {c['name']}  ({c['suppliers']} suppliers, "
                  f"{c.get('customers', 0)} customers)")
    else:
        print("  No clients set up yet — that's fine, picking a company "
              "below will create one automatically.")

    print("\nScanning this PC for SQL Accounting company files...")
    dcfs, fdbs = scan_sql_companies()
    if not dcfs or not fdbs:
        print("  No SQL Accounting files found in the usual folders.")
        print("  In SQL Accounting, open File -> Open Company to see the "
              "DCF path and company file names, then edit agent_config.yaml "
              "manually (see AGENT_SETUP.md).")
        return False
    dcf = dcfs[0]
    if len(dcfs) > 1:
        print("\nFound more than one DCF (company directory):")
        for i, d in enumerate(dcfs, 1):
            print(f"  {i}. {d}")
        pick = input(f"Which one? [1-{len(dcfs)}, Enter=1]: ").strip()
        dcf = dcfs[int(pick) - 1] if pick.isdigit() else dcfs[0]

    print(f"\nCompany databases found (via {dcf.name}):")
    for i, f in enumerate(fdbs, 1):
        print(f"  {i}. {f.name}   ({f})")

    clients: dict = {}
    print("\nFor each company you want Kira to post into: pick it from the "
          "list above, and Kira will register it with the cloud for you —")
    print("you won't need to create anything in the console first.")
    while True:
        pick = input(f"\nCompany number to set up [1-{len(fdbs)}, "
                     "Enter to finish]: ").strip()
        if not pick:
            break
        if not (pick.isdigit() and 1 <= int(pick) <= len(fdbs)):
            print("  Not a valid number — skipped.")
            continue
        fdb = fdbs[int(pick) - 1]

        user = input("  SQL Accounting username [ADMIN]: ").strip() or "ADMIN"
        password = input("  SQL Accounting password: ").strip()

        print("  Reading this company's details...")
        label = try_extract_company_label(user, password, str(dcf), fdb.name)
        if label:
            print(f"  Found company name: {label}")
        else:
            label = fdb.stem
            print("  Could not read the company name automatically "
                  f"(needs SQL Accounting installed here) — using '{label}' "
                  "as a placeholder label.")

        suggested = _slugify(label)
        if known_names:
            print(f"  Existing Kira clients: {', '.join(sorted(known_names))}")
        cname = input(f"  Kira client name [{suggested}] (type an existing "
                     "name to link instead): ").strip() or suggested

        reg = register_client_on_cloud(server, token, cname, label=label,
                                       fdb_name=fdb.name, agent_name=agent_name)
        if reg is None:
            print("  Not registered (offline?) — will still save locally; "
                  "re-run the wizard once you can reach Kira Cloud.")
        elif reg["created"]:
            print(f"  Registered new client '{cname}' in Kira Cloud — add "
                  "its chart of accounts / suppliers / customers / tax codes "
                  "in the console when convenient (AI coding improves once "
                  "you do; it still works on a fallback until then).")
            known_names.add(cname)
        else:
            print(f"  Linked to the existing client '{cname}' in Kira Cloud.")

        clients[cname] = {
            "dry_run": True,
            "user": user, "password": password,
            "dcf_path": str(dcf), "fdb_name": fdb.name,
        }
        print(f"  Mapped {cname} -> {fdb.name} (dry-run until the go-live test).")

    if not clients:
        print("No clients mapped — nothing written.")
        return False

    Path(config_path).write_text(yaml.safe_dump({
        "agent_name": agent_name, "server_url": server,
        "agent_token": token, "poll_seconds": 30, "clients": clients,
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"\nSaved {config_path}. Every company starts in DRY RUN — follow "
          "the go-live checklist in AGENT_SETUP.md before switching to live.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--setup", action="store_true",
                    help="run the interactive setup wizard")
    ap.add_argument("--config", default="agent_config.yaml")
    args = ap.parse_args()

    setup_logging()

    if args.setup or not Path(args.config).exists():
        if not args.setup:
            print("No agent_config.yaml found — starting first-time setup.")
        if not setup_wizard(args.config):
            if not args.once:
                input("Press Enter to close...")
            return 2
        if args.setup:
            return 0

    try:
        cfg = load_cfg(args.config)
    except FileNotFoundError:
        log.error("agent_config.yaml not found next to the program.")
        if not args.once:
            input("Press Enter to close...")
        return 2

    banner(cfg)

    if args.once:
        try:
            log.info("Single poll -> %s", poll_once(cfg))
            return 0
        except Exception as e:
            log.error("Could not reach Kira Cloud: %s", e)
            log.error("(Server may be restarting — try again in a minute.)")
            return 1

    idle_streak = 0
    while True:
        try:
            status = poll_once(cfg)
            if status != "idle":
                idle_streak = 0
                continue  # drain the queue without waiting
            idle_streak += 1
            if idle_streak % 20 == 1:  # heartbeat line ~every 10 min
                log.info("Watching for approved batches... (all quiet)")
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return 0
        except Exception as e:
            log.error("Connection problem: %s — retrying shortly", e)
        time.sleep(cfg.get("poll_seconds", 30))


if __name__ == "__main__":
    sys.exit(main())
