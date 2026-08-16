# ─────────────────────────────────────────────────────────────────────────────
# app/core/email.py  (NEW)
#
# Outbound email. Everything in the app was in-portal only, so a client who
# was denied at KYC, had their password reset, or had an investment mature
# only found out if they happened to log in.
#
# DEFAULT BEHAVIOUR: logs the email instead of sending it. Nothing breaks and
# no credentials are needed for local development; you can see exactly what
# would have been sent in the server output.
#
# TO ENABLE REAL SENDING, add to .env:
#   EMAIL_BACKEND=smtp
#   SMTP_HOST=smtp.gmail.com          (or your provider)
#   SMTP_PORT=587
#   SMTP_USER=<username>
#   SMTP_PASSWORD=<password or app password>
#   SMTP_FROM=Prime Capital <noreply@primecapital.ng>
#   SMTP_USE_TLS=true
#
# Sending never raises. A failed email must not roll back the action that
# triggered it: a client's password reset should still succeed even if the
# mail server is briefly unreachable. Failures are logged for follow-up.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


def _cfg(name: str, default: str = "") -> str:
    """Reads from app.core.config if present, else the environment."""
    try:
        from app.core.config import settings as app_settings
        value = getattr(app_settings, name, None)
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv(name, default)


GOLD = "#A67C1A"


def _wrap(title: str, body_html: str, cta_label: str = "", cta_url: str = "") -> str:
    """
    Wraps message content in a branded shell. Deliberately simple, table-free
    inline CSS, because email clients (Outlook especially) mangle anything
    more ambitious.
    """
    cta = ""
    if cta_label and cta_url:
        cta = (
            f'<p style="margin:28px 0;">'
            f'<a href="{cta_url}" style="background:{GOLD};color:#ffffff;text-decoration:none;'
            f'padding:12px 28px;border-radius:8px;font-weight:600;display:inline-block;">{cta_label}</a>'
            f'</p>'
        )
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f5f5f5;font-family:Helvetica,Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;padding:32px;">
    <h1 style="color:{GOLD};font-size:20px;margin:0 0 4px;">Prime Capital &amp; Investment Ltd</h1>
    <p style="color:#888;font-size:12px;margin:0 0 24px;">{title}</p>
    <div style="color:#333;font-size:14px;line-height:1.6;">{body_html}</div>
    {cta}
    <hr style="border:none;border-top:1px solid #eee;margin:28px 0 16px;">
    <p style="color:#999;font-size:11px;margin:0;">
      This is an automated message from the Prime Capital Investor Portal.
      If you weren't expecting it, please contact us.
    </p>
  </div>
</body></html>"""


def send_email(to: str, subject: str, body_html: str, body_text: Optional[str] = None) -> bool:
    """
    Sends an email. Returns True if sent (or logged in dev), False on failure.
    Never raises.
    """
    backend = (_cfg("EMAIL_BACKEND", "log") or "log").lower()
    sender = _cfg("SMTP_FROM", "Prime Capital <noreply@primecapital.ng>")

    if backend != "smtp":
        logger.info(
            "[EMAIL not sent: EMAIL_BACKEND is not 'smtp']\n"
            f"  To:      {to}\n"
            f"  Subject: {subject}\n"
            f"  Body:    {(body_text or body_html)[:400]}"
        )
        return True

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        msg.set_content(body_text or "Please view this message in an HTML-capable email client.")
        msg.add_alternative(body_html, subtype="html")

        host = _cfg("SMTP_HOST")
        port = int(_cfg("SMTP_PORT", "587") or 587)
        user = _cfg("SMTP_USER")
        password = _cfg("SMTP_PASSWORD")
        use_tls = (_cfg("SMTP_USE_TLS", "true") or "true").lower() == "true"

        if not host:
            logger.warning("EMAIL_BACKEND is 'smtp' but SMTP_HOST is not set. Email not sent.")
            return False

        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)

        logger.info(f"Email sent to {to}: {subject}")
        return True
    except Exception as e:
        # Deliberately swallowed: a mail failure must not undo the action that
        # triggered it (password reset, KYC decision, etc).
        logger.warning(f"Could not send email to {to} ({subject}): {e}")
        return False


# ── Specific messages ────────────────────────────────────────────────────────

def send_password_reset(to: str, full_name: str, reset_url: str, expires_minutes: int = 30) -> bool:
    return send_email(
        to,
        "Reset your Prime Capital password",
        _wrap(
            "Password reset",
            f"<p>Hello {full_name},</p>"
            f"<p>We received a request to reset your portal password. "
            f"Use the button below within the next {expires_minutes} minutes.</p>"
            f"<p style='color:#888;font-size:13px;'>If you didn't request this, you can ignore "
            f"this email. Your password won't change.</p>",
            "Reset Password", reset_url,
        ),
        body_text=f"Hello {full_name},\n\nReset your password: {reset_url}\n\n"
                  f"This link expires in {expires_minutes} minutes. If you didn't request it, ignore this email.",
    )


def send_kyc_decision(to: str, full_name: str, approved: bool, reason: str = "") -> bool:
    if approved:
        return send_email(
            to, "Your KYC has been approved",
            _wrap("KYC approved",
                  f"<p>Hello {full_name},</p><p>Your KYC verification has been approved. "
                  f"You can now subscribe to our investment products.</p>"),
            body_text=f"Hello {full_name},\n\nYour KYC verification has been approved.",
        )
    return send_email(
        to, "Your KYC needs attention",
        _wrap("KYC not approved",
              f"<p>Hello {full_name},</p><p>Your KYC submission was not approved.</p>"
              f"<p><strong>Reason:</strong> {reason or 'Not specified'}</p>"
              f"<p>Please sign in to review and resubmit.</p>"),
        body_text=f"Hello {full_name},\n\nYour KYC was not approved.\nReason: {reason or 'Not specified'}\n\nPlease sign in to resubmit.",
    )


def send_temp_password(to: str, full_name: str, temp_password: str, is_staff: bool = False) -> bool:
    who = "staff account" if is_staff else "investor portal account"
    return send_email(
        to, "Your Prime Capital account access",
        _wrap("Account access",
              f"<p>Hello {full_name},</p>"
              f"<p>A temporary password has been set for your {who}:</p>"
              f"<p style='font-family:monospace;font-size:16px;background:#f5f5f5;"
              f"padding:12px 16px;border-radius:8px;display:inline-block;'>{temp_password}</p>"
              f"<p>You'll be asked to choose your own password the first time you sign in.</p>"),
        body_text=f"Hello {full_name},\n\nTemporary password: {temp_password}\n\n"
                  f"You'll be asked to set your own password when you first sign in.",
    )


def send_subscription_active(to: str, full_name: str, product: str, amount: str, reference: str) -> bool:
    return send_email(
        to, "Your investment is now active",
        _wrap("Investment activated",
              f"<p>Hello {full_name},</p>"
              f"<p>Your investment has been confirmed and is now active.</p>"
              f"<p><strong>Product:</strong> {product}<br>"
              f"<strong>Amount:</strong> {amount}<br>"
              f"<strong>Reference:</strong> {reference}</p>"
              f"<p>You can track it any time from your dashboard.</p>"),
        body_text=f"Hello {full_name},\n\nYour investment is now active.\n"
                  f"Product: {product}\nAmount: {amount}\nReference: {reference}",
    )


def send_email_verification(to: str, full_name: str, verify_url: str, expires_hours: int = 48) -> bool:
    return send_email(
        to,
        "Confirm your email address",
        _wrap(
            "Email verification",
            f"<p>Hello {full_name},</p>"
            f"<p>Thanks for registering with Prime Capital. Please confirm this is your "
            f"email address so we can keep you updated about your investments.</p>"
            f"<p style='color:#888;font-size:13px;'>This link is valid for {expires_hours} hours.</p>",
            "Confirm Email", verify_url,
        ),
        body_text=f"Hello {full_name},\n\nConfirm your email address: {verify_url}\n\n"
                  f"This link is valid for {expires_hours} hours.",
    )
