"""
API consumed by the TAXILLA AI Assistant frontend.

  GET  /api/reports              -> list of reports (fetched + uploaded)
  GET  /api/reports/<id>/download -> the original file
  POST /api/reports/upload        -> manual upload (multipart/form-data, field "file")
  POST /api/sync                  -> trigger an immediate mailbox poll
  POST /api/ask                   -> answers questions via Cohere or Gemini (your choice), keeps keys server-side
  GET  /api/health                -> liveness check
  POST /api/push/<provider>/enable  -> turn on real-time push for gmail/outlook (needs PUBLIC_BASE_URL)
  POST /api/push/<provider>/disable -> turn it back off
  POST /api/webhooks/gmail        -> Google Cloud Pub/Sub push target
  POST /api/webhooks/outlook      -> Microsoft Graph subscription notification target

Everything else (any path that isn't /api/...) serves the built React
frontend from ../frontend/dist, so the whole app runs on ONE origin/port
(http://localhost:5000 by default) instead of two separate dev servers.
Run `npm run build` in frontend/ first — see frontend/dist_missing check
below for the exact command if that folder doesn't exist yet.
"""
import logging
import os
import threading
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory, abort, Response
from flask_cors import CORS

import config
import storage
from scheduler import start_background_scheduler, run_sync_once, reschedule_polling

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("app")

FRONTEND_DIST = (Path(__file__).resolve().parent.parent / "frontend" / "dist")

app = Flask(__name__)
# CORS_ORIGIN is a list (see config.py) — pass "*" as a bare string when
# that's the configured value so flask-cors applies its wildcard behavior
# correctly, rather than treating ["*"] as one specific literal origin.
_cors_origins = "*" if config.CORS_ORIGIN == ["*"] else config.CORS_ORIGIN
CORS(app, origins=_cors_origins)

# Holds the live APScheduler instance once started (see __main__ below) so
# /api/config can reschedule the polling job at runtime without a restart.
_scheduler_holder = {"instance": None}


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/reports")
def get_reports():
    return jsonify(storage.list_reports())


@app.get("/api/reports/<report_id>/download")
def download_report(report_id):
    path, entry = storage.report_path(report_id)
    if not path or not path.exists():
        abort(404, description="Report not found")
    return send_file(path, as_attachment=True, download_name=entry["filename"])


@app.get("/api/reports/<report_id>/text")
def report_text(report_id):
    """Extracted PDF text, used both for the preview modal and for
    grounding chat answers in PDF content the same way spreadsheet rows
    already are."""
    path = storage.extracted_text_path(report_id)
    if not path:
        return jsonify({"text": ""})
    return jsonify({"text": path.read_text(encoding="utf-8")})

@app.post("/api/reports/delete")
def delete_reports():
    """Body: {"ids": ["id1", "id2", ...]}. Deletes just those reports."""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not ids:
        return jsonify({"error": "No report ids provided"}), 400
    deleted = storage.delete_reports(ids)
    return jsonify({"deleted": deleted, "count": len(deleted)})


@app.post("/api/reports/delete-all")
def delete_all_reports():
    storage.delete_all_reports()
    return jsonify({"status": "ok"})


@app.post("/api/reports/upload")
def upload_report():
    if "file" not in request.files:
        abort(400, description="No file provided (expected form field 'file')")
    f = request.files["file"]
    if not f.filename:
        abort(400, description="Empty filename")
    entry = storage.save_attachment(
        filename=f.filename,
        content_bytes=f.read(),
        source="upload",
        provider_message_id=None,
    )
    if not entry:
        abort(415, description=f"Unsupported file type. Allowed: {sorted(config.ALLOWED_EXTENSIONS)}")
    return jsonify(entry), 201


@app.post("/api/test-imap")
def test_imap():
    from fetchers import gmail_imap_fetcher
    body = request.get_json(silent=True) or {}
    addr = (body.get("gmailImapAddress") or body.get("gmailAddress") or config.GMAIL_IMAP_ADDRESS or os.environ.get("GMAIL_IMAP_ADDRESS") or "").strip()
    pwd = (body.get("gmailImapAppPassword") or body.get("gmailAppPassword") or config.GMAIL_IMAP_APP_PASSWORD or os.environ.get("GMAIL_IMAP_APP_PASSWORD") or "").replace(" ", "").strip()
    try:
        imap = gmail_imap_fetcher._connect(address=addr, app_password=pwd)
        imap.logout()
        return jsonify({"ok": True, "message": "Successfully authenticated with Gmail IMAP!"})
    except Exception as e:
        log.exception("Gmail IMAP test failed")
        err_msg = str(e)
        if "11001" in err_msg or "getaddrinfo failed" in err_msg:
            err_msg = "DNS Lookup Failed for imap.gmail.com. Please check your internet connection or firewall/VPN settings."
        return jsonify({"ok": False, "error": err_msg}), 400


@app.route("/api/send-email", methods=["POST"])
def send_email():
    body = request.get_json(silent=True) or {}
    to_recipients = body.get("toRecipients", [])
    subject = body.get("subject", "TAXILLA Enhanced Report")
    html_content = body.get("htmlContent", "")
    sender_addr = (
        body.get("gmailAddress") or 
        body.get("gmailImapAddress") or 
        config.GMAIL_IMAP_ADDRESS or 
        os.environ.get("GMAIL_IMAP_ADDRESS") or ""
    ).strip()
    app_pwd = (
        body.get("gmailAppPassword") or 
        body.get("gmailImapAppPassword") or 
        config.GMAIL_IMAP_APP_PASSWORD or 
        os.environ.get("GMAIL_IMAP_APP_PASSWORD") or ""
    ).replace(" ", "").strip()

    if not sender_addr or not app_pwd:
        return jsonify({
            "ok": False,
            "error": "Gmail credentials not configured. Please configure your Gmail Address and App Password in Settings (⚙️)."
        }), 400

    valid_recipients = [r.strip() for r in to_recipients if r and "@" in r and not r.endswith("@taxilla.com")]
    if not valid_recipients:
        valid_recipients = [sender_addr]

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_addr
    msg["To"] = ", ".join(valid_recipients)
    msg.attach(MIMEText(html_content, "html"))

    smtp_server = os.environ.get("GMAIL_SMTP_SERVER", "smtp.gmail.com")
    # Try STARTTLS on 587 first, then fall back to implicit SSL on 465.
    # Some hosting providers (Render included, depending on plan/region)
    # block or silently drop one of the two outbound SMTP ports — trying
    # both here means a send only fails if BOTH are actually blocked,
    # instead of failing outright the moment the first one is.
    attempts = [
        (int(os.environ.get("GMAIL_SMTP_PORT", 587)), "starttls"),
        (465, "ssl"),
    ]
    last_error = None
    for port, mode in attempts:
        try:
            if mode == "starttls":
                with smtplib.SMTP(smtp_server, port, timeout=15) as server:
                    server.starttls()
                    server.login(sender_addr, app_pwd)
                    server.sendmail(sender_addr, valid_recipients, msg.as_string())
            else:
                import ssl
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(smtp_server, port, timeout=15, context=context) as server:
                    server.login(sender_addr, app_pwd)
                    server.sendmail(sender_addr, valid_recipients, msg.as_string())
            return jsonify({"ok": True, "message": f"Successfully sent enhanced report email to {len(valid_recipients)} recipient(s) ({', '.join(valid_recipients)})!"})
        except smtplib.SMTPRecipientsRefused as e:
            log.exception("SMTP recipient refused")
            return jsonify({"ok": False, "error": f"Recipient Rejected: The email recipient address was refused by Gmail ({e}). Please enter a valid recipient email address."}), 400
        except smtplib.SMTPAuthenticationError as e:
            log.exception("SMTP authentication failed")
            return jsonify({"ok": False, "error": "Gmail rejected the App Password. Please regenerate it in your Google Account (Security > App Passwords) and update Settings."}), 400
        except Exception as e:
            log.warning("SMTP send via port %s (%s) failed, trying next option: %s", port, mode, e)
            last_error = e
            continue

    err_str = str(last_error)
    if "11001" in err_str or "getaddrinfo failed" in err_str:
        err_str = "DNS resolution error while connecting to smtp.gmail.com. Please check your internet connection or firewall/VPN settings."
    elif "timed out" in err_str.lower() or "timeout" in err_str.lower():
        err_str = "Both SMTP ports (587 and 465) timed out — outbound SMTP may be blocked on this host's network. Consider connecting Gmail via OAuth instead, which sends over HTTPS and isn't affected by SMTP port restrictions."
    return jsonify({"ok": False, "error": err_str}), 500


@app.post("/api/auth/gmail/reset")
def reset_gmail_token():
    token_path = Path(config.GMAIL_TOKEN_PATH)
    if token_path.exists():
        try:
            token_path.unlink()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "message": "Gmail OAuth token removed. You can now authenticate with a new account."})


@app.post("/api/sync")
def sync_now():
    results = run_sync_once()
    return jsonify({"results": results})


@app.post("/api/push/<provider>/enable")
def push_enable(provider):
    if provider == "gmail":
        if config.GMAIL_AUTH_METHOD != "oauth":
            abort(400, description="Gmail push requires OAuth auth method, not App Password (IMAP).")
        from fetchers import gmail_fetcher
        try:
            state = gmail_fetcher.start_watch()
        except Exception as e:
            log.exception("Failed to enable Gmail push")
            abort(400, description=str(e))
        return jsonify({"enabled": True, "state": state})
    elif provider == "outlook":
        from fetchers import outlook_fetcher
        try:
            state = outlook_fetcher.create_subscription()
        except Exception as e:
            log.exception("Failed to enable Outlook push")
            abort(400, description=str(e))
        return jsonify({"enabled": True, "state": state})
    abort(404, description="Unknown provider — use 'gmail' or 'outlook'")


@app.post("/api/push/<provider>/disable")
def push_disable(provider):
    if provider == "gmail":
        from fetchers import gmail_fetcher
        try:
            gmail_fetcher.stop_watch()
        except Exception as e:
            log.exception("Failed to disable Gmail push")
            abort(400, description=str(e))
        return jsonify({"enabled": False})
    elif provider == "outlook":
        from fetchers import outlook_fetcher
        try:
            outlook_fetcher.delete_subscription()
        except Exception as e:
            log.exception("Failed to disable Outlook push")
            abort(400, description=str(e))
        return jsonify({"enabled": False})
    abort(404, description="Unknown provider — use 'gmail' or 'outlook'")


def _sync_in_background(provider_name):
    """Runs a single provider's fetch on a worker thread so the webhook
    handler can return immediately — both Pub/Sub and Graph expect a fast
    ack and will retry (or eventually give up) if the request hangs."""
    def _run():
        try:
            if provider_name == "gmail":
                if config.GMAIL_AUTH_METHOD == "imap":
                    from fetchers import gmail_imap_fetcher as gmail_fetcher
                else:
                    from fetchers import gmail_fetcher
                gmail_fetcher.fetch_new_reports()
            elif provider_name == "outlook":
                from fetchers import outlook_fetcher
                outlook_fetcher.fetch_new_reports()
        except Exception:
            log.exception("Webhook-triggered %s sync failed", provider_name)
    threading.Thread(target=_run, daemon=True).start()


@app.post("/api/webhooks/gmail")
def webhook_gmail():
    """Google Cloud Pub/Sub push target. Pub/Sub retries on any non-2xx
    response, so this always returns 200 once the secret checks out —
    fetch failures are logged, not surfaced to Pub/Sub as a retry signal."""
    if request.args.get("secret") != config.WEBHOOK_SHARED_SECRET:
        abort(403)
    _sync_in_background("gmail")
    return "", 200


@app.route("/api/webhooks/outlook", methods=["GET", "POST"])
def webhook_outlook():
    """Microsoft Graph subscription notification target. Handles both the
    one-time validation handshake (a GET/POST with ?validationToken=...,
    which must be echoed back as plain text within 10 seconds) and actual
    change notifications."""
    validation_token = request.args.get("validationToken")
    if validation_token is not None:
        return Response(validation_token, status=200, mimetype="text/plain")

    if request.args.get("secret") != config.WEBHOOK_SHARED_SECRET:
        abort(403)

    body = request.get_json(silent=True) or {}
    for notification in body.get("value", []):
        if notification.get("clientState") != config.WEBHOOK_SHARED_SECRET:
            continue  # ignore anything that doesn't carry our secret
        _sync_in_background("outlook")
        break
    return "", 202


from rag_engine import ReportRAGEngine


def _new_rag_engine():
    return ReportRAGEngine(
        cohere_api_key=config.COHERE_API_KEY,
        gemini_api_key=config.GEMINI_API_KEY,
        groq_api_key=config.GROQ_API_KEY,
        ai_provider=config.AI_PROVIDER,
        cohere_model=config.COHERE_CHAT_MODEL,
        gemini_model=config.GEMINI_MODEL,
        groq_model=config.GROQ_MODEL,
    )


rag_engine = _new_rag_engine()


def _build_chunks_from_reports():
    """Loads every real, currently-stored report (xlsx/xls/csv/pdf) into
    text chunks. This is the ONLY source of truth for chat answers —
    nothing here is hardcoded or fabricated."""
    chunks = []
    for r in storage.list_reports():
        ext = r.get("ext", "")
        path = config.REPORTS_DIR / r["storedAs"]
        if not path.exists():
            continue
        if ext in ("xlsx", "xls"):
            chunks.extend(rag_engine.load_xlsx_chunks(path, r["filename"]))
        elif ext == "csv":
            chunks.extend(rag_engine.load_csv_chunks(path, r["filename"]))
        elif ext == "pdf":
            text_path = storage.extracted_text_path(r["id"])
            if text_path:
                chunks.extend(rag_engine.load_pdf_text_chunks(text_path.read_text(encoding="utf-8"), r["filename"]))
    return chunks


@app.get("/api/status")
def status():
    if config.GMAIL_AUTH_METHOD == "imap":
        gmail_connected = config.GMAIL_ENABLED and bool(config.GMAIL_IMAP_ADDRESS and config.GMAIL_IMAP_APP_PASSWORD)
    else:
        gmail_connected = config.GMAIL_ENABLED and Path(config.GMAIL_TOKEN_PATH).exists()
    outlook_connected = config.OUTLOOK_ENABLED and Path(config.OUTLOOK_TOKEN_CACHE_PATH).exists()

    gmail_push = None
    if config.GMAIL_AUTH_METHOD == "oauth":
        try:
            from fetchers import gmail_fetcher
            gmail_push = gmail_fetcher.watch_status()
        except Exception:
            gmail_push = None

    outlook_push = None
    try:
        from fetchers import outlook_fetcher
        outlook_push = outlook_fetcher.subscription_status()
    except Exception:
        outlook_push = None

    return jsonify({
        "gmail": {
            "enabled": config.GMAIL_ENABLED,
            "connected": gmail_connected,
            "authMethod": config.GMAIL_AUTH_METHOD,
            "pushEnabled": bool(gmail_push),
            "pushExpiresAtMs": (gmail_push or {}).get("expiration"),
        },
        "outlook": {
            "enabled": config.OUTLOOK_ENABLED,
            "connected": outlook_connected,
            "pushEnabled": bool(outlook_push),
            "pushExpiresAt": (outlook_push or {}).get("expirationDateTime"),
        },
        "pollIntervalSeconds": config.POLL_INTERVAL_SECONDS,
        "publicBaseUrlSet": bool(config.PUBLIC_BASE_URL),
        "aiProvider": config.AI_PROVIDER,
        "cohereConfigured": bool(config.COHERE_API_KEY),
        "geminiConfigured": bool(config.GEMINI_API_KEY),
        "groqConfigured": bool(config.GROQ_API_KEY),
    })


@app.get("/api/config")
def get_config():
    return jsonify({
        "senderFilter": config.SENDER_FILTER,
        "subjectFilter": config.SUBJECT_FILTER,
        "pollIntervalSeconds": config.POLL_INTERVAL_SECONDS,
        "gmailEnabled": config.GMAIL_ENABLED,
        "outlookEnabled": config.OUTLOOK_ENABLED,
        "gmailAuthMethod": config.GMAIL_AUTH_METHOD,
        "gmailImapAddress": config.GMAIL_IMAP_ADDRESS,
        "gmailImapAppPasswordSet": bool(config.GMAIL_IMAP_APP_PASSWORD),
        "publicBaseUrl": config.PUBLIC_BASE_URL,
        "gmailPubsubTopic": config.GMAIL_PUBSUB_TOPIC,
        "aiProvider": config.AI_PROVIDER,
        "cohereApiKey": config.COHERE_API_KEY[:8] + "••••••••" if config.COHERE_API_KEY else "",
        "cohereConfigured": bool(config.COHERE_API_KEY),
        "cohereChatModel": config.COHERE_CHAT_MODEL,
        "geminiApiKey": config.GEMINI_API_KEY[:8] + "••••••••" if config.GEMINI_API_KEY else "",
        "geminiConfigured": bool(config.GEMINI_API_KEY),
        "geminiModel": config.GEMINI_MODEL,
        "groqApiKey": config.GROQ_API_KEY[:8] + "••••••••" if config.GROQ_API_KEY else "",
        "groqConfigured": bool(config.GROQ_API_KEY),
        "groqModel": config.GROQ_MODEL,
    })


@app.post("/api/config")
def set_config():
    global rag_engine
    body = request.get_json(force=True) or {}
    allowed = {
        "senderFilter", "subjectFilter", "pollIntervalSeconds", "pollIntervalMinutes",
        "gmailEnabled", "outlookEnabled",
        "gmailAuthMethod", "gmailImapAddress", "gmailImapAppPassword",
        "publicBaseUrl", "gmailPubsubTopic",
        "aiProvider", "cohereApiKey", "cohereChatModel", "geminiApiKey", "geminiModel",
        "groqApiKey", "groqModel",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if (updates.get("gmailImapAddress") or updates.get("gmailImapAppPassword")) and "gmailAuthMethod" not in updates:
        updates["gmailAuthMethod"] = "imap"
    # Don't overwrite a previously-saved secret with an empty string — the
    # frontend only ever shows a masked/"set" indicator, never the value back.
    for secret_field in ("gmailImapAppPassword", "cohereApiKey", "geminiApiKey", "groqApiKey"):
        if secret_field in updates and not updates[secret_field]:
            del updates[secret_field]
    if not updates:
        abort(400, description=f"No recognized fields. Allowed: {sorted(allowed)}")
    config.save_runtime_overrides(**updates)
    if {"cohereApiKey", "geminiApiKey", "groqApiKey", "aiProvider", "cohereChatModel", "geminiModel", "groqModel"} & updates.keys():
        rag_engine = _new_rag_engine()
    if ({"pollIntervalSeconds", "pollIntervalMinutes"} & updates.keys()) and _scheduler_holder["instance"]:
        reschedule_polling(_scheduler_holder["instance"])
    return jsonify({
        "saved": True,
        "aiProvider": config.AI_PROVIDER,
        "cohereConfigured": bool(config.COHERE_API_KEY),
        "geminiConfigured": bool(config.GEMINI_API_KEY),
        "groqConfigured": bool(config.GROQ_API_KEY),
        "pollIntervalSeconds": config.POLL_INTERVAL_SECONDS,
    })


import json


def _no_reports_response():
    return json.dumps({
        "answer": "No TAXILLA reports are loaded in the system yet. Please configure report data sources in Settings or upload a report file, then try asking again.",
        "chartType": None, "chartTitle": None, "chartData": None,
    })


def _not_configured_response():
    return json.dumps({
        "answer": "The TAXILLA AI Assistant is not fully configured on the server. Please configure an AI Provider API key (Gemini, Groq, or Cohere) in Settings.",
        "chartType": None, "chartTitle": None, "chartData": None,
    })


def _unreachable_response():
    return json.dumps({
        "answer": "I wasn't able to reach the AI provider just now. Please try again in a moment.",
        "chartType": None, "chartTitle": None, "chartData": None,
    })


@app.post("/api/ask")
def ask():
    body = request.get_json(force=True) or {}
    question = (body.get("question") or "").strip()
    provider = body.get("provider")  # optional per-request override ("cohere" | "gemini")
    if not question:
        return jsonify({"text": _no_reports_response()})

    if not config.COHERE_API_KEY and not config.GEMINI_API_KEY and not config.GROQ_API_KEY:
        return jsonify({"text": _not_configured_response()})

    try:
        chunks = _build_chunks_from_reports()
    except Exception:
        log.exception("Failed to build chunks from stored reports")
        chunks = []

    if not chunks:
        return jsonify({"text": _no_reports_response()})

    try:
        history = body.get("history") or []
        rag_text = rag_engine.ask(question, chunks, top_k=6, provider=provider, history=history)
        if rag_text:
            return jsonify({"text": rag_text})
    except Exception as e:
        log.exception("RAG ask() failed: %s", e)

    return jsonify({"text": _unreachable_response()})


@app.post("/api/enhance-report")
def enhance_report():
    """Takes the raw text of a report (already extracted client-side or
    server-side) plus free-text instructions, and returns a genuinely
    AI-generated, dashboard-style HTML report from the configured LLM
    provider. This is the step that was previously missing — file upload
    was producing an HTML preview mechanically, with no LLM call at all."""
    body = request.get_json(force=True) or {}
    report_text = (body.get("reportText") or "").strip()
    instructions = (body.get("instructions") or "").strip()
    provider = body.get("provider")

    if not config.COHERE_API_KEY and not config.GEMINI_API_KEY and not config.GROQ_API_KEY:
        return jsonify({"ok": False, "error": "not_configured",
                         "message": "No AI Provider API key (Gemini, Groq, or Cohere) is configured in Settings."}), 200

    if not report_text:
        return jsonify({"ok": False, "error": "no_data",
                         "message": "No report data was provided to enhance."}), 200

    try:
        html = rag_engine.generate_report_html(instructions, report_text, provider=provider)
    except Exception:
        log.exception("generate_report_html() failed")
        html = None

    if not html:
        return jsonify({"ok": False, "error": "unreachable",
                         "message": "Wasn't able to reach the AI provider just now. Please try again."}), 200

    return jsonify({"ok": True, "html": html})


# ---------------------------------------------------------------------
# Serve the built React frontend (frontend/dist, from `npm run build`)
# so the whole app runs on this one origin/port — no separate frontend
# dev server needed. Registered last so it never shadows an /api/* route
# above. Falls back to a helpful message if the frontend hasn't been
# built yet, instead of a raw 404.
# ---------------------------------------------------------------------
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if not FRONTEND_DIST.exists():
        return (
            "Frontend build not found. Run 'npm run build' inside the "
            "frontend/ folder, then restart the backend.",
            503,
        )
    target = FRONTEND_DIST / path
    if path and target.exists() and target.is_file():
        return send_from_directory(FRONTEND_DIST, path)
    # Any other path (including "/") serves index.html — fine here since
    # this app has no client-side routes to fall back for.
    return send_from_directory(FRONTEND_DIST, "index.html")


# Start the background mail-poll scheduler unconditionally, not gated
# behind `if __name__ == "__main__"` — that guard only ever runs true
# under `python app.py`, never when the app is imported as a WSGI module
# by gunicorn (`gunicorn app:app`), which is how it actually runs in
# production. The scheduler was silently never starting there at all,
# meaning report emails were only ever picked up by a manual "Check
# mailbox" click, never automatically. Safe to run at import time here:
# Render's WEB_CONCURRENCY=1 default means exactly one worker process
# imports this module, so this runs exactly once.
_scheduler_holder["instance"] = start_background_scheduler()

# Start the background mail-poll scheduler and run the first mailbox
# sync, both unconditionally at import time — not gated behind
# `if __name__ == "__main__"`. That guard only ever runs true under
# `python app.py`, never when the app is imported as a WSGI module by
# gunicorn (`gunicorn app:app`), which is how it actually runs in
# production. Both were silently never running there at all, meaning
# report emails were only ever picked up by a manual "Check mailbox"
# click, never automatically. Safe to run at import time here: Render's
# WEB_CONCURRENCY=1 default means exactly one worker process imports
# this module, so this runs exactly once.
_scheduler_holder["instance"] = start_background_scheduler()


def _initial_sync():
    try:
        run_sync_once()
    except Exception:
        log.exception("Initial sync failed — check your provider credentials in Settings")


threading.Thread(target=_initial_sync, daemon=True).start()

if __name__ == "__main__":
    # Local dev only (`python app.py`) — production runs via gunicorn,
    # which binds the port itself using the Start Command instead.
    app.run(host=config.API_HOST, port=config.API_PORT, debug=False)
