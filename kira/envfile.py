"""Tiny .env loader — no dependency, no magic.

If a .env file sits next to the app, its KEY=VALUE lines are loaded into the
environment (without overwriting variables already set). This is how the
console and Agent find the deployed Kira Cloud without anyone typing env vars:
drop .env in the folder -> cloud mode; remove it -> local mode.

.env is gitignored; it holds the deployment URL and tokens.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
