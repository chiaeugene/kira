"""Per-client record of every file ever received, keyed by content hash.

Guards against the classic double-entry source: the same invoice or Excel
forwarded twice (by WhatsApp, Telegram, email, or a second upload). Identical
bytes are flagged before they become a batch — on top of the line-level
DUP_POSTED check, which catches re-keyed duplicates with different files.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class FileLog:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "file_log.json"
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def seen(self, data: bytes) -> dict | None:
        """Return the prior receipt record if these exact bytes were seen before."""
        return self.entries.get(self.digest(data))

    def record(self, filename: str, data: bytes, channel: str = "upload") -> None:
        self.entries[self.digest(data)] = {
            "file": filename,
            "channel": channel,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False),
                             encoding="utf-8")
