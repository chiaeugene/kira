"""One clock for every Kira timestamp: Malaysia time (UTC+8), always.

Kira Cloud runs on a server whose local clock is UTC, so every
time.strftime() there produced timestamps 8 hours behind what the firm's
staff see on their own wall clock ("received 05:23" for a 13:23 upload —
2026-07-25 field feedback). Kira serves Malaysian firms; all stored and
displayed timestamps use Malaysia time regardless of where the server or
the Agent happens to run.

A fixed offset is deliberate: Malaysia has no daylight saving, and a fixed
timezone needs no tz database — which the PyInstaller-frozen Agent exe
would otherwise have to bundle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

MYT = timezone(timedelta(hours=8), name="MYT")


def now() -> datetime:
    return datetime.now(MYT)


def now_str() -> str:
    """Timestamp for storage/display: 2026-07-25T19:04:31 (Malaysia time)."""
    return now().strftime("%Y-%m-%dT%H:%M:%S")


def today_str() -> str:
    """Date stamp for ids/filenames: 20260725 (Malaysia time)."""
    return now().strftime("%Y%m%d")


def stamp() -> str:
    """Filename-safe timestamp: 20260725_190431 (Malaysia time)."""
    return now().strftime("%Y%m%d_%H%M%S")
