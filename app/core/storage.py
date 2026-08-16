# ─────────────────────────────────────────────────────────────────────────────
# app/core/storage.py  (NEW)
#
# Swappable file storage. Local disk by default (development), Supabase
# Storage when configured (production).
#
# WHY THIS EXISTS
# Uploaded files were being written to the server's local disk. That is fine
# locally, but on most cloud hosts (Render included) the disk is wiped on
# every restart and redeploy, so real client KYC documents would silently
# disappear after a deploy. This routes uploads to durable cloud storage in
# production while keeping local disk for development, so nobody needs cloud
# credentials just to run the project.
#
# HOW TO ENABLE SUPABASE
#   1. Create a Storage bucket in your Supabase project (e.g. "pcil-uploads")
#   2. Add to .env:
#        STORAGE_BACKEND=supabase
#        SUPABASE_URL=https://<your-project>.supabase.co
#        SUPABASE_SERVICE_KEY=<service_role key, NOT the anon key>
#        SUPABASE_BUCKET=pcil-uploads
#   3. pip install supabase
#
# Leave STORAGE_BACKEND unset (or "local") and everything behaves exactly as
# it does today, no cloud account needed.
#
# BUCKET VISIBILITY: USE A PRIVATE BUCKET
# These files include passports, utility bills and other identity documents.
# In a public bucket, anyone holding the URL can open them with no login at
# all, which is not acceptable for client identity data.
#
# Documents are therefore served through an authenticated endpoint that
# streams the bytes (see read_file_bytes below and the /admin/kyc/{id}/document
# endpoint), so the storage location is never exposed to the browser. The
# stored URL is only ever used server-side as a lookup key.
#
# Set the bucket to PRIVATE in Supabase. Nothing in the app depends on public
# access.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _cfg(name: str, default: str = "") -> str:
    """
    Reads a setting from app.core.config if present, else the environment.
    Written defensively so this module works whether or not these keys have
    been added to the Settings class yet.
    """
    try:
        from app.core.config import settings as app_settings
        value = getattr(app_settings, name, None)
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv(name, default)


STORAGE_BACKEND = (_cfg("STORAGE_BACKEND", "local") or "local").lower()
SUPABASE_URL = _cfg("SUPABASE_URL")
SUPABASE_SERVICE_KEY = _cfg("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET = _cfg("SUPABASE_BUCKET", "pcil-uploads")

# Local disk root — matches where main.py mounts /static
LOCAL_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _supabase_client():
    """Lazily created so the supabase package is only needed if actually used."""
    from supabase import create_client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "STORAGE_BACKEND is 'supabase' but SUPABASE_URL / SUPABASE_SERVICE_KEY are not set."
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def build_filename(prefix: str, original_filename: Optional[str], content_type: Optional[str]) -> str:
    """
    Builds a collision-proof filename that keeps a sensible extension.
    Falls back to the content type when the browser sends no filename.
    """
    ext = Path(original_filename or "").suffix
    if not ext:
        ext = {
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(content_type or "", "")
    return f"{prefix}_{uuid.uuid4().hex[:8]}{ext}"


def save_file(content: bytes, folder: str, filename: str, content_type: Optional[str] = None,
              request_base_url: Optional[str] = None) -> str:
    """
    Saves file bytes and returns a URL the app can store and serve later.

    folder:           logical folder, e.g. "kyc-documents" or "avatars"
    request_base_url: only used by the local backend, to build an absolute
                      URL from the current request's own host rather than a
                      hardcoded one.

    Never silently falls back between backends: if Supabase is configured but
    failing, that raises rather than quietly writing to a disk that will be
    wiped, which would look like success while losing the file later.
    """
    if STORAGE_BACKEND == "supabase":
        client = _supabase_client()
        path = f"{folder}/{filename}"
        client.storage.from_(SUPABASE_BUCKET).upload(
            path=path,
            file=content,
            file_options={"content-type": content_type or "application/octet-stream", "upsert": "true"},
        )
        url = client.storage.from_(SUPABASE_BUCKET).get_public_url(path)
        logger.info(f"File saved to Supabase: {path}")
        return url

    # Local disk
    target_dir = LOCAL_STATIC_DIR / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / filename).write_bytes(content)
    base = (request_base_url or "http://localhost:8000").rstrip("/")
    logger.info(f"File saved locally: {folder}/{filename}")
    return f"{base}/static/{folder}/{filename}"


def delete_file(url: str) -> bool:
    """
    Best-effort delete of a previously saved file. Returns True if something
    was removed. Never raises: a failed cleanup should not break the request
    that triggered it (e.g. replacing an avatar still succeeds even if the
    old file couldn't be removed).
    """
    if not url:
        return False
    try:
        if STORAGE_BACKEND == "supabase":
            marker = f"/{SUPABASE_BUCKET}/"
            if marker not in url:
                return False
            path = url.split(marker, 1)[1].split("?")[0]
            _supabase_client().storage.from_(SUPABASE_BUCKET).remove([path])
            return True

        if "/static/" not in url:
            return False
        rel = url.split("/static/", 1)[1].split("?")[0]
        target = LOCAL_STATIC_DIR / rel
        if target.exists():
            target.unlink()
            return True
        return False
    except Exception as e:
        logger.warning(f"Could not delete stored file {url}: {e}")
        return False


def read_file_bytes(url: str):
    """
    Reads a stored file's bytes so it can be streamed to an authenticated
    user, rather than handing out a publicly reachable URL.

    Returns (content_bytes, content_type), or (None, None) if unreachable.
    Never raises, so a missing file surfaces as a clean 404 rather than a
    500 error.

    This is what allows KYC documents to live in a PRIVATE bucket: nothing
    outside the application ever receives a usable link to them.
    """
    import mimetypes

    if not url:
        return None, None

    guessed = mimetypes.guess_type(url.split("?")[0])[0]

    try:
        if STORAGE_BACKEND == "supabase":
            marker = f"/{SUPABASE_BUCKET}/"
            if marker in url:
                path = url.split(marker, 1)[1].split("?")[0]
            else:
                # Already a bare storage path rather than a full URL
                path = url.lstrip("/")
            content = _supabase_client().storage.from_(SUPABASE_BUCKET).download(path)
            return content, guessed

        local = read_local_path(url)
        if local:
            return local.read_bytes(), guessed
        return None, None
    except Exception as e:
        logger.warning(f"Could not read stored file {url}: {e}")
        return None, None


def get_signed_url(url: str, expires_in_seconds: int = 3600) -> str:
    """
    Returns a time-limited URL for a private Supabase bucket. Not used yet —
    the app currently stores permanent URLs from a public bucket. Provided so
    switching to a private bucket (recommended for KYC documents) is a
    contained change rather than a rewrite.

    Returns the original URL unchanged for local storage or on any failure,
    so callers never end up with a broken link.
    """
    if STORAGE_BACKEND != "supabase" or not url:
        return url
    try:
        marker = f"/{SUPABASE_BUCKET}/"
        if marker not in url:
            return url
        path = url.split(marker, 1)[1].split("?")[0]
        result = _supabase_client().storage.from_(SUPABASE_BUCKET).create_signed_url(path, expires_in_seconds)
        return result.get("signedURL") or result.get("signed_url") or url
    except Exception as e:
        logger.warning(f"Could not sign URL {url}: {e}")
        return url


def read_local_path(url: str) -> Optional[Path]:
    """
    Maps a stored URL back to a local disk path, for server-side reads such
    as embedding an uploaded image into a generated PDF. Returns None for
    cloud-hosted files, which must be fetched over HTTP instead.
    """
    if STORAGE_BACKEND != "local" or not url or "/static/" not in url:
        return None
    rel = url.split("/static/", 1)[1].split("?")[0]
    path = LOCAL_STATIC_DIR / rel
    return path if path.exists() else None
