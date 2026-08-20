"""
Supabase-backed storage: report metadata lives in the `reports` Postgres
table, the original files live in the `reports` Storage bucket, and
deletion tombstones live in `deleted_reports`. See supabase_schema.sql.

A local on-disk cache (REPORTS_DIR) is still used underneath so the rest
of the app (rag_engine.py's xlsx/csv reading, send_file downloads) can
keep working with plain file paths unchanged — files are downloaded from
Supabase Storage into the cache on first access after a restart, then
served from disk from then on. Supabase is always the source of truth;
the local cache is disposable.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

from config import REPORTS_DIR, ALLOWED_EXTENSIONS, SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET

_client = None


def _sb():
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY "
                "(the secret/service_role key) in backend/.env."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def _row_to_entry(row):
    """DB column names are snake_case; the rest of the app (and the
    frontend) expects the original camelCase keys, so translate here."""
    return {
        "id": row["id"],
        "filename": row["filename"],
        "storedAs": row["stored_as"],
        "ext": row["ext"],
        "sizeBytes": row["size_bytes"],
        "source": row["source"],
        "providerMessageId": row.get("provider_message_id"),
        "sender": row.get("sender"),
        "subject": row.get("subject"),
        "receivedAt": row.get("received_at"),
        "fetchedAt": row["fetched_at"],
        "hasExtractedText": row.get("has_extracted_text", False),
    }


def list_reports():
    res = _sb().table("reports").select("*").order("fetched_at", desc=True).execute()
    return [_row_to_entry(r) for r in res.data]


def already_have(source, provider_message_id, filename):
    sb = _sb()
    active = (
        sb.table("reports")
        .select("id")
        .eq("source", source)
        .eq("provider_message_id", provider_message_id)
        .eq("filename", filename)
        .limit(1)
        .execute()
    )
    if active.data:
        return True

    key = f"{source}:{provider_message_id}:{filename}"
    deleted = sb.table("deleted_reports").select("key").eq("key", key).limit(1).execute()
    return bool(deleted.data)


def _cache_path(report_id, ext):
    return REPORTS_DIR / f"{report_id}{ext}"


# Render's free/starter disk is only 512MB total (shared with the Python
# runtime itself), and REPORTS_DIR is purely a disposable local cache —
# Supabase Storage is the real source of truth (see module docstring).
# Capping it and evicting the least-recently-used files keeps the app
# from ever filling the disk, since files simply get re-downloaded from
# Supabase on next access (_ensure_cached) if they're needed again.
_MAX_CACHE_BYTES = 150 * 1024 * 1024  # 150MB — comfortably under 512MB total


def _evict_cache_if_needed():
    try:
        entries = [p for p in REPORTS_DIR.glob("*") if p.is_file() and p.name not in ("index.json", "deleted_index.json")]
        total = sum(p.stat().st_size for p in entries)
        if total <= _MAX_CACHE_BYTES:
            return
        # Oldest-accessed first — st_atime is updated whenever a file is
        # read (downloads, chat retrieval), so this is a real LRU, not
        # just oldest-written.
        entries.sort(key=lambda p: p.stat().st_atime)
        for p in entries:
            if total <= _MAX_CACHE_BYTES:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
            except Exception:
                continue
    except Exception:
        pass  # cache eviction is best-effort — never let it break a request


def delete_reports(report_ids):
    sb = _sb()
    ids = list(set(report_ids))
    if not ids:
        return []

    rows = sb.table("reports").select("*").in_("id", ids).execute().data
    deleted = []
    tombstones = []

    for row in rows:
        # Remove from the storage bucket.
        try:
            sb.storage.from_(SUPABASE_BUCKET).remove([row["stored_as"]])
        except Exception:
            pass  # already gone / never uploaded — fine, we still clean up the row

        # Remove local cache copies, if any.
        cache_file = REPORTS_DIR / row["stored_as"]
        if cache_file.exists():
            cache_file.unlink()
        cache_text = REPORTS_DIR / f"{row['id']}.txt"
        if cache_text.exists():
            cache_text.unlink()

        deleted.append(row["id"])
        if row.get("provider_message_id"):
            tombstones.append({"key": f"{row['source']}:{row['provider_message_id']}:{row['filename']}"})

    if tombstones:
        sb.table("deleted_reports").upsert(tombstones, on_conflict="key").execute()
    if ids:
        sb.table("reports").delete().in_("id", ids).execute()

    return deleted


def delete_all_reports():
    sb = _sb()
    rows = sb.table("reports").select("id").execute().data
    ids = [r["id"] for r in rows]
    if ids:
        delete_reports(ids)


def _extract_pdf_text(path, max_chars=12000):
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

    sb = _sb()

    # Insert first to let Postgres generate the id (uuid default), so the
    # storage object name matches the row id.
    fetched_at = datetime.now(timezone.utc).isoformat()
    insert_res = (
        sb.table("reports")
        .insert(
            {
                "filename": filename,
                "stored_as": "",  # patched below once we know the id
                "ext": ext.lstrip("."),
                "size_bytes": len(content_bytes),
                "source": source,
                "provider_message_id": provider_message_id,
                "sender": sender,
                "subject": subject,
                "received_at": received_at,
                "fetched_at": fetched_at,
                "has_extracted_text": False,
            }
        )
        .execute()
    )
    row = insert_res.data[0]
    report_id = row["id"]
    safe_name = f"{report_id}{ext}"

    # Upload the file to Supabase Storage.
    content_type = {
        ".pdf": "application/pdf",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }.get(ext, "application/octet-stream")
    sb.storage.from_(SUPABASE_BUCKET).upload(
        safe_name, content_bytes, {"content-type": content_type}
    )

    # Cache locally too, so rag_engine/send_file can read it immediately
    # without a round trip back to Supabase.
    dest = _cache_path(report_id, ext)
    with open(dest, "wb") as f:
        f.write(content_bytes)
    _evict_cache_if_needed()

    has_text = False
    extracted_text = None
    if ext == ".pdf":
        text = _extract_pdf_text(dest)
        if text.strip():
            (REPORTS_DIR / f"{report_id}.txt").write_text(text, encoding="utf-8")
            has_text = True
            extracted_text = text

    update_payload = {"stored_as": safe_name, "has_extracted_text": has_text}
    if extracted_text is not None:
        update_payload["extracted_text"] = extracted_text
    sb.table("reports").update(update_payload).eq("id", report_id).execute()

    row.update(update_payload)
    return _row_to_entry(row)


def _ensure_cached(report_id, stored_as):
    """Downloads the file from Supabase Storage into the local cache if
    it isn't already there (e.g. after a restart or on a fresh machine)."""
    ext = Path(stored_as).suffix
    cache_file = _cache_path(report_id, ext)
    if cache_file.exists():
        return cache_file
    data = _sb().storage.from_(SUPABASE_BUCKET).download(stored_as)
    with open(cache_file, "wb") as f:
        f.write(data)
    _evict_cache_if_needed()
    return cache_file


def report_path(report_id):
    res = _sb().table("reports").select("*").eq("id", report_id).limit(1).execute()
    if not res.data:
        return None, None
    row = res.data[0]
    path = _ensure_cached(report_id, row["stored_as"])
    return path, _row_to_entry(row)


def extracted_text_path(report_id):
    """Writes the cached .txt from the DB column if needed, and returns
    its path (kept as a real file since rag_engine reads these as paths)."""
    cache_file = REPORTS_DIR / f"{report_id}.txt"
    if cache_file.exists():
        return cache_file
    res = (
        _sb()
        .table("reports")
        .select("extracted_text")
        .eq("id", report_id)
        .limit(1)
        .execute()
    )
    if not res.data or not res.data[0].get("extracted_text"):
        return None
    cache_file.write_text(res.data[0]["extracted_text"], encoding="utf-8")
    return cache_file
