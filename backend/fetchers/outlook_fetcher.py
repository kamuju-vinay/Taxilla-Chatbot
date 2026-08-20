"""
Outlook / Microsoft 365 attachment fetcher.

Reuses the same MSAL device-code flow as mailbox_fetcher.ipynb, but
additionally downloads each message's actual attachments (xlsx/csv/pdf)
via Microsoft Graph and saves them with storage.save_attachment(),
instead of just printing message metadata.
"""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.append(str(Path(__file__).resolve().parent.parent))
import config
import storage

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _get_token():
    import msal

    if not config.OUTLOOK_CLIENT_ID or config.OUTLOOK_CLIENT_ID.startswith("your-"):
        raise RuntimeError(
            "OUTLOOK_CLIENT_ID isn't set in .env — Outlook sync is skipped until it is. "
            "See backend/README.md for the Azure app registration steps, or set "
            "OUTLOOK_ENABLED=false in .env if you're not using Outlook."
        )

    authority = f"https://login.microsoftonline.com/{config.OUTLOOK_TENANT_ID}"
    cache = msal.SerializableTokenCache()
    cache_path = Path(config.OUTLOOK_TOKEN_CACHE_PATH)
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())

    app = msal.PublicClientApplication(config.OUTLOOK_CLIENT_ID, authority=authority, token_cache=cache)

    accounts = app.get_accounts()
    result = app.acquire_token_silent(config.OUTLOOK_SCOPES, account=accounts[0]) if accounts else None

    if not result:
        flow = app.initiate_device_flow(scopes=config.OUTLOOK_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError("Failed to start Outlook device-code flow.")
        print(flow["message"])  # one-time interactive step, shown in the service logs
        result = app.acquire_token_by_device_flow(flow)

    if cache.has_state_changed:
        cache_path.write_text(cache.serialize())

    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "Outlook authentication failed"))
    return result["access_token"]


def _build_filter():
    clauses = ["hasAttachments eq true"]
    if config.SUBJECT_FILTER:
        clauses.append(f"contains(subject,'{config.SUBJECT_FILTER}')")
    return " and ".join(clauses)


def fetch_new_reports(max_results=25):
    """Polls Outlook and saves any new matching attachments. Returns a summary dict."""
    token = _get_token()
    headers = {"Authorization": f"Bearer {token}"}
    saved = []

    resp = requests.get(
        f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
        headers=headers,
        params={
            "$top": max_results,
            "$filter": _build_filter(),
            "$select": "id,from,subject,receivedDateTime,hasAttachments",
            "$orderby": "receivedDateTime desc",
        },
        timeout=20,
    )
    resp.raise_for_status()
    messages = resp.json().get("value", [])

    for m in messages:
        sender = (m.get("from") or {}).get("emailAddress", {}).get("address")
        if config.SENDER_FILTER and sender != config.SENDER_FILTER:
            continue
        subject = m.get("subject")
        received = m.get("receivedDateTime")

        att_resp = requests.get(f"{GRAPH_BASE}/me/messages/{m['id']}/attachments", headers=headers, timeout=20)
        att_resp.raise_for_status()
        for att in att_resp.json().get("value", []):
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            filename = att.get("name")
            content_bytes = base64.b64decode(att["contentBytes"])
            entry = storage.save_attachment(
                filename=filename,
                content_bytes=content_bytes,
                source="outlook",
                provider_message_id=m["id"],
                sender=sender,
                subject=subject,
                received_at=received,
            )
            if entry:
                saved.append(entry)

    return {"provider": "outlook", "messagesScanned": len(messages), "newReports": len(saved), "reports": saved}


# ---------------------------------------------------------------------
# Real-time push via a Microsoft Graph subscription. Unlike Gmail this
# needs no separate cloud-console setup — the app registration you
# already have (for the OAuth device-code flow above) is enough, as
# long as PUBLIC_BASE_URL points at a publicly reachable HTTPS URL for
# {PUBLIC_BASE_URL}/api/webhooks/outlook. Subscriptions expire (max
# ~4230 minutes, about 3 days, for the messages resource) and must be
# renewed — scheduler.py handles that automatically while the backend
# is running.
# ---------------------------------------------------------------------

MAX_SUBSCRIPTION_MINUTES = 4230


def create_subscription():
    if not config.PUBLIC_BASE_URL:
        raise RuntimeError(
            "PUBLIC_BASE_URL isn't set in .env — Outlook push needs a publicly reachable "
            "HTTPS URL (e.g. an ngrok/Cloudflare Tunnel URL) to send notifications to."
        )
    token = _get_token()
    expiration = (datetime.now(timezone.utc) + timedelta(minutes=MAX_SUBSCRIPTION_MINUTES)).isoformat().replace("+00:00", "Z")
    body = {
        "changeType": "created",
        "notificationUrl": f"{config.PUBLIC_BASE_URL}/api/webhooks/outlook?secret={config.WEBHOOK_SHARED_SECRET}",
        "resource": "me/mailFolders('inbox')/messages",
        "expirationDateTime": expiration,
        "clientState": config.WEBHOOK_SHARED_SECRET,
    }
    resp = requests.post(
        f"{GRAPH_BASE}/subscriptions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=20,
    )
    resp.raise_for_status()
    subscription = resp.json()
    config.OUTLOOK_SUBSCRIPTION_STATE_PATH.write_text(json.dumps(subscription))
    return subscription


def renew_subscription():
    state = subscription_status()
    if not state or "id" not in state:
        return create_subscription()
    token = _get_token()
    expiration = (datetime.now(timezone.utc) + timedelta(minutes=MAX_SUBSCRIPTION_MINUTES)).isoformat().replace("+00:00", "Z")
    resp = requests.patch(
        f"{GRAPH_BASE}/subscriptions/{state['id']}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"expirationDateTime": expiration},
        timeout=20,
    )
    if resp.status_code == 404:
        # subscription already expired / was deleted server-side — recreate it
        return create_subscription()
    resp.raise_for_status()
    subscription = resp.json()
    config.OUTLOOK_SUBSCRIPTION_STATE_PATH.write_text(json.dumps(subscription))
    return subscription


def delete_subscription():
    state = subscription_status()
    if not state or "id" not in state:
        return
    try:
        token = _get_token()
        requests.delete(
            f"{GRAPH_BASE}/subscriptions/{state['id']}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    finally:
        if config.OUTLOOK_SUBSCRIPTION_STATE_PATH.exists():
            config.OUTLOOK_SUBSCRIPTION_STATE_PATH.unlink()


def subscription_status():
    if not config.OUTLOOK_SUBSCRIPTION_STATE_PATH.exists():
        return None
    try:
        return json.loads(config.OUTLOOK_SUBSCRIPTION_STATE_PATH.read_text())
    except Exception:
        return None
