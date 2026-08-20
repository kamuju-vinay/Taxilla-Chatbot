# TAXILLA Mailbox Sync Service

Fetches Gmail and Outlook/Microsoft 365 report attachments (xlsx/csv/pdf)
into a shared `reports/` folder that the TAXILLA AI Assistant reads from,
and answers questions about them via Cohere or Gemini.

Two ways to get new emails into the app:
- **Fast polling** (default, works everywhere, zero extra setup) — checks
  the mailbox on a timer, as low as every 30 seconds.
- **Real-time push** (optional, needs a public URL) — Gmail/Outlook notify
  this app the instant a new email arrives, no polling delay at all.

## 1. Install

```bash
cd backend
python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
cp .env.example .env
```

## 2. Set an AI provider (required)

In `.env`, set **one**:
```
COHERE_API_KEY=...     # https://dashboard.cohere.com/api-keys
```
or
```
AI_PROVIDER=gemini
GEMINI_API_KEY=...     # https://aistudio.google.com/apikey
```
Without this, the chatbot runs but every question gets an honest
"not configured yet" reply instead of an answer.

## 3. Set up mailbox credentials

**Gmail — OAuth (default)**
1. Google Cloud Console → APIs & Services → enable the Gmail API.
2. Credentials → Create OAuth Client ID → type **Desktop app**.
3. Download the JSON, save it as `backend/credentials.json`.

**Gmail — App Password (simpler alternative, no cloud project)**
```
GMAIL_AUTH_METHOD=imap
GMAIL_IMAP_ADDRESS=you@gmail.com
GMAIL_IMAP_APP_PASSWORD=your16charapppassword
```
Requires 2-Step Verification on; generate the password at
https://myaccount.google.com/apppasswords. Note: real-time push (below)
only works with OAuth — App Password mode always uses polling.

**Outlook**
1. Azure Portal → Entra ID → App registrations → New registration.
2. API permissions → Microsoft Graph → Delegated → add `Mail.Read` and
   `Mail.ReadWrite` (the latter is needed to create push subscriptions) →
   grant admin consent (or allow user consent).
3. Copy the **Application (client) ID** and **Directory (tenant) ID**
   into `.env` as `OUTLOOK_CLIENT_ID` / `OUTLOOK_TENANT_ID`.

Set `REPORT_SENDER_FILTER` in `.env` to the address your report process
actually sends from, so sync doesn't pick up unrelated attachments.

## 4. Run

```bash
python app.py
```

First run: Gmail (OAuth mode) opens a browser login once and saves
`gmail_token.json`; Outlook prints a device code + URL to log in once and
caches the token in `outlook_token_cache.json`. Both refresh silently
after that.

Everything above (sender filter, poll interval, AI provider, Gmail auth
method) can also be changed later from the app's own Settings panel —
no `.env` editing or restart required for those.

## 5. Real-time push (optional)

Real push notifications require Google/Microsoft to reach this backend
over the public internet — `localhost` is not reachable from their
servers. For local development, expose it with a tunnel:

```bash
ngrok http 5000
```

Copy the `https://...ngrok-free.app` URL it prints, paste it into
Settings → "2. Email Integration" → **Public Base URL** (or set
`PUBLIC_BASE_URL` in `.env`), then:

**For Gmail push**, you additionally need a Pub/Sub topic:
1. Google Cloud Console → Pub/Sub → Create topic (e.g. `gmail-push`).
2. Grant the topic's Publish role to `gmail-api-push@system.gserviceaccount.com`.
3. Create a **push subscription** on that topic with endpoint:
   `{PUBLIC_BASE_URL}/api/webhooks/gmail?secret={WEBHOOK_SHARED_SECRET}`
4. Put the topic's full name (`projects/PROJECT_ID/topics/gmail-push`)
   into `GMAIL_PUBSUB_TOPIC` in `.env`, or the "Gmail Pub/Sub Topic" field
   in Settings.
5. Click **"Enable Gmail Push"** in Settings — this calls Gmail's
   `watch()` API to start notifications flowing.

**For Outlook push**, no separate cloud console step is needed — just
click **"Enable Outlook Push"** in Settings once `PUBLIC_BASE_URL` is
set; the app creates the Microsoft Graph subscription automatically.

Both push mechanisms expire (Gmail: ~7 days, Outlook: ~3 days) and are
renewed automatically every hour by a background job while `app.py` is
running — no action needed once enabled.

**For production**, deploy the backend behind a real public domain with
HTTPS (not a tunnel) and set `PUBLIC_BASE_URL` to that domain.

## API reference

- `GET  /api/health` — liveness check
- `GET  /api/reports` — list of reports (fetched + uploaded)
- `GET  /api/reports/<id>/download` — original file
- `GET  /api/reports/<id>/text` — extracted PDF text (used by chat + preview)
- `POST /api/reports/upload` — manual upload from the chatbot UI
- `POST /api/sync` — trigger an immediate mailbox poll ("Check mailbox" button)
- `POST /api/ask` — answers a question, grounded in your real stored reports, via Cohere or Gemini
- `GET  /api/status` — connection state (Gmail/Outlook, AI provider, push status)
- `GET/POST /api/config` — all settings above, editable live from Settings
- `POST /api/push/<gmail|outlook>/enable` — turn on real-time push for that provider
- `POST /api/push/<gmail|outlook>/disable` — turn it back off
- `POST /api/webhooks/gmail` — Pub/Sub push target (not for direct use)
- `GET/POST /api/webhooks/outlook` — Graph subscription notification target (not for direct use)

## Notes

- If you only use one provider, set `GMAIL_ENABLED=false` or
  `OUTLOOK_ENABLED=false` in `.env` — the other's credentials become
  optional.
- `reports/index.json` is the single source of truth for what the
  chatbot can see. Back it up like any other data your company relies on.
- For a real deployment, run this behind a process manager (systemd,
  pm2, Docker + restart policy) rather than `python app.py` directly.
