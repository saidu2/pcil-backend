# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/auth.py
#
# Authentication endpoints for BOTH client and admin logins.
#
# CLIENT endpoints:
#   POST /api/v1/auth/register     — new client registration
#   POST /api/v1/auth/login        — client login → returns JWT
#   POST /api/v1/auth/refresh      — get new access token using refresh cookie
#   POST /api/v1/auth/logout       — clear refresh token cookie
#   GET  /api/v1/auth/me           — get current logged-in client profile
#
# ADMIN endpoints:
#   POST /api/v1/auth/admin/login  — admin login → returns JWT + sets cookie
#   GET  /api/v1/auth/admin/me     — get current admin profile
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, Request, UploadFile, File, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_refresh_token,
    create_mfa_token, decode_mfa_token, generate_temp_password,
)
from app.core.storage import save_file, delete_file, build_filename
from app.core.mailer import send_password_reset, send_temp_password, send_email_verification
from app.core.mfa import generate_mfa_secret, get_provisioning_uri, generate_qr_code_base64, verify_totp_code
from app.db.session import get_db
from app.models.models import User, AdminUser, AuditLog, Workflow, WorkflowInstance, Product, Subscription, KycSubmission, Notification, PasswordResetToken
from app.api.v1.endpoints.subscriptions import generate_reference as generate_subscription_reference
from app.schemas.schemas import (
    UserRegister, UserLogin, TokenResponse, AdminLogin, AdminLoginResponse,
    UserResponse, AdminUserResponse, MessageResponse,
    ChangePassword, AdminCreateClient, ClientCreatedResponse,
    AdminChangePassword, AdminResetPassword, PasswordResetResponse,
    ForgotPasswordRequest, ResetPasswordWithToken, VerifyEmailRequest,
    MfaSetupResponse, MfaVerifyRequest, MfaLoginVerifyRequest, MfaDisableRequest,
)
from app.api.v1.deps import get_current_user, get_current_admin, require_staff_permission
from app.core.config import settings

logger = logging.getLogger(__name__)

# How long a password reset link stays valid.
RESET_TOKEN_MINUTES = 30

# How long an email confirmation link stays valid.
VERIFY_TOKEN_HOURS = 48


def _cfg_frontend_url(request: Request) -> str:
    """
    Base URL for links sent to clients. FRONTEND_URL in production; falls back
    to the request's own origin so reset links work locally without config.
    """
    configured = os.getenv("FRONTEND_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    return str(request.base_url).rstrip("/")
router = APIRouter()

# ── Cookie settings ───────────────────────────────────────────────────────────
# Refresh token is stored in an HttpOnly cookie — JavaScript cannot read it.
# This protects against XSS attacks stealing the long-lived token.
REFRESH_COOKIE_KEY = "pcil_refresh_token"
REFRESH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds


# True in production (HTTPS), False in local development (HTTP)
# True in production (HTTPS), False in development (HTTP localhost)

def set_refresh_cookie(response: Response, token: str):
    """Helper: set HttpOnly refresh token cookie on the response"""
    response.set_cookie(
        key=REFRESH_COOKIE_KEY,
        value=token,
        httponly=True,         # JavaScript cannot access — XSS protection
        secure=settings.is_production,  # Auto: True when APP_ENV=production, False in dev
        samesite="lax",        # CSRF protection
        max_age=REFRESH_COOKIE_MAX_AGE,
        path="/",              # Sent to all routes so refresh works everywhere
    )


def clear_refresh_cookie(response: Response):
    """Helper: clear the refresh token cookie on logout"""
    response.delete_cookie(key=REFRESH_COOKIE_KEY, path="/")


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new client account",
)
async def register(
    body: UserRegister,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new client account.

    1. Check email is not already registered
    2. Hash the password
    3. Create user record
    4. Return access token (and set refresh cookie)

    Frontend: Called from Signup.jsx on form submit.
    On success, store access_token in memory and redirect to /dashboard.
    """
    # Check if email already exists
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    # Create user
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        phone=body.phone,
        account_type=body.account_type,
    )
    db.add(user)
    await db.flush()  # Get the ID without committing yet

    # TODO: Send welcome email via SendGrid Celery task
    # from app.tasks.email_tasks import send_welcome_email
    # send_welcome_email.delay(user.email, user.full_name)

    await db.commit()

    # Generate tokens
    user_id = str(user.id)
    access_token = create_access_token(user_id)
    refresh_token = create_refresh_token(user_id)

    # Refresh token goes in HttpOnly cookie
    set_refresh_cookie(response, refresh_token)

    # Send the confirmation link. Deliberately after the tokens are issued,
    # so a mail failure can't block someone from finishing registration.
    await _issue_verification_email(user, request, db)

    logger.info(f"New client registered: {user.email}")
    return TokenResponse(access_token=access_token, email_verification_required=True)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Client login",
)
async def login(
    body: UserLogin,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate a client and return JWT tokens.

    Frontend: Called from Login.jsx on form submit.
    On success, store access_token in memory (not localStorage).
    """
    # Find user by email
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    # Verify credentials — same error message for both cases (security best practice)
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended. Please contact support.",
        )

    user_id = str(user.id)

    # If client has MFA enabled, issue a short-lived mfa_token instead of
    # a real access token — same two-step pattern as staff login.
    if user.mfa_enabled:
        mfa_token = create_mfa_token(user_id)
        logger.info(f"Client password OK, awaiting MFA: {user.email}")
        return TokenResponse(mfa_required=True, mfa_token=mfa_token)

    access_token = create_access_token(user_id, extra_claims={"kyc_status": user.kyc_status})
    refresh_token = create_refresh_token(user_id)

    set_refresh_cookie(response, refresh_token)

    logger.info(f"Client logged in: {user.email}")
    return TokenResponse(access_token=access_token, must_change_password=user.temp_password_active)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Get new access token using refresh cookie",
)
async def refresh_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Issue a new access token using the HttpOnly refresh token cookie.

    Frontend: Call this automatically when an API request returns 401.
    If this also fails (cookie expired), redirect to /login.
    """
    token = request.cookies.get(REFRESH_COOKIE_KEY)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token not found. Please log in again.",
        )

    user_id = decode_refresh_token(token)
    if not user_id:
        clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid or expired. Please log in again.",
        )

    # Verify user still exists and is active
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")

    # Issue new tokens (token rotation — improves security)
    new_access = create_access_token(str(user.id))
    new_refresh = create_refresh_token(str(user.id))
    set_refresh_cookie(response, new_refresh)

    return TokenResponse(access_token=new_access)


@router.post("/logout", response_model=MessageResponse, summary="Client logout")
async def logout(response: Response):
    """
    Log out the client by clearing the refresh token cookie.
    Frontend: Also clear the access token from memory.
    """
    clear_refresh_cookie(response)
    return MessageResponse(message="Logged out successfully.")


@router.get("/me", response_model=UserResponse, summary="Get current client profile")
async def get_me(current_user: User = Depends(get_current_user)):
    """
    Get the authenticated client's profile.
    Frontend: Call this on app load to restore user state from a valid access token.
    Replaces the sessionStorage restore logic in AuthContext.jsx.
    """
    return current_user


async def _issue_verification_email(user: User, request: Request, db: AsyncSession) -> None:
    """
    Creates a verification token and emails the link. Shared by registration
    and the resend endpoint so the two can't drift apart.
    """
    raw_token = secrets.token_urlsafe(32)
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=hash_password(raw_token),
        purpose="email_verification",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=VERIFY_TOKEN_HOURS),
    ))
    await db.commit()

    frontend = _cfg_frontend_url(request)
    send_email_verification(
        to=user.email, full_name=user.full_name,
        verify_url=f"{frontend}/verify-email?token={raw_token}",
        expires_hours=VERIFY_TOKEN_HOURS,
    )


@router.post(
    "/verify-email",
    response_model=MessageResponse,
    summary="Confirm an email address using the emailed token",
)
async def verify_email(
    body: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Marks the account's email as verified. Until now nothing ever set
    is_verified, so someone could register with a mistyped or fake address
    and never find out, and we'd have no way to reach them.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.purpose == "email_verification",
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    candidates = result.scalars().all()
    match = next((t for t in candidates if verify_password(body.token, t.token_hash)), None)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That confirmation link is invalid or has expired. Please request a new one.",
        )

    user_result = await db.execute(select(User).where(User.id == match.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")

    user.is_verified = True
    user.email_verified_at = now
    match.used_at = now
    await db.commit()

    logger.info(f"Email verified for {user.email}")
    return MessageResponse(message="Your email address has been confirmed.")


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    summary="Send a fresh email confirmation link",
)
async def resend_verification(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-sends the confirmation email, for links that expired or never arrived."""
    if current_user.is_verified:
        return MessageResponse(message="Your email address is already confirmed.")
    await _issue_verification_email(current_user, request, db)
    return MessageResponse(message="We've sent a new confirmation link to your email.")


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    summary="Request a password reset link",
)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Emails a single-use reset link. Previously a locked-out client had no way
    back in without phoning staff.

    Always returns the same success message whether or not the email exists.
    Revealing which addresses have accounts would let anyone enumerate your
    client list by trying addresses one at a time.
    """
    generic = MessageResponse(
        message="If that email is registered, a reset link has been sent to it."
    )

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return generic

    # The raw token goes in the email; only its hash is stored, so a leak of
    # this table can't be used to seize accounts.
    raw_token = secrets.token_urlsafe(32)
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=hash_password(raw_token),
        purpose="password_reset",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_MINUTES),
    ))
    await db.commit()

    frontend = _cfg_frontend_url(request)
    send_password_reset(
        to=user.email, full_name=user.full_name,
        reset_url=f"{frontend}/reset-password?token={raw_token}",
        expires_minutes=RESET_TOKEN_MINUTES,
    )

    logger.info(f"Password reset requested for {user.email}")
    return generic


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    summary="Set a new password using a reset link token",
)
async def reset_password_with_token(
    body: ResetPasswordWithToken,
    db: AsyncSession = Depends(get_db),
):
    """
    Completes the reset. The token is single-use and time-limited.

    Tokens are stored hashed, so we can't look one up directly: we check the
    small set of unexpired, unused tokens instead. That set is tiny in
    practice, and it's the same trade-off as password verification.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.purpose == "password_reset",
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    candidates = result.scalars().all()

    match = next((t for t in candidates if verify_password(body.token, t.token_hash)), None)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That reset link is invalid or has expired. Please request a new one.",
        )

    user_result = await db.execute(select(User).where(User.id == match.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")

    user.hashed_password = hash_password(body.new_password)
    user.temp_password_active = False
    match.used_at = now

    # Invalidate any other outstanding tokens for this user, so an older
    # email can't be replayed after a successful reset.
    for t in candidates:
        if t.user_id == user.id and t.used_at is None:
            t.used_at = now

    db.add(Notification(
        user_id=user.id,
        title="Password Changed",
        message="Your password was changed using a reset link. If this wasn't you, please contact us immediately.",
        notification_type="account",
    ))
    await db.commit()

    logger.info(f"Password reset completed for {user.email}")
    return MessageResponse(message="Your password has been changed. You can now sign in.")


@router.post(
    "/avatar",
    response_model=UserResponse,
    summary="Upload or replace my profile photo",
)
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Stores the client's profile photo through app.core.storage, so it lands
    on local disk in development and cloud storage in production. Replaces
    any existing avatar, deleting the old file so they don't accumulate.
    """
    allowed = ["image/jpeg", "image/jpg", "image/png", "image/webp"]
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Please upload a JPG, PNG or WEBP image.")

    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large. Maximum size is 2MB.")

    old_url = current_user.avatar_url

    filename = build_filename(f"{current_user.id}_avatar", file.filename, file.content_type)
    current_user.avatar_url = save_file(
        content=content, folder="avatars", filename=filename,
        content_type=file.content_type, request_base_url=str(request.base_url),
    )
    await db.commit()
    await db.refresh(current_user)

    # Clean up the previous file after the new one is safely saved, so a
    # failed upload never leaves the client with no avatar at all.
    if old_url and old_url != current_user.avatar_url:
        delete_file(old_url)

    logger.info(f"Avatar updated for {current_user.email}")
    return current_user


@router.delete(
    "/avatar",
    response_model=UserResponse,
    summary="Remove my profile photo",
)
async def remove_avatar(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_url = current_user.avatar_url
    current_user.avatar_url = None
    await db.commit()
    await db.refresh(current_user)
    if old_url:
        delete_file(old_url)
    return current_user


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Change password — also clears the forced temp-password flag",
)
async def change_password(
    body: ChangePassword,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Change the client's password. Used for:
      1. The forced change after a staff-created temp password (NEW in v11)
         — frontend redirects here whenever login returns must_change_password=True.
      2. Any future voluntary "change my password" feature.

    Requires the current password (the temp one, in case 1) for verification
    — same security bar as a normal password change, temp password or not.
    """
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    current_user.hashed_password = hash_password(body.new_password)
    current_user.temp_password_active = False
    await db.commit()

    logger.info(f"Password changed for {current_user.email}")
    return MessageResponse(message="Password changed successfully.")


# ── Client MFA (NEW in v11 — optional, client opt-in) ─────────────────────────

@router.post(
    "/mfa/setup",
    response_model=MfaSetupResponse,
    summary="Client: begin MFA enrollment — generates secret + QR code",
)
async def client_mfa_setup(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates a fresh TOTP secret for the client and returns a QR code to
    scan with any authenticator app. mfa_enabled stays False until they
    confirm with /mfa/verify. Calling this again before verifying replaces
    the pending secret (lets the client restart if they lose the QR code).
    """
    secret = generate_mfa_secret()
    current_user.mfa_secret = secret
    await db.commit()

    uri = get_provisioning_uri(secret, current_user.email)
    qr_code = generate_qr_code_base64(uri)

    logger.info(f"Client MFA setup started for {current_user.email}")
    return MfaSetupResponse(secret=secret, qr_code=qr_code, otpauth_uri=uri)


@router.post(
    "/mfa/verify",
    response_model=MessageResponse,
    summary="Client: confirm MFA enrollment with a code from the authenticator app",
)
async def client_mfa_verify(
    body: MfaVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Confirms the client scanned the QR code correctly by verifying their
    first TOTP code. Only after this succeeds does mfa_enabled flip True
    and future logins require the second step.
    """
    if not current_user.mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No MFA setup in progress. Call /auth/mfa/setup first.",
        )
    if not verify_totp_code(current_user.mfa_secret, body.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication code. Please try again.",
        )

    current_user.mfa_enabled = True
    await db.commit()

    logger.info(f"Client MFA enabled for {current_user.email}")
    return MessageResponse(message="Two-factor authentication has been enabled on your account.")


@router.post(
    "/mfa/login-verify",
    response_model=TokenResponse,
    summary="Client: second step of login — verify TOTP code",
)
async def client_mfa_login_verify(
    body: MfaLoginVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchanges a valid mfa_token + correct TOTP code for a real access token.
    Same pattern as /auth/admin/mfa/login-verify but for client accounts.
    """
    user_id = decode_mfa_token(body.mfa_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA session expired. Please log in again.",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found.")

    if not verify_totp_code(user.mfa_secret, body.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication code.")

    access_token = create_access_token(str(user.id), extra_claims={"kyc_status": user.kyc_status})
    refresh_token = create_refresh_token(str(user.id))
    set_refresh_cookie(response, refresh_token)

    logger.info(f"Client MFA verified, logged in: {user.email}")
    return TokenResponse(access_token=access_token, must_change_password=user.temp_password_active)


@router.post(
    "/mfa/disable",
    response_model=MessageResponse,
    summary="Client: disable MFA — requires a valid code as proof of possession",
)
async def client_mfa_disable(
    body: MfaDisableRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Lets a client turn off MFA (optional for clients, unlike mandatory for
    staff). Requires a valid TOTP code before disabling — so a stolen session
    token alone can't strip someone's MFA protection.
    """
    if not current_user.mfa_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled on this account.")

    if not verify_totp_code(current_user.mfa_secret, body.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication code.")

    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    await db.commit()

    logger.info(f"Client MFA disabled for {current_user.email}")
    return MessageResponse(message="Two-factor authentication has been disabled.")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/admin/login",
    response_model=AdminLoginResponse,
    summary="Admin login — two-step if MFA is enabled (NEW in v11)",
)
async def admin_login(
    body: AdminLogin,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate an admin user. Password check is unchanged; what happens
    after differs based on MFA state (NEW in v11):

      - MFA enabled  -> no access_token yet. Returns mfa_required=True +
                         a short-lived mfa_token. Frontend must call
                         /auth/admin/mfa/login-verify with a 6-digit code
                         to get the real access_token.
      - MFA not set up -> logs in normally (access_token issued, refresh
                         cookie set) but flags mfa_setup_required=True so
                         the frontend can push the admin to enroll — MFA is
                         mandatory for staff, but enrollment itself requires
                         being logged in, so this isn't a hard lockout.
    """
    result = await db.execute(
        select(AdminUser).where(AdminUser.email == body.email)
    )
    admin = result.scalar_one_or_none()

    if not admin or not verify_password(body.password, admin.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials.",
        )

    if not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This admin account has been deactivated.",
        )

    admin_id = str(admin.id)

    if admin.mfa_enabled:
        mfa_token = create_mfa_token(admin_id)
        logger.info(f"Admin password OK, awaiting MFA: {admin.email}")
        return AdminLoginResponse(mfa_required=True, mfa_token=mfa_token)

    access_token = create_access_token(
        admin_id,
        extra_claims={
            "role": admin.role,
            "permissions": admin.permissions or [],
            "is_admin": True,
        }
    )
    refresh_token = create_refresh_token(admin_id)
    set_refresh_cookie(response, refresh_token)

    logger.info(f"Admin logged in: {admin.email} [{admin.role}] — MFA not yet enrolled")
    return AdminLoginResponse(
        access_token=access_token, mfa_setup_required=True,
        must_change_password=admin.temp_password_active,
    )


@router.post(
    "/admin/mfa/login-verify",
    response_model=AdminLoginResponse,
    summary="Second step of admin login — verify TOTP code (NEW in v11)",
)
async def admin_mfa_login_verify(
    body: MfaLoginVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchanges a valid mfa_token + correct TOTP code for a real access token.
    No auth dependency here — the admin doesn't have a real token yet; the
    mfa_token itself (short-lived, single-purpose) is the proof they got
    the password right in step one.
    """
    admin_id = decode_mfa_token(body.mfa_token)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA session expired. Please log in again.",
        )

    result = await db.execute(select(AdminUser).where(AdminUser.id == admin_id))
    admin = result.scalar_one_or_none()
    if not admin or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin account not found.")

    if not verify_totp_code(admin.mfa_secret, body.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication code.")

    access_token = create_access_token(
        str(admin.id),
        extra_claims={
            "role": admin.role,
            "permissions": admin.permissions or [],
            "is_admin": True,
        }
    )
    refresh_token = create_refresh_token(str(admin.id))
    set_refresh_cookie(response, refresh_token)

    logger.info(f"Admin MFA verified, logged in: {admin.email} [{admin.role}]")
    return AdminLoginResponse(
        access_token=access_token,
        must_change_password=admin.temp_password_active,
    )


@router.post(
    "/admin/mfa/setup",
    response_model=MfaSetupResponse,
    summary="Begin MFA enrollment — generates a new secret + QR code (NEW in v11)",
)
async def admin_mfa_setup(
    current_admin: AdminUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates a fresh TOTP secret and stores it (mfa_enabled stays False
    until /mfa/verify confirms the admin actually scanned it correctly).
    Calling this again before verifying replaces the pending secret —
    lets the admin restart if they lose the QR code mid-setup.
    """
    secret = generate_mfa_secret()
    current_admin.mfa_secret = secret
    await db.commit()

    uri = get_provisioning_uri(secret, current_admin.email)
    qr_code = generate_qr_code_base64(uri)

    logger.info(f"MFA setup started for {current_admin.email}")
    return MfaSetupResponse(secret=secret, qr_code=qr_code, otpauth_uri=uri)


@router.post(
    "/admin/mfa/verify",
    response_model=MessageResponse,
    summary="Confirm MFA enrollment with a code from the authenticator app (NEW in v11)",
)
async def admin_mfa_verify(
    body: MfaVerifyRequest,
    current_admin: AdminUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Confirms the admin actually set up their authenticator app correctly.
    Only after this succeeds does mfa_enabled flip True and future logins
    require the second step.
    """
    if not current_admin.mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No MFA setup in progress. Call /auth/admin/mfa/setup first.",
        )

    if not verify_totp_code(current_admin.mfa_secret, body.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication code.")

    current_admin.mfa_enabled = True
    await db.commit()

    logger.info(f"MFA enabled for {current_admin.email}")
    return MessageResponse(message="MFA has been enabled on your account.")


@router.post(
    "/admin/change-password",
    response_model=MessageResponse,
    summary="Staff: change my own password (also clears any forced-change flag)",
)
async def admin_change_password(
    body: AdminChangePassword,
    current_admin: AdminUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Lets a staff member change their own password. Clients already had this;
    staff had no way to do it at all, so a temp password issued by IT could
    never be replaced by the person using it.

    Requires the current password, so a hijacked session can't silently lock
    the real owner out by changing it.
    """
    if not verify_password(body.current_password, current_admin.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    current_admin.hashed_password = hash_password(body.new_password)
    current_admin.temp_password_active = False

    db.add(AuditLog(
        action="Staff Password Changed", target=current_admin.full_name,
        target_id=str(current_admin.id), action_type="admin",
        performed_by=current_admin.id, performed_by_name=current_admin.full_name,
        details={"self_service": True},
    ))
    await db.commit()

    logger.info(f"Staff password changed by {current_admin.email}")
    return MessageResponse(message="Password changed successfully.")


@router.get(
    "/admin/me",
    response_model=AdminUserResponse,
    summary="Get current admin profile",
)
async def get_admin_me(current_admin: AdminUser = Depends(get_current_admin)):
    """Get the authenticated admin's profile and permissions."""
    return current_admin


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — CLIENTS ENDPOINT
# Lists ALL registered users (with or without KYC submission)
# so the admin panel shows every client, not just KYC submitters.
# ─────────────────────────────────────────────────────────────────────────────

from app.models.models import KycSubmission
from sqlalchemy.orm import selectinload

admin_router = APIRouter()

@admin_router.get(
    "",
    summary="Admin: List all registered clients with KYC status",
)
async def list_clients(
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),  # any logged-in staff can VIEW — actions stay permission-gated
):
    """
    Returns every registered client account joined with their KYC record (if any).
    Admin panel → Clients section and KYC Management section both use this.
    """
    # Load all users with their KYC submission in one query
    result = await db.execute(
        select(User)
        .options(selectinload(User.kyc_submission))
        .order_by(User.created_at.desc())
    )
    users = result.scalars().all()

    clients = []
    for u in users:
        kyc = u.kyc_submission
        clients.append({
            # User fields
            "id":           str(u.id),
            "user_id":      str(u.id),
            "full_name":    u.full_name,
            "email":        u.email,
            "phone":        u.phone or "",
            "account_type": u.account_type,
            "is_active":    u.is_active,
            "created_at":   u.created_at,
            # KYC fields — None if not submitted yet
            "kyc_id":       str(kyc.id) if kyc else None,
            "status":       kyc.status if kyc else "not_submitted",
            "kyc_status":   kyc.status if kyc else "not_submitted",
            "denial_reason": kyc.denial_reason if kyc else None,
            "d365_synced":  kyc.d365_synced if kyc else False,
            "submitted_at": kyc.submitted_at if kyc else None,
            "reviewed_at":  kyc.reviewed_at if kyc else None,
        })
    return clients


@admin_router.patch(
    "/{user_id}/reset-password",
    response_model=PasswordResetResponse,
    summary="Staff: reset a client's password",
)
async def reset_client_password(
    user_id: UUID,
    body: AdminResetPassword,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_clients")),
):
    """
    Resets a client's password when they've forgotten it. Gated by
    can_manage_clients, so it isn't limited to a single role — the same staff
    who can create client accounts can also help them back in.

    Generates a secure temp password unless one is supplied, and flags the
    account so the client must set their own at next login. The temp password
    is returned once and hashed immediately, so it must be passed on now.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client account not found.")

    temp_password = body.new_password or generate_temp_password()
    client.hashed_password = hash_password(temp_password)
    client.temp_password_active = True

    db.add(AuditLog(
        action="Client Password Reset", target=client.full_name, target_id=str(user_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"email": client.email},
    ))
    db.add(Notification(
        user_id=client.id,
        title="Your Password Was Reset",
        message="A member of our team reset your password at your request. You'll be asked to set a new one when you next sign in.",
        notification_type="account",
    ))
    await db.commit()
    await db.refresh(client)

    send_temp_password(to=client.email, full_name=client.full_name,
                       temp_password=temp_password, is_staff=False)

    logger.info(f"Password reset for client {client.email} by staff {admin.email}")
    return PasswordResetResponse(
        id=client.id, full_name=client.full_name, email=client.email,
        temp_password=temp_password,
    )


@admin_router.post(
    "",
    response_model=ClientCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Staff: Create a client account with a temp password — NEW in v11",
)
async def create_client(
    body: AdminCreateClient,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_clients")),
):
    """
    Staff (any role with can_manage_clients — not limited to IT Admin)
    creates a client account on the client's behalf. A temp password is
    generated (or the caller can supply one manually via `temp_password`)
    and returned in this response ONCE — it's hashed immediately and can
    never be retrieved again, so it must be shared with the client now
    (manually, or via whatever notification flow gets built later).

    Client is forced to change it on first login: temp_password_active=True
    means the client's next POST /auth/login returns must_change_password=True,
    and the frontend should redirect straight to the change-password screen
    before showing anything else.
    """
    if body.onboarding_type not in ("new", "existing"):
        raise HTTPException(status_code=400, detail="onboarding_type must be 'new' or 'existing'.")

    if body.onboarding_type == "existing":
        if not body.product_id or not body.investment_amount:
            raise HTTPException(
                status_code=400,
                detail="product_id and investment_amount are required when onboarding_type is 'existing'.",
            )
        product_check = await db.execute(select(Product).where(Product.id == body.product_id))
        if not product_check.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Product not found.")

    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A client account with this email already exists.",
        )

    temp_password = body.temp_password or generate_temp_password()

    new_user = User(
        email=body.email,
        hashed_password=hash_password(temp_password),
        full_name=body.full_name,
        phone=body.phone,
        account_type=body.account_type,
        is_active=True,
        temp_password_active=True,
    )
    db.add(new_user)
    await db.flush()

    subscription_created = False
    if body.onboarding_type == "existing":
        # ── Already onboarded offline: KYC done on paper, payment already
        # made, already in D365. Pre-approve KYC (as a real, auditable
        # record — not just a flag) and create the matching active
        # subscription directly, so the client's first login shows their
        # real portfolio instead of an empty "please submit KYC" state.
        new_user.kyc_status = "approved"

        stub_kyc = KycSubmission(
            user_id=new_user.id,
            status="approved",
            reviewed_at=datetime.now(timezone.utc),
            reviewed_by=f"Pre-onboarded by {admin.full_name}",
            extra_data={
                "account_type": body.account_type.capitalize(),
                "onboarding_note": "Client was onboarded offline (KYC completed on paper, payment already received, already recorded in D365) prior to portal account creation.",
            },
        )
        db.add(stub_kyc)

        new_subscription = Subscription(
            user_id=new_user.id,
            product_id=body.product_id,
            amount=body.investment_amount,
            currency=body.investment_currency,
            reference=generate_subscription_reference(),
            status="active",
            d365_record_id=body.d365_reference,
            d365_synced=bool(body.d365_reference),
            activated_at=body.investment_start_date or datetime.now(timezone.utc),
            maturity_date=body.investment_maturity_date,
        )
        db.add(new_subscription)
        subscription_created = True

    audit = AuditLog(
        action="Client Account Created by Staff" if body.onboarding_type == "new" else "Existing Client Onboarded to Portal",
        target=body.full_name,
        target_id=str(new_user.id),
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={
            "email": body.email,
            "onboarding_type": body.onboarding_type,
            **({"investment_amount": body.investment_amount, "product_id": str(body.product_id)} if subscription_created else {}),
        },
    )
    db.add(audit)
    await db.commit()
    await db.refresh(new_user)

    # Fire workflow trigger (e.g. a follow-up KYC-nudge or welcome-call task)
    # — still relevant even for "existing" onboarding (e.g. a welcome-call
    # checklist), but the KYC/subscription review triggers are correctly
    # NOT fired here since there's nothing to review — it already happened.
    try:
        wf_result = await db.execute(
            select(Workflow).where(Workflow.trigger == "client_account_created", Workflow.is_active == True)
        )
        workflow = wf_result.scalars().first()
        if workflow:
            db.add(WorkflowInstance(
                workflow_id=workflow.id, record_type="client", record_id=new_user.id,
                client_id=new_user.id, client_name=new_user.full_name,
                current_step=1, status="pending", step_history=[],
            ))
            await db.commit()
    except Exception as e:
        logger.warning(f"Workflow trigger failed silently (client_account_created): {e}")

    logger.info(f"Client account created for {body.email} by staff {admin.email} (onboarding_type={body.onboarding_type})")
    return ClientCreatedResponse(
        id=new_user.id,
        full_name=new_user.full_name,
        email=new_user.email,
        temp_password=temp_password,
        subscription_created=subscription_created,
    )
