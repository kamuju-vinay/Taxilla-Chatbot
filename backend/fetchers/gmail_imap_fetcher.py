"""
Alternative Gmail fetcher using IMAP + an App Password instead of OAuth.

Use this INSTEAD of gmail_fetcher.py (the OAuth-based one) if you'd
rather not set up a Google Cloud OAuth consent screen — an App Password
is quicker to get going with, at the cost of being a bit more manual
(2-Step Verification must be on, and you generate the password by hand).

To use this instead of the OAuth flow, set in .env:
    GMAIL_AUTH_METHOD=imap
    GMAIL_IMAP_ADDRESS=you@gmail.com
    GMAIL_IMAP_APP_PASSWORD=xxxxxxxxxxxxxxxx   (16 chars, no spaces)

Saves real attachments via storage.save_attachment() exactly like the
OAuth fetcher does — no demo/fake data is ever generated here.
"""
import email
import imaplib
import sys
from email.header import decode_header
from pathlib import Path

# Add backend directory to sys.path if needed
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import config
import storage
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
import config
import os

IMAP_SERVER = os.environ.get("GMAIL_IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("GMAIL_IMAP_PORT", "993"))


def _decode(value):
    if value is None:
        return ""
    parts = decode_header(value)
    out = ""
    for text, enc in parts:
        out += text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


def _connect(address=None, app_password=None):
    addr = (address or config.GMAIL_IMAP_ADDRESS or "").strip()
    pwd = (app_password or config.GMAIL_IMAP_APP_PASSWORD or "").replace(" ", "").strip()
    if not addr or not pwd:
        raise RuntimeError(
            "Gmail Address and 16-character App Password must be provided."
        )
    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    imap.login(addr, pwd)
    return imap


def _build_search():
    return ["ALL"]


def fetch_new_reports(max_results=50):
    """Polls Gmail over IMAP and saves any new matching real attachments."""
    imap = _connect()
    try:
        imap.select("INBOX")
        status, data = imap.search(None, *_build_search())
        if status != "OK" or not data[0]:
            return {"provider": "gmail-imap", "messagesScanned": 0, "newReports": 0, "reports": []}

        ids = data[0].split()[::-1][:max_results]  # newest first
        saved = []

        for msg_id in ids:
            status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            sender = _decode(msg.get("From"))
            subject = _decode(msg.get("Subject"))
            received = msg.get("Date")

            if config.SENDER_FILTER and config.SENDER_FILTER.lower() not in sender.lower():
                continue
            if config.SUBJECT_FILTER and config.SUBJECT_FILTER.lower() not in subject.lower():
                continue

            for part in msg.walk():
                filename = part.get_filename()
                if not filename:
                    continue
                filename = _decode(filename)
                content = part.get_payload(decode=True)
                if content is None:
                    continue
                entry = storage.save_attachment(
                    filename=filename,
                    content_bytes=content,
                    source="gmail",
                    provider_message_id=msg_id.decode(),
                    sender=sender,
                    subject=subject,
                    received_at=received,
                )
                if entry:
                    saved.append(entry)

        return {"provider": "gmail-imap", "messagesScanned": len(ids), "newReports": len(saved), "reports": saved}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    # Quick manual test: python gmail_imap_fetcher.py
    result = fetch_new_reports()
    print(f"Scanned {result['messagesScanned']} messages, saved {result['newReports']} new report(s).")
