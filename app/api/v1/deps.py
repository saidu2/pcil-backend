# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/deps.py
#
# FastAPI dependency functions — injected into endpoints via Depends().
# These handle authentication and authorisation for every protected route.
#
# Usage in any endpoint:
#   from app.api.v1.deps import get_current_user, get_current_admin, require_super_admin
#
#   @router.get("/something")
#   async def my_endpoint(user: User = Depends(get_current_user)):
#       ...
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.models import User, AdminUser, StaffRole

# ── OAuth2 scheme ─────────────────────────────────────────────────────────────
# Extracts the Bearer token from the Authorization header.
# tokenUrl points to the login endpoint (used by auto-generated API docs).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
admin_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/admin/login")


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT DEPENDENCIES
# ─────────────────────────────────────────────────────────────────────────────

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Dependency: Get the currently authenticated client.
    Raises 401 if token is missing, invalid, or expired.

    Use on any protected client endpoint.
    """
    user_id = decode_access_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended.",
        )

    return user


async def get_current_verified_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Dependency: Like get_current_user but also requires KYC approval.
    Use on endpoints that require a verified (KYC-approved) client.
    e.g. subscribing to a product.
    """
    if current_user.kyc_status != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your KYC must be approved before accessing this feature.",
        )
    return current_user


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN DEPENDENCIES
# ─────────────────────────────────────────────────────────────────────────────

async def get_current_admin(
    token: str = Depends(admin_oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    """
    Dependency: Get the currently authenticated admin.
    Raises 401 if token is invalid or not an admin token.

    Use on all admin endpoints.
    """
    user_id = decode_access_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired admin token.",
        )

    result = await db.execute(
        select(AdminUser)
        .options(selectinload(AdminUser.staff_role))
        .where(AdminUser.id == user_id)
    )
    admin = result.scalar_one_or_none()

    if not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin account not found.",
        )

    if not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This admin account has been deactivated.",
        )

    return admin


async def require_super_admin(
    current_admin: AdminUser = Depends(get_current_admin),
) -> AdminUser:
    """
    Dependency: Require super_admin role.
    Use on endpoints that ONLY the super admin (Saidu Safiyanu) can access:
      - Creating/deleting junior admins
      - System settings changes
      - System alert banner
      - Fee configuration
    """
    if current_admin.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires Super Admin access.",
        )
    return current_admin


# Maps the legacy section names used throughout the existing endpoints onto
# StaffRole permission flags. This is what lets every `require_permission("kyc")`
# call keep working unchanged while the actual authority now comes from the
# staff member's assigned role.
#
# A value of None means "any logged-in staff member may access this" — used
# for sections that are read-only overviews rather than real responsibilities.
LEGACY_SECTION_TO_FLAG = {
    "dashboard":     None,                          # overview only, no real authority
    "products":      "can_manage_products",
    "clients":       "can_manage_clients",
    "kyc":           "can_approve_kyc",
    "subscriptions": "can_manage_subscriptions",
    "redemptions":   "can_manage_redemptions",
    "certificates":  "can_manage_certificates",
    "maturity":      "can_manage_maturity",
    "payments":      "can_manage_payments",
    "fees":          "can_manage_fees",
    "notifications": "can_manage_clients",          # sending client notifications
    "announcements": "can_manage_system_settings",  # platform-wide publishing
    "reports":       "can_view_reports",
    "audit":         "can_view_audit_log",
    "settings":      "can_manage_system_settings",
    "alert":         "can_manage_system_settings",  # platform-wide banner
    "nav":           "can_manage_nav",
    "admin_users":   "can_manage_staff_users",
}


def require_permission(section: str):
    """
    Factory: returns a dependency enforcing access to a named section.

    The permission now comes ENTIRELY from the staff member's assigned
    StaffRole. The old per-account `permissions` JSON list is no longer
    consulted at all — having two parallel systems meant every staff member
    had to be configured twice, and it was easy to grant one without the
    other. StaffRole is now the single source of truth.

    Existing endpoints call this unchanged (e.g. require_permission("kyc")),
    so the section names stay as they were; only where the answer comes from
    has changed. See LEGACY_SECTION_TO_FLAG above for the mapping.

    Super admin always passes. Staff with no role assigned are denied, with
    a message telling them who to ask — deliberately not falling back to the
    old list, which would recreate the very confusion this removes.
    """
    async def check_permission(
        current_admin: AdminUser = Depends(get_current_admin),
        db: AsyncSession = Depends(get_db),
    ) -> AdminUser:
        if current_admin.role == "super_admin":
            return current_admin

        flag = LEGACY_SECTION_TO_FLAG.get(section, section)

        # Sections mapped to None are open to any logged-in staff member
        if flag is None:
            return current_admin

        if not current_admin.staff_role_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have a staff role assigned yet. Please contact your IT Admin.",
            )

        result = await db.execute(
            select(StaffRole).where(StaffRole.id == current_admin.staff_role_id)
        )
        role = result.scalar_one_or_none()

        if not role or not role.is_active or not getattr(role, flag, False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your role doesn't have permission to access '{section}'.",
            )
        return current_admin

    return check_permission


def require_staff_permission(flag: str):
    """
    Factory: Returns a dependency that checks a StaffRole permission flag
    (NEW in v11) — e.g. "can_manage_clients", "can_approve_kyc". Distinct
    from require_permission() above, which checks the legacy junior_admin
    permissions JSON list.

    Super admin always passes. Any other staff member passes only if their
    assigned staff_role has this flag set True — regardless of department
    or role name, so this is intentionally not limited to IT Admin: any
    role you configure with e.g. can_manage_clients=True can do it.

    Queries StaffRole directly by admin.staff_role_id rather than accessing
    current_admin.staff_role (the relationship) — avoids a lazy-load on the
    async session, which would raise MissingGreenlet if unloaded.

    Usage:
        @router.post("/clients")
        async def create_client(admin = Depends(require_staff_permission("can_manage_clients"))):
            ...
    """
    async def check_staff_permission(
        current_admin: AdminUser = Depends(get_current_admin),
        db: AsyncSession = Depends(get_db),
    ) -> AdminUser:
        if current_admin.role == "super_admin":
            return current_admin

        if not current_admin.staff_role_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have a staff role assigned. Contact your IT Admin.",
            )

        result = await db.execute(
            select(StaffRole).where(StaffRole.id == current_admin.staff_role_id)
        )
        role = result.scalar_one_or_none()

        if not role or not role.is_active or not getattr(role, flag, False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to perform this action.",
            )
        return current_admin

    return check_staff_permission


async def get_current_user_password_ok(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Like get_current_user, but blocks access if the client still has a temp
    password active (NEW in v11) — for endpoints that should be unreachable
    until the forced password change is complete (e.g. subscribing).

    NOT swapped in anywhere automatically yet — this only takes effect on
    endpoints that explicitly use Depends(get_current_user_password_ok)
    instead of Depends(get_current_user). Decide which endpoints should be
    gated and swap them in deliberately, rather than blocking everything
    (e.g. /auth/me and /auth/change-password must stay reachable regardless).
    """
    if current_user.temp_password_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must change your temporary password before continuing.",
        )
    return current_user


async def get_current_verified_email_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Like get_current_user, but blocks access until the client has confirmed
    their email address (NEW). Registration issues a real access token
    immediately (see auth.py register()), so a client is technically
    "logged in" the moment they sign up, before ever clicking the
    confirmation link — this dependency is what turns that into an
    actual gate.

    Use on dashboard/portfolio/subscription endpoints — anywhere real
    access should wait for a confirmed email address.

    Deliberately NOT applied to /auth/me, /auth/logout, /auth/verify-email,
    or /auth/resend-verification — those must stay reachable regardless of
    verification status, or a client can never get past this gate at all.
    """
    if not current_user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please confirm your email address before continuing.",
        )
    return current_user
