# ─────────────────────────────────────────────────────────────────────────────
# app/core/security_middleware.py
#
# Security hardening for Prime Capital portal.
# PLACE THIS FILE AT: pcil-backend/app/core/security_middleware.py
#
# What this adds:
#   1. Security response headers (HSTS, X-Frame-Options, CSP, etc.)
#   2. In-memory rate limiter (no Redis required — works standalone)
#   3. File upload validation (type + size + magic bytes)
#   4. Input sanitiser for KYC text fields (strips HTML/XSS)
# ─────────────────────────────────────────────────────────────────────────────

import re
import time
import logging
import os
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. SECURITY HEADERS
# ─────────────────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Injects security headers into every HTTP response.
    Protects against clickjacking, MIME sniffing, XSS, and insecure transport.
    """
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        h = response.headers

        # Prevent iframe embedding (clickjacking protection)
        h["X-Frame-Options"] = "DENY"

        # Prevent MIME-type sniffing (e.g. serving a .js as text/html)
        h["X-Content-Type-Options"] = "nosniff"

        # Force HTTPS for 1 year — skip for localhost so dev still works
        if request.url.hostname not in ("localhost", "127.0.0.1"):
            h["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Legacy XSS protection for older browsers
        h["X-XSS-Protection"] = "1; mode=block"

        # Don't leak full URL in Referer header to third parties
        h["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Disable browser features the portal doesn't use
        h["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )

        # Content-Security-Policy
        # Adjust script-src if you add any CDN scripts later
        h["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
            "font-src 'self' fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )

        return response


# ─────────────────────────────────────────────────────────────────────────────
# 2. IN-MEMORY RATE LIMITER
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimitStore:
    """
    Sliding-window counter stored in process memory.
    Thread-safe for asyncio (single-threaded event loop).
    Cleans up stale entries every 5 minutes to prevent memory growth.
    """
    def __init__(self):
        self._store: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup: float = time.time()

    def _cleanup(self):
        now = time.time()
        if now - self._last_cleanup < 300:
            return
        cutoff = now - 3600  # discard anything older than 1 hour
        to_delete = []
        for key, ts_list in self._store.items():
            fresh = [t for t in ts_list if t > cutoff]
            if fresh:
                self._store[key] = fresh
            else:
                to_delete.append(key)
        for k in to_delete:
            del self._store[k]
        self._last_cleanup = now

    def check(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        """
        Returns (is_allowed, retry_after_seconds).
        Records the attempt if allowed.
        """
        self._cleanup()
        now = time.time()
        cutoff = now - window_seconds
        self._store[key] = [t for t in self._store[key] if t > cutoff]

        if len(self._store[key]) >= max_requests:
            oldest = min(self._store[key])
            retry_after = int(oldest + window_seconds - now) + 1
            return False, retry_after

        self._store[key].append(now)
        return True, 0


_store = _RateLimitStore()

# Rules: (path_fragment, max_requests, window_seconds)
# Applied to POST requests only
_RATE_RULES: list[tuple[str, int, int]] = [
    ("/auth/login",       5,   900),   # 5 attempts / 15 min per IP
    ("/auth/admin/login", 5,   900),   # same for admin
    ("/auth/register",   10,  3600),   # 10 registrations / hour per IP
    ("/kyc/submit",       5,  3600),   # 5 KYC submissions / hour per IP
]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Applies rate limiting to sensitive POST endpoints.
    Returns HTTP 429 with Retry-After header when limit exceeded.
    """
    async def dispatch(self, request: Request, call_next):
        if request.method == "POST":
            ip = (request.client.host if request.client else "unknown")
            path = request.url.path

            for fragment, max_req, window in _RATE_RULES:
                if fragment in path:
                    allowed, retry_after = _store.check(
                        f"{ip}:{fragment}", max_req, window
                    )
                    if not allowed:
                        logger.warning(
                            f"Rate limit hit: ip={ip} path={path} "
                            f"retry_after={retry_after}s"
                        )
                        return Response(
                            content='{"detail":"Too many attempts. Please try again later."}',
                            status_code=429,
                            media_type="application/json",
                            headers={"Retry-After": str(retry_after)},
                        )
                    break  # only apply first matching rule

        return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# 3. FILE UPLOAD VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

_ALLOWED_RECEIPT_MIME   = {"image/jpeg", "image/jpg", "image/png", "image/webp", "application/pdf"}
_ALLOWED_RECEIPT_EXT    = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
_MAX_RECEIPT_BYTES      = 10 * 1024 * 1024   # 10 MB

_ALLOWED_KYC_MIME       = {"image/jpeg", "image/jpg", "image/png", "application/pdf"}
_MAX_KYC_DOC_BYTES      = 15 * 1024 * 1024   # 15 MB

# Magic bytes → expected MIME (first 8 bytes of file content)
_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff",        "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n",  "image/png"),
    (b"%PDF",                "application/pdf"),
    (b"RIFF",                "image/webp"),
]


def _detect_mime(header: bytes) -> Optional[str]:
    for magic, mime in _MAGIC:
        if header[:len(magic)] == magic:
            return mime
    return None


async def validate_receipt_file(file) -> bytes:
    """
    Validates a payment receipt upload.
    Checks: size ≤ 10 MB, extension, MIME type, magic bytes.
    Returns raw bytes on success. Raises HTTPException on failure.
    Call this inside POST /subscriptions/{id}/receipt.
    """
    content = await file.read()

    if len(content) > _MAX_RECEIPT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large. Maximum allowed size is 10 MB.",
        )

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_RECEIPT_EXT:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File type not allowed. Please upload JPEG, PNG, WEBP, or PDF.",
        )

    declared_mime = (file.content_type or "").split(";")[0].strip().lower()
    if declared_mime not in _ALLOWED_RECEIPT_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File type not allowed.",
        )

    # Magic bytes check — catches renamed executables
    detected = _detect_mime(content[:16])
    if detected:
        normalised_declared = "image/jpeg" if declared_mime == "image/jpg" else declared_mime
        if detected != normalised_declared:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="File content does not match its declared type. Upload rejected.",
            )

    return content


async def validate_kyc_document(file) -> bytes:
    """
    Validates a KYC document upload.
    Checks: size ≤ 15 MB, MIME type, magic bytes.
    Returns raw bytes on success.
    """
    content = await file.read()

    if len(content) > _MAX_KYC_DOC_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large. Maximum allowed size is 15 MB.",
        )

    declared_mime = (file.content_type or "").split(";")[0].strip().lower()
    if declared_mime not in _ALLOWED_KYC_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File type not allowed. Please upload JPEG, PNG, or PDF.",
        )

    detected = _detect_mime(content[:16])
    if detected:
        normalised = "image/jpeg" if declared_mime == "image/jpg" else declared_mime
        if detected != normalised:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="File content does not match its declared type. Upload rejected.",
            )

    return content


# ─────────────────────────────────────────────────────────────────────────────
# 4. INPUT SANITISER
# ─────────────────────────────────────────────────────────────────────────────

_HTML_TAG   = re.compile(r"<[^>]+>")
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keep \t \n \r


def sanitise(value: Optional[str], max_length: int = 500) -> Optional[str]:
    """
    Strip HTML tags, null bytes, and control characters from a string.
    Trims to max_length. Returns None if result is empty.
    Use on every KYC text field before storing in the DB.
    """
    if value is None:
        return None
    value = _HTML_TAG.sub("", value)       # strip HTML
    value = _CTRL_CHARS.sub("", value)     # strip control chars
    value = value.strip()[:max_length]
    return value or None


# KYC fields that must be sanitised
_KYC_TEXT_FIELDS = [
    "date_of_birth", "nationality", "state", "lga", "address",
    "occupation", "employer", "annual_income", "investment_experience",
    "risk_profile", "company_name", "registration_number",
    "company_category", "bank_name", "account_number",
    "account_name", "bvn",
]


def sanitise_kyc(data: dict) -> dict:
    """
    Sanitise all KYC text fields in a payload dict.
    Call this at the top of POST /kyc/submit before any DB writes.
    """
    out = dict(data)
    for field in _KYC_TEXT_FIELDS:
        if field in out and isinstance(out[field], str):
            out[field] = sanitise(out[field])
    return out
