# ─────────────────────────────────────────────────────────────────────────────
# app/core/security.py
#
# Authentication utilities:
#   - Password hashing (bcrypt via passlib)
#   - JWT token creation and verification
#   - Token types: access (15 min) + refresh (30 days)
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# ── Password Hashing ──────────────────────────────────────────────────────────
# bcrypt automatically salts passwords — never store plain text passwords
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """
    Hash a plain text password using bcrypt.
    Called when a user registers or changes their password.
    The hash includes a random salt — same password hashes differently each time.
    """
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain text password against a stored bcrypt hash.
    Called on every login attempt.
    Returns True if match, False otherwise.
    """
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT Token Creation ────────────────────────────────────────────────────────

def create_access_token(
    subject: str,
    extra_claims: Optional[Dict[str, Any]] = None
) -> str:
    """
    Create a short-lived JWT access token (default: 15 minutes).

    Args:
        subject:      The user's ID (UUID string) — stored as 'sub' claim
        extra_claims: Optional additional claims e.g. {'role': 'super_admin'}

    Returns:
        Signed JWT string to be sent to the client in the response body.

    Usage on frontend:
        Store in memory (NOT localStorage — XSS risk).
        Send as: Authorization: Bearer <token>
    """
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": subject,
        "exp": expires,
        "type": "access",
        **(extra_claims or {}),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(subject: str) -> str:
    """
    Create a long-lived JWT refresh token (default: 30 days).

    The refresh token is used to get a new access token when it expires.
    Store this in an HttpOnly cookie (done in the auth router).
    Never expose refresh tokens to JavaScript.
    """
    expires = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    payload = {
        "sub": subject,
        "exp": expires,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_mfa_token(subject: str) -> str:
    """
    Create a short-lived (5 min) MFA challenge token (NEW in v11).

    Issued by /auth/admin/login when the account has MFA enabled, in place
    of a real access token — proves "password was correct" without granting
    any actual access. The client sends this back to /auth/admin/mfa/login-verify
    along with their 6-digit code to exchange it for a real access token.

    Deliberately a distinct 'type' claim so decode_access_token() can never
    accidentally accept one of these as a real session token.
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    payload = {
        "sub": subject,
        "exp": expires,
        "type": "mfa",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_mfa_token(token: str) -> Optional[str]:
    """
    Decode an MFA challenge token and return the subject (admin ID).
    Returns None if invalid, expired, or not an MFA token.
    """
    payload = decode_token(token)
    if payload is None:
        return None
    if payload.get("type") != "mfa":
        return None
    return payload.get("sub")


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode and verify a JWT token.

    Returns the payload dict if valid, None if expired or tampered.
    The jose library automatically checks the 'exp' claim.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )
        return payload
    except JWTError:
        return None


def decode_access_token(token: str) -> Optional[str]:
    """
    Decode an access token and return the subject (user ID).
    Returns None if the token is invalid, expired, or not an access token.
    """
    payload = decode_token(token)
    if payload is None:
        return None
    if payload.get("type") != "access":
        return None
    return payload.get("sub")


def decode_refresh_token(token: str) -> Optional[str]:
    """
    Decode a refresh token and return the subject (user ID).
    Returns None if invalid, expired, or not a refresh token.
    """
    payload = decode_token(token)
    if payload is None:
        return None
    if payload.get("type") != "refresh":
        return None
    return payload.get("sub")


def generate_temp_password(length: int = 10) -> str:
    """
    Generate a secure random temporary password: letters + digits, guaranteed
    at least one of each. Used when staff create a client account, and when a
    super admin resets someone's password. Lives here rather than in an
    endpoint module so every caller shares one implementation.
    """
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pwd) and any(c.isalpha() for c in pwd):
            return pwd
