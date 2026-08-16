# ─────────────────────────────────────────────────────────────────────────────
# app/core/mfa.py  (NEW in v11)
#
# TOTP-based MFA helpers — used for both staff (mandatory) and client
# (optional) MFA. Wraps pyotp for the TOTP math and qrcode for generating a
# scannable provisioning QR code, base64-encoded so it can go straight into
# a JSON response and render as an <img src="data:image/png;base64,..."> on
# the frontend with no separate image-hosting endpoint needed.
#
# Requires: pip install pyotp qrcode[pil]
# ─────────────────────────────────────────────────────────────────────────────

import base64
import io

import pyotp
import qrcode

from app.core.config import settings

# Shown in the authenticator app (Google Authenticator, Authy, etc.) next to
# the account name — lets the user tell this apart from other TOTP entries.
MFA_ISSUER = getattr(settings, "APP_NAME", "Prime Capital & Investment Ltd")


def generate_mfa_secret() -> str:
    """Generate a new random base32 TOTP secret."""
    return pyotp.random_base32()


def get_provisioning_uri(secret: str, account_email: str) -> str:
    """
    Build the otpauth:// URI that authenticator apps understand.
    Encoded into the QR code below, and also returned as plain text so the
    user can type it in manually if they can't scan (some apps support
    pasting the URI directly, or the raw secret via manual entry).
    """
    return pyotp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=MFA_ISSUER)


def generate_qr_code_base64(otpauth_uri: str) -> str:
    """
    Render the provisioning URI as a QR code PNG, base64-encoded as a data URI.
    Frontend can drop this straight into an <img src="..."> with no extra fetch.
    """
    img = qrcode.make(otpauth_uri)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def verify_totp_code(secret: str, code: str) -> bool:
    """
    Verify a 6-digit TOTP code against a secret.
    valid_window=1 allows the code from one 30s step before/after the
    current one, to tolerate minor clock drift between server and phone.
    """
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)
