# TAXILLA AI Assistant — full project

Everything built in this conversation, packaged as a runnable project.

```
taxilla-chatbot/
├── frontend/     React app (Vite) — the chatbot UI
└── backend/      Flask service — mailbox sync (Gmail + Outlook) + Cohere/Gemini proxy
```

## What this app does

- Chat interface styled to match your enComply / TAXILLA product
- Pulls report attachments (xlsx/csv/pdf) from Gmail and Outlook —
  either via **fast polling** (as low as 30 seconds, works everywhere,
  zero setup) or **true real-time push** (instant, needs a public URL —
  see `backend/README.md`) — or accepts manual upload
- Answers questions grounded ONLY in your real, actually-stored reports — via
  either **Cohere** or **Gemini** (your choice), with no fabricated fallback
  data anywhere in the app. If nothing relevant is found, or no AI provider
  is configured, it says so plainly instead of guessing.
- Optional charts (bar/pie) per answer, toggle in Settings
- Settings panel lets you pick the active AI provider, set API keys, choose
  Gmail auth method (OAuth or App Password), configure mailbox sync speed,
  and turn on real-time push — all without touching a file

## Quick start

**1. Backend** (mailbox sync + AI Q&A)

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set **one** of these (required — the chatbot can't answer
questions without it):
```
COHERE_API_KEY=...     # get one at https://dashboard.cohere.com/api-keys
# or
GEMINI_API_KEY=...     # get one at https://aistudio.google.com/apikey
AI_PROVIDER=gemini      # only needed if you're using Gemini instead of Cohere (default is cohere)
```

Then:
```bash
python app.py
```

Runs on `http://localhost:5000`. Everything else (Gmail/Outlook credentials,
sender filter, poll interval, which AI provider is active) can also be set
later from the app's own Settings panel instead of editing `.env` — see
`backend/README.md` for the mailbox OAuth/App-Password setup steps.

**2. Frontend** (the chatbot)

```bash
cd frontend
npm install
npm run dev
```

Opens on `http://localhost:5173`, already pointed at `http://localhost:5000`
(see `API_BASE` near the top of `src/TaxillaChatbot.jsx` if you deploy the
backend somewhere else). The app starts with an empty report list — nothing
fabricated — and shows only reports that were genuinely fetched or uploaded.

## Switching AI providers later

Settings → gear icon → **"5. AI Configuration"** → pick Cohere or Gemini,
enter that provider's API key, save. No restart needed — it takes effect
immediately. You can have both keys saved and just flip which one is active.

## Getting mail into the app faster / instantly

Settings → gear icon → **"2. Email Integration"**:
- **Fetch Frequency** dropdown goes down to every 30 seconds — no setup needed.
- **"Real-Time Push"** section turns on true instant notifications (the
  moment an email lands, zero polling delay) — but this requires a
  publicly reachable URL, since Google/Microsoft can't reach `localhost`.
  For local testing, run `ngrok http 5000` and paste the URL it gives you
  into "Public Base URL". Full setup (including the one-time Google Cloud
  Pub/Sub step Gmail push needs): `backend/README.md`.

## Where each earlier request landed

| Ask | File |
|---|---|
| Chatbot UI matching the product screenshots | `frontend/src/TaxillaChatbot.jsx` |
| Fetch report emails from Gmail/Outlook | `backend/fetchers/gmail_fetcher.py` (OAuth), `backend/fetchers/gmail_imap_fetcher.py` (App Password), `backend/fetchers/outlook_fetcher.py` |
| Shared folder + index of reports | `backend/storage.py` |
| Scheduled polling | `backend/scheduler.py` |
| Report chunking + retrieval + AI answer generation | `backend/rag_engine.py` |
| API the frontend talks to | `backend/app.py` |
| Settings → AI provider, mailbox auth, visualization toggle | `SettingsModal` in `TaxillaChatbot.jsx` |
