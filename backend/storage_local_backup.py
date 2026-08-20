"""
Shared-folder storage: every fetched (or manually uploaded) report is a
file in REPORTS_DIR plus one entry in index.json. The chatbot's API reads
index.json to build the "Attached Reports" list.
"""
import json
import os
import uuid
try:
    import fcntl
except ImportError:
    fcntl = None
from datetime import datetime, timezone
from pathlib import Path

from config import REPORTS_DIR, INDEX_PATH, DELETED_INDEX_PATH, ALLOWED_EXTENSIONS


def _load_index():
    if not INDEX_PATH.exists():
        return []
    with open(INDEX_PATH, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save_index(entries):
    # Simple file lock so the scheduler and API requests don't race.
    with open(INDEX_PATH, "w") as f:
        if fcntl:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
            except (AttributeError, OSError):
                pass  # fcntl not available on Windows — fine for single-process use
        json.dump(entries, f, indent=2)


def _load_deleted_index():
    if not DELETED_INDEX_PATH.exists():
        return []
    with open(DELETED_INDEX_PATH, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save_deleted_index(entries):
    with open(DELETED_INDEX_PATH, "w") as f:
        if fcntl:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
            except (AttributeError, OSError):
                pass
        json.dump(entries, f, indent=2)


def list_reports():
    return sorted(_load_index(), key=lambda e: e["fetchedAt"], reverse=True)


def already_have(source, provider_message_id, filename):
    # Check if currently active in index
    in_active = any(
        e["source"] == source
        and e.get("providerMessageId") == provider_message_id
        and e["filename"] == filename
        for e in _load_index()
    )
    if in_active:
        return True

    # Check if previously deleted by user
    key = f"{source}:{provider_message_id}:{filename}"
    deleted_keys = _load_deleted_index()
    if key in deleted_keys:
        return True

    return False


def delete_reports(report_ids):
    """Deletes the given report ids: their stored file, extracted text
    (if any), and index entry. Records tombstones so background polling
    will not re-import previously deleted files."""
    ids = set(report_ids)
    entries = _load_index()
    deleted = []
    remaining = []
    deleted_tombstones = _load_deleted_index()

    for e in entries:
        if e["id"] in ids:
            file_path = REPORTS_DIR / e["storedAs"]
            if file_path.exists():
                file_path.unlink()
            text_path = REPORTS_DIR / f"{e['id']}.txt"
            if text_path.exists():
                text_path.unlink()
            deleted.append(e["id"])
            if e.get("providerMessageId"):
                key = f"{e['source']}:{e['providerMessageId']}:{e['filename']}"
                if key not in deleted_tombstones:
                    deleted_tombstones.append(key)
        else:
            remaining.append(e)

    _save_deleted_index(deleted_tombstones)
    _save_index(remaining)
    return deleted


def delete_all_reports():
    """Wipes every stored report file + the index and records tombstones for all."""
    entries = _load_index()
    deleted_tombstones = _load_deleted_index()
    for e in entries:
        file_path = REPORTS_DIR / e["storedAs"]
        if file_path.exists():
            file_path.unlink()
        text_path = REPORTS_DIR / f"{e['id']}.txt"
        if text_path.exists():
            text_path.unlink()
        if e.get("providerMessageId"):
            key = f"{e['source']}:{e['providerMessageId']}:{e['filename']}"
            if key not in deleted_tombstones:
                deleted_tombstones.append(key)

    _save_deleted_index(deleted_tombstones)
    _save_index([])

def _extract_pdf_text(path, max_chars=12000):
    """Best-effort text extraction so PDFs are searchable in chat like
    spreadsheets are. Returns '' if extraction fails for any reason —
    the PDF still gets stored and listed either way."""
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages[:30]:
                text = page.extract_text() or ""
                if text:
                    parts.append(text)
                if sum(len(p) for p in parts) > max_chars:
                    break
        return "\n\n".join(parts)[:max_chars]
    except Exception:
        return ""


def save_attachment(*, filename, content_bytes, source, provider_message_id,
                     sender=None, subject=None, received_at=None):
    """
    source: 'gmail' | 'outlook' | 'upload'
    Returns the new index entry, or None if the extension isn't allowed
    or this exact attachment was already saved.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None
    if provider_message_id and already_have(source, provider_message_id, filename):
        return None

    report_id = str(uuid.uuid4())
    safe_name = f"{report_id}{ext}"
    dest = REPORTS_DIR / safe_name
    with open(dest, "wb") as f:
        f.write(content_bytes)

    has_text = False
    if ext == ".pdf":
        text = _extract_pdf_text(dest)
        if text.strip():
            (REPORTS_DIR / f"{report_id}.txt").write_text(text, encoding="utf-8")
            has_text = True

    entry = {
        "id": report_id,
        "filename": filename,
        "storedAs": safe_name,
        "ext": ext.lstrip("."),
        "sizeBytes": len(content_bytes),
        "source": source,                       # gmail | outlook | upload
        "providerMessageId": provider_message_id,
        "sender": sender,
        "subject": subject,
        "receivedAt": received_at,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "hasExtractedText": has_text,            # true for PDFs we could read
    }
    entries = _load_index()
    entries.append(entry)
    _save_index(entries)
    return entry


def report_path(report_id):
    for e in _load_index():
        if e["id"] == report_id:
            return REPORTS_DIR / e["storedAs"], e
    return None, None


def extracted_text_path(report_id):
    path = REPORTS_DIR / f"{report_id}.txt"
    return path if path.exists() else None
