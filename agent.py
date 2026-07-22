"""Kira Agent — runs on the PC where SQL Accounting is installed.

Outbound-only: polls Kira Cloud for approved batches, posts them into SQL
via the free official SDK, reports the result back. No inbound ports, so it
works behind any office router. Install once, forget it exists.

Run:   python agent.py            (continuous, poll every 30s)
       python agent.py --once     (single poll — used by tests / task scheduler)

Config: agent_config.yaml
  server_url: http://localhost:8600
  agent_token: <token matching server config.yaml>
  agent_name: office-pc-1
  poll_seconds: 30
  clients:                # SQL login per client company this PC can post to
    DEMO_CLIENT:
      dry_run: true
      user: ADMIN
      password: ADMIN
      dcf_path: 'C:\\eStream\\SQLAccounting\\Share\\Default.DCF'
      fdb_name: 'ACC-0001.FDB'
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import yaml

from kira.batches import records_to_df
from kira.poster import SQLConfig, post_batch


import os

from kira.envfile import load_env

load_env()  # .env can carry KIRA_SERVER_URL / KIRA_AGENT_TOKEN


def load_cfg(path: str = "agent_config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    # env overrides so the installed Agent can be pointed at prod without edits
    cfg["server_url"] = os.environ.get("KIRA_SERVER_URL", cfg["server_url"])
    cfg["agent_token"] = os.environ.get("KIRA_AGENT_TOKEN", cfg["agent_token"])
    return cfg


def poll_once(cfg: dict, client: httpx.Client | None = None) -> str:
    """One poll cycle. Returns 'idle', 'posted', or 'failed'."""
    http = client or httpx.Client(base_url=cfg["server_url"], timeout=60)
    headers = {"Authorization": f"Bearer {cfg['agent_token']}"}

    r = http.post("/api/agent/poll", headers=headers, json={
        "agent_name": cfg.get("agent_name", "agent"),
        "clients": list(cfg["clients"].keys()),
        # heartbeat detail for the Connections tab
        "modes": {c: ("dry_run" if v.get("dry_run", True) else "live")
                  for c, v in cfg["clients"].items()},
    })
    r.raise_for_status()
    job = r.json()
    if not job.get("batch_id"):
        return "idle"

    bid, client_name = job["batch_id"], job["client"]
    print(f"[agent] got batch {bid} for {client_name} "
          f"(RM {job['total_rm']:,.2f}, {len(job['rows'])} lines)")

    sql_cfg = SQLConfig(**cfg["clients"][client_name])
    try:
        df = records_to_df(job["rows"])
        result = post_batch(df, sql_cfg, out_dir="posted")
        ok = len(result.get("errors", [])) == 0
    except Exception as e:  # report failures, never swallow them
        result = {"mode": "exception", "invoices": 0, "errors": [str(e)]}
        ok = False

    rep = http.post("/api/agent/report", headers=headers, json={
        "batch_id": bid,
        "ok": ok,
        "mode": result["mode"],
        "invoices": result.get("invoices", 0),
        "errors": [str(e) for e in result.get("errors", [])],
    })
    rep.raise_for_status()
    print(f"[agent] batch {bid} -> {'posted' if ok else 'FAILED'} "
          f"({result['mode']})")
    return "posted" if ok else "failed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--config", default="agent_config.yaml")
    args = ap.parse_args()
    cfg = load_cfg(args.config)

    if args.once:
        print(f"[agent] single poll -> {poll_once(cfg)}")
        return 0

    print(f"[agent] {cfg.get('agent_name', 'agent')} polling "
          f"{cfg['server_url']} every {cfg.get('poll_seconds', 30)}s")
    while True:
        try:
            status = poll_once(cfg)
            if status != "idle":
                continue  # drain the queue without waiting
        except Exception as e:
            print(f"[agent] error: {e} — retrying")
        time.sleep(cfg.get("poll_seconds", 30))


if __name__ == "__main__":
    sys.exit(main())
