"""Kira Telegram bridge — a real intake channel, runnable today.

Clients (or bookkeepers) send an Excel / PDF / receipt photo to the firm's
Telegram bot; it lands in Kira Cloud as a batch in review, and the sender
gets a confirmation with the line count and total.

Telegram's Bot API is free and uses long polling, so this needs NO public
URL and NO Meta business account (unlike WhatsApp). Setup:

  1. In Telegram, talk to @BotFather -> /newbot -> copy the token.
  2. setx TELEGRAM_BOT_TOKEN <token>      (or pass --token)
  3. Map each sender's chat_id to a client in client_data/telegram_map.yaml
     (send the bot any message; this bridge logs unknown chat_ids so you can
     copy them into the map).
  4. python telegram_bot.py               (runs alongside the server)

Flow: getUpdates (long poll) -> download document/photo -> POST to
Kira Cloud /api/intake/telegram?chat_id=... -> reply with the batch summary.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
import yaml


def tg(token: str, method: str, **params):
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}",
                   json=params, timeout=70)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data}")
    return data["result"]


def download_file(token: str, file_id: str) -> tuple[str, bytes]:
    info = tg(token, "getFile", file_id=file_id)
    path = info["file_path"]
    r = httpx.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=120)
    r.raise_for_status()
    return Path(path).name, r.content


def pick_attachment(msg: dict) -> tuple[str, str] | None:
    """Return (file_id, filename) for a document or the largest photo."""
    if "document" in msg:
        d = msg["document"]
        return d["file_id"], d.get("file_name", "document")
    if "photo" in msg and msg["photo"]:
        best = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        return best["file_id"], "photo.jpg"
    return None


def handle_update(update: dict, token: str, server: str) -> None:
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return
    chat_id = str(msg["chat"]["id"])

    att = pick_attachment(msg)
    if att is None:
        tg(token, "sendMessage", chat_id=chat_id, text=(
            "Send me an Excel book, a PDF invoice, or a receipt photo and "
            "I will queue it for your bookkeeper's review."))
        return

    file_id, fallback_name = att
    filename, data = download_file(token, file_id)
    filename = filename or fallback_name

    r = httpx.post(f"{server}/api/intake/telegram", params={"chat_id": chat_id},
                   files={"file": (filename, data)}, timeout=300)
    if r.status_code == 404:
        print(f"[telegram] UNMAPPED chat_id {chat_id} "
              f"({msg['chat'].get('title') or msg['chat'].get('first_name', '?')}) "
              f"— add it to client_data/telegram_map.yaml")
        tg(token, "sendMessage", chat_id=chat_id, text=(
            "This chat isn't linked to a client yet — ask your bookkeeper to "
            f"register chat id {chat_id}."))
        return
    if r.status_code == 422:
        detail = (r.json().get("detail") or {})
        notes = detail.get("notes", []) if isinstance(detail, dict) else []
        if any("DUPLICATE FILE" in n for n in notes):
            reply = (f"{filename} looks identical to a file you already sent — "
                     "I didn't queue it again. If this is a new document, "
                     "re-export or re-photograph it and resend.")
        else:
            reply = (f"I couldn't read anything usable from {filename}. "
                     "I accept Excel (xlsx/xls/csv), PDF, and photos.")
        tg(token, "sendMessage", chat_id=chat_id, text=reply)
        return
    r.raise_for_status()
    s = r.json()
    tg(token, "sendMessage", chat_id=chat_id, text=(
        f"Received {filename} for {s['client']}: {s['lines']} line(s), "
        f"RM {s['total_rm']:,.2f}. Queued for your bookkeeper's review"
        + (f" — {s['errors']} item(s) flagged." if s["errors"] else ".")))
    print(f"[telegram] {filename} -> {s['client']} batch {s['batch_id']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN"))
    ap.add_argument("--server", default="http://localhost:8600")
    args = ap.parse_args()
    if not args.token:
        print("Set TELEGRAM_BOT_TOKEN (from @BotFather) or pass --token.")
        return 2

    me = tg(args.token, "getMe")
    print(f"[telegram] bridging @{me['username']} -> {args.server}")
    offset = 0
    while True:
        try:
            updates = tg(args.token, "getUpdates", offset=offset, timeout=50)
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    handle_update(u, args.token, args.server)
                except Exception as e:
                    print(f"[telegram] update error: {e}")
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"[telegram] poll error: {e} — retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
