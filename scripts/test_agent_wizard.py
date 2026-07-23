"""Regression test for the Agent setup wizard — the "asked me to set up
AGAIN every launch" field bug.

What happened in the field: staff mapped a company, then CLOSED THE WINDOW
at the "Enter to finish" prompt. The old wizard only wrote agent_config.yaml
at the very end, so nothing was saved and every launch restarted setup
(and re-registered duplicate clients in the cloud).

This test simulates that exact sequence with a fake keyboard and asserts:
  1. the config file exists ON DISK the moment the first company is mapped,
     even when the window is killed at the next prompt;
  2. re-running the wizard MERGES (adds company 2, keeps company 1 and the
     PC name) instead of starting over.

Run:  python scripts/test_agent_wizard.py
"""
from __future__ import annotations

import builtins
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

import agent


class FakeKeyboard:
    """Feeds scripted answers to input(); raises KeyboardInterrupt when the
    script runs out — i.e. 'the staff closed the window here'."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        if not self.answers:
            raise KeyboardInterrupt
        return self.answers.pop(0)


def run_wizard(tmp: Path, answers: list[str]) -> bool:
    real_input = builtins.input
    builtins.input = FakeKeyboard(answers)
    try:
        return agent.setup_wizard(str(tmp / "agent_config.yaml"))
    finally:
        builtins.input = real_input


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kira_wizard_"))
    cfg_path = tmp / "agent_config.yaml"

    # No SQL, no cloud, no real keyboard.
    dcf = tmp / "Company.DCF"
    fdb1, fdb2 = tmp / "ACC-0001.FDB", tmp / "ACC-0002.FDB"
    agent.scan_sql_companies = lambda roots=None: ([dcf], [fdb1, fdb2], 0)
    agent.try_extract_company_label = lambda *a, **k: None
    agent.fetch_cloud_clients = lambda *a, **k: []
    registered: list[str] = []
    agent.register_client_on_cloud = (
        lambda server, token, name, **k: registered.append(name)
        or {"created": True, "client": name})
    os.environ["KIRA_SERVER_URL"] = "https://example.test"
    os.environ["KIRA_AGENT_TOKEN"] = "tok"

    # --- Run 1: name PC, map company 1... then the window is closed at the
    # "Enter to finish" prompt (FakeKeyboard runs dry -> KeyboardInterrupt).
    ok = run_wizard(tmp, ["gongji", "1", "", "pw1", ""])
    assert ok, "wizard should report success even when interrupted after a save"
    assert cfg_path.exists(), "config MUST be on disk after the first mapping"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["agent_name"] == "gongji"
    assert len(cfg["clients"]) == 1, cfg["clients"]
    (name1,) = cfg["clients"]
    assert cfg["clients"][name1]["fdb_name"] == "ACC-0001.FDB"
    assert cfg["clients"][name1]["user"] == "ADMIN"
    print(f"1. window closed mid-wizard -> config saved anyway ({name1})  OK")

    # --- Run 2: wizard again (as --setup would). PC name accepted by Enter,
    # map company 2, finish properly with Enter.
    ok = run_wizard(tmp, ["", "2", "", "pw2", "", ""])
    assert ok
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["agent_name"] == "gongji", "PC name must be remembered"
    assert len(cfg["clients"]) == 2, "second run must ADD, not replace"
    assert name1 in cfg["clients"], "company 1 must survive the second run"
    fdbs = sorted(c["fdb_name"] for c in cfg["clients"].values())
    assert fdbs == ["ACC-0001.FDB", "ACC-0002.FDB"], fdbs
    print("2. re-run merges: both companies kept, PC name remembered      OK")

    # --- Registration happened once per company, no duplicates.
    assert len(registered) == 2, registered
    print("3. exactly one cloud registration per company                  OK")

    print("\nAll wizard regression checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
