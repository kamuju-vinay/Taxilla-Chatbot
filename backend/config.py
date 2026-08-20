"""
Central configuration, loaded from environment variables (see .env.example).
Nothing here is a secret by itself — actual credentials live in
credentials.json (Gmail) / your Azure app registration values (Outlook),
referenced by path/ID below.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from this backend/ folder before reading any environment
# variables below. Without this call every setting in .env is silently
# ignored and falls back to hardcoded defaults — verified as the actual
# cause of "mail configuration not working": Outlook client/tenant ID,
# the sender filter, COHERE_API_KEY/GEMINI_API_KEY, and
# GMAIL_ENABLED/OUTLOOK_ENABLED were all being ignored no matter what
# was in .env.
load_dotenv(Path(__file__).resolve().parent / ".env")

BASE_DIR = Path(__file__).resolve().parent

# Shared folder the chatbot frontend reads from. Point this at a folder
# that both this service and the chatbot's server (or a mounted volume /
# network share) can see.
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", BASE_DIR / "reports"))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = REPORTS_DIR / "index.json"
DELETED_INDEX_PATH = REPORTS_DIR / "deleted_index.json"

# How often to poll both mailboxes. POLL_INTERVAL_SECONDS takes priority
# if set; otherwise it's derived from POLL_INTERVAL_MINUTES. This is the
# "near real-time" mechanism that works with zero extra setup — it can go
# as low as 30 seconds. True instant push (below) is a separate mechanism
# layered on top, not a replacement for this.
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "10"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "0")) or POLL_INTERVAL_MINUTES * 60

# Only attachments with these extensions are saved.
ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf"}

# Optional filters so the fetchers only pick up the automated report
# emails, not every attachment in the inbox. Leave blank to disable.
SENDER_FILTER = os.environ.get("REPORT_SENDER_FILTER", "")   # e.g. "reports@yourcompany.com"
SUBJECT_FILTER = os.environ.get("REPORT_SUBJECT_FILTER", "") # e.g. "Daily Report"

# --- Gmail ---
GMAIL_ENABLED = os.environ.get("GMAIL_ENABLED", "true").lower() == "true"
# "oauth" (default, uses gmail_fetcher.py) or "imap" (uses gmail_imap_fetcher.py,
# simpler App-Password auth — see that file's docstring).
GMAIL_AUTH_METHOD = os.environ.get("GMAIL_AUTH_METHOD", "oauth").lower()
GMAIL_CREDENTIALS_PATH = os.environ.get("GMAIL_CREDENTIALS_PATH", str(BASE_DIR / "credentials.json"))
GMAIL_TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", str(BASE_DIR / "gmail_token.json"))
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_IMAP_ADDRESS = os.environ.get("GMAIL_IMAP_ADDRESS", "")
GMAIL_IMAP_APP_PASSWORD = os.environ.get("GMAIL_IMAP_APP_PASSWORD", "")
#FETCH_SINCE_DATE = os.environ.get("FETCH_SINCE_DATE", "")  # format: YYYY/MM/DD, e.g. 2026/08/18

# --- Outlook / Microsoft 365 ---
OUTLOOK_ENABLED = os.environ.get("OUTLOOK_ENABLED", "true").lower() == "true"
OUTLOOK_CLIENT_ID = os.environ.get("OUTLOOK_CLIENT_ID", "")
OUTLOOK_TENANT_ID = os.environ.get("OUTLOOK_TENANT_ID", "common")
OUTLOOK_TOKEN_CACHE_PATH = os.environ.get("OUTLOOK_TOKEN_CACHE_PATH", str(BASE_DIR / "outlook_token_cache.json"))
OUTLOOK_SCOPES = ["Mail.Read"]

# --- Real-time push (instant notifications instead of polling) ---
# Both Gmail and Outlook need a publicly reachable HTTPS URL to send push
# notifications to — they cannot reach localhost. Set PUBLIC_BASE_URL to
# that public URL (e.g. an ngrok/Cloudflare Tunnel URL, or your real
# deployed domain) to enable this. Leave blank to just use fast polling
# instead — that works everywhere with no extra setup.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Gmail push uses Google Cloud Pub/Sub. You need: a Pub/Sub topic, Gmail's
# push service account (gmail-api-push@system.gserviceaccount.com) granted
# Publish rights on it, and a push subscription on that topic pointing to
# {PUBLIC_BASE_URL}/api/webhooks/gmail?secret=WEBHOOK_SHARED_SECRET.
# Full steps: backend/README.md.
GMAIL_PUBSUB_TOPIC = os.environ.get("GMAIL_PUBSUB_TOPIC", "")  # e.g. projects/my-project/topics/gmail-push
GMAIL_WATCH_STATE_PATH = Path(os.environ.get("GMAIL_WATCH_STATE_PATH", str(BASE_DIR / "gmail_watch_state.json")))

# Outlook push uses a Microsoft Graph subscription — created automatically
# by this app (no manual cloud console step needed beyond the existing
# Azure app registration), as long as PUBLIC_BASE_URL is set.
OUTLOOK_SUBSCRIPTION_STATE_PATH = Path(os.environ.get("OUTLOOK_SUBSCRIPTION_STATE_PATH", str(BASE_DIR / "outlook_subscription_state.json")))

# Shared secret appended as a query param to both webhook URLs, and used
# as Outlook's clientState, so random internet traffic can't trigger a
# mailbox sync. Set your own value in .env for anything beyond local testing.
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "dev-secret-change-me")

# API server
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "5000"))
# Comma-separated list of allowed origins for the frontend dev server(s).
# Defaults to the standard local Vite ports on both localhost and 127.0.0.1
# (whichever one the browser happens to use) — tighter than "*" while
# still requiring zero config for the normal local-dev setup. Override in
# .env with your own value (or "*") if you need something broader, e.g.
# a deployed frontend domain.
CORS_ORIGIN = os.environ.get(
    "CORS_ORIGIN",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5000,http://127.0.0.1:5000",
).split(",")

# --- Runtime-editable settings ---
RUNTIME_CONFIG_PATH = Path(os.environ.get("RUNTIME_CONFIG_PATH", BASE_DIR / "runtime_config.json"))
# Neither key has a hardcoded default on purpose — set one via your own
# .env file or the Settings panel. AI_PROVIDER picks which one actually
# generates chat answers; the other can still be set but won't be used
# for generation unless you switch AI_PROVIDER to it.
COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# --- Supabase (metadata table + file storage bucket) ---
# SUPABASE_KEY must be the secret/service_role key (server-side only —
# bypasses row level security). Never put this in frontend code.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "reports")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "cohere").lower()  # "cohere" | "gemini" | "groq"
COHERE_CHAT_MODEL = os.environ.get("COHERE_CHAT_MODEL", "command-r-plus")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


def _load_runtime_overrides():
    global SENDER_FILTER, SUBJECT_FILTER, POLL_INTERVAL_MINUTES, POLL_INTERVAL_SECONDS, GMAIL_ENABLED, OUTLOOK_ENABLED, COHERE_API_KEY
    global GMAIL_AUTH_METHOD, GMAIL_IMAP_ADDRESS, GMAIL_IMAP_APP_PASSWORD
    global GEMINI_API_KEY, GROQ_API_KEY, AI_PROVIDER, COHERE_CHAT_MODEL, GEMINI_MODEL, GROQ_MODEL
    global PUBLIC_BASE_URL, GMAIL_PUBSUB_TOPIC
    if not RUNTIME_CONFIG_PATH.exists():
        return
    try:
        import json
        data = json.loads(RUNTIME_CONFIG_PATH.read_text())
    except Exception:
        return
    if "senderFilter" in data:
        SENDER_FILTER = data["senderFilter"]
    if "subjectFilter" in data:
        SUBJECT_FILTER = data["subjectFilter"]
    if "pollIntervalSeconds" in data:
        POLL_INTERVAL_SECONDS = int(data["pollIntervalSeconds"])
    elif "pollIntervalMinutes" in data:
        POLL_INTERVAL_MINUTES = int(data["pollIntervalMinutes"])
        POLL_INTERVAL_SECONDS = POLL_INTERVAL_MINUTES * 60
    if "gmailEnabled" in data:
        GMAIL_ENABLED = bool(data["gmailEnabled"])
    if "outlookEnabled" in data:
        OUTLOOK_ENABLED = bool(data["outlookEnabled"])
    if "cohereApiKey" in data:
        COHERE_API_KEY = str(data["cohereApiKey"])
    if "gmailAuthMethod" in data:
        GMAIL_AUTH_METHOD = str(data["gmailAuthMethod"]).lower()
    if "gmailImapAddress" in data:
        GMAIL_IMAP_ADDRESS = str(data["gmailImapAddress"])
    if "gmailImapAppPassword" in data:
        GMAIL_IMAP_APP_PASSWORD = str(data["gmailImapAppPassword"])
    if "geminiApiKey" in data:
        GEMINI_API_KEY = str(data["geminiApiKey"])
    if "groqApiKey" in data:
        GROQ_API_KEY = str(data["groqApiKey"])
    if "aiProvider" in data:
        AI_PROVIDER = str(data["aiProvider"]).lower()
    if "cohereChatModel" in data:
        COHERE_CHAT_MODEL = str(data["cohereChatModel"])
    if "geminiModel" in data:
        GEMINI_MODEL = str(data["geminiModel"])
    if "groqModel" in data:
        GROQ_MODEL = str(data["groqModel"])
    if "publicBaseUrl" in data:
        PUBLIC_BASE_URL = str(data["publicBaseUrl"]).rstrip("/")
    if "gmailPubsubTopic" in data:
        GMAIL_PUBSUB_TOPIC = str(data["gmailPubsubTopic"])


def save_runtime_overrides(**kwargs):
    """Called from the API when Settings are changed. Persists to disk and
    updates this module's live values without server restart.

    NOTE: gmailImapAppPassword / geminiApiKey / cohereApiKey end up in
    runtime_config.json in plaintext, same as they would in a .env file —
    treat that file as a secret and keep it out of version control (see
    .gitignore)."""
    import json
    global SENDER_FILTER, SUBJECT_FILTER, POLL_INTERVAL_MINUTES, POLL_INTERVAL_SECONDS, GMAIL_ENABLED, OUTLOOK_ENABLED, COHERE_API_KEY
    global GMAIL_AUTH_METHOD, GMAIL_IMAP_ADDRESS, GMAIL_IMAP_APP_PASSWORD
    global GEMINI_API_KEY, GROQ_API_KEY, AI_PROVIDER, COHERE_CHAT_MODEL, GEMINI_MODEL, GROQ_MODEL
    global PUBLIC_BASE_URL, GMAIL_PUBSUB_TOPIC
    current = {}
    if RUNTIME_CONFIG_PATH.exists():
        try:
            current = json.loads(RUNTIME_CONFIG_PATH.read_text())
        except Exception:
            current = {}
    current.update(kwargs)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(current, indent=2))
    _load_runtime_overrides()


_load_runtime_overrides()
