"""
Gmail attachment fetcher.

Reuses the same OAuth flow as mailbox_fetcher.ipynb (Desktop-app
credentials.json + saved token.json), but additionally downloads the
actual attachment bytes for xlsx/csv/pdf files and saves them via
storage.save_attachment(), instead of just printing message metadata.
"""
import base64
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
import config
import storage


def _get_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(config.GMAIL_TOKEN_PATH)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), config.GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(config.GMAIL_CREDENTIALS_PATH).exists():
                raise FileNotFoundError(
                    f"Gmail credentials.json not found at {config.GMAIL_CREDENTIALS_PATH}. "
                    "Download it from Google Cloud Console (OAuth Client ID, Desktop app type)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(config.GMAIL_CREDENTIALS_PATH, config.GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _build_query():
    parts = ["has:attachment"]
    if config.SENDER_FILTER:
        parts.append(f"from:{config.SENDER_FILTER}")
    if config.SUBJECT_FILTER:
        parts.append(f'subject:"{config.SUBJECT_FILTER}"')
    return " ".join(parts)


def fetch_new_reports(max_results=25):
    """Polls Gmail and saves any new matching attachments. Returns a summary dict."""
    service = _get_service()
    saved = []

    results = service.users().messages().list(
        userId="me", maxResults=max_results, q=_build_query()
    ).execute()
    message_ids = results.get("messages", [])

    for m in message_ids:
        msg = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        sender = headers.get("From")
        subject = headers.get("Subject")
        received = headers.get("Date")

        for part in _walk_parts(msg["payload"]):
            filename = part.get("filename")
            body = part.get("body", {})
            if not filename or not body.get("attachmentId"):
                continue
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"]
            ).execute()
            content = base64.urlsafe_b64decode(att["data"])
            entry = storage.save_attachment(
                filename=filename,
                content_bytes=content,
                source="gmail",
                provider_message_id=msg["id"],
                sender=sender,
                subject=subject,
                received_at=received,
            )
            if entry:
                saved.append(entry)

    return {"provider": "gmail", "messagesScanned": len(message_ids), "newReports": len(saved), "reports": saved}


def _walk_parts(payload):
    """Yield every MIME part (Gmail nests attachments under multipart parts)."""
    if payload.get("filename"):
        yield payload
    for p in payload.get("parts", []) or []:
        yield from _walk_parts(p)


# ---------------------------------------------------------------------
# Real-time push via Google Cloud Pub/Sub. Gmail has no per-app webhook
# config like Outlook does — instead you create a Pub/Sub topic, grant
# Gmail's push service account Publish rights on it, create a push
# subscription pointing at our /api/webhooks/gmail endpoint, then call
# users.watch() (below) to tell Gmail to start publishing to that topic.
# The watch expires after ~7 days and must be renewed — scheduler.py
# handles that automatically while the backend is running.
# ---------------------------------------------------------------------

def start_watch():
    """Registers (or renews) a Gmail push watch. Returns the watch response
    dict ({"historyId": ..., "expiration": <epoch ms>}) on success."""
    if not config.GMAIL_PUBSUB_TOPIC:
        raise RuntimeError(
            "GMAIL_PUBSUB_TOPIC isn't set in .env — see backend/README.md for the "
            "Google Cloud Pub/Sub setup steps needed before Gmail push can be enabled."
        )
    service = _get_service()
    response = service.users().watch(
        userId="me",
        body={"topicName": config.GMAIL_PUBSUB_TOPIC, "labelIds": ["INBOX"]},
    ).execute()
    config.GMAIL_WATCH_STATE_PATH.write_text(json.dumps(response))
    return response


def stop_watch():
    service = _get_service()
    service.users().stop(userId="me").execute()
    if config.GMAIL_WATCH_STATE_PATH.exists():
        config.GMAIL_WATCH_STATE_PATH.unlink()


def watch_status():
    """Returns the last-known watch state (or None if never started)."""
    if not config.GMAIL_WATCH_STATE_PATH.exists():
        return None
    try:
        return json.loads(config.GMAIL_WATCH_STATE_PATH.read_text())
    except Exception:
        return None
