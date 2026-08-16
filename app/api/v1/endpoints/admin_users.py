# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/admin_users.py
#
# Admin user management — Super Admin only.
# Admin panel → Admin Users section (Section 17).
#
# SUPER ADMIN only:
#   GET    /api/v1/admin/users          — list all admin users
#   POST   /api/v1/admin/users          — create junior admin
#   PATCH  /api/v1/admin/users/{id}     — update permissions / deactivate
#   DELETE /api/v1/admin/users/{id}     — delete junior admin
#
# Only super_admin can access these endpoints.
# Super admin cannot be deleted or demoted.
# ─────────────────────────────────────────────────────────────────────────────

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.core.security import hash_password, generate_temp_password
from app.core.mailer import send_temp_password
from app.models.models import AdminUser, AuditLog, StaffRole
from app.schemas.schemas import (
    AdminUserCreate, AdminUserUpdate, AdminUserResponse, MessageResponse,
    AssignStaffRole, AdminResetPassword, PasswordResetResponse,
)
from app.api.v1.deps import require_super_admin

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "",
    response_model=list[AdminUserResponse],
    summary="List all admin users — Super Admin only",
)
async def list_admin_users(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """List all admin accounts. Admin panel → Admin Users section."""
    result = await db.execute(
        select(AdminUser)
        .options(selectinload(AdminUser.staff_role))
        .order_by(AdminUser.created_at.desc())
    )
    return result.scalars().all()


@router.post(
    "",
    response_model=AdminUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a junior admin — Super Admin only",
)
async def create_admin_user(
    body: AdminUserCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """
    Create a new junior admin account.

    permissions is a list of section names the junior admin can access.
    Example: ["dashboard", "kyc", "subscriptions", "notifications", "reports"]

    Available permission sections:
      dashboard, products, clients, kyc, subscriptions, redemptions,
      certificates, maturity, payments, fees, notifications, announcements,
      reports, audit, settings, alert, admin_users

    staff_role_id (NEW in v11, optional) — assigns a granular StaffRole at
    creation time instead of a separate PATCH .../role call afterwards.
    """
    # Check email not already in use
    existing = await db.execute(
        select(AdminUser).where(AdminUser.email == body.email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="An admin account with this email already exists."
        )

    if body.staff_role_id is not None:
        role_result = await db.execute(
            select(StaffRole).where(StaffRole.id == body.staff_role_id)
        )
        staff_role = role_result.scalar_one_or_none()
        if not staff_role:
            raise HTTPException(status_code=404, detail="Staff role not found.")
        if not staff_role.is_active:
            raise HTTPException(status_code=400, detail="Staff role is inactive.")

    new_admin = AdminUser(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role="junior_admin",
        permissions=body.permissions,
        department=body.department,
        staff_role_id=body.staff_role_id,
        is_active=True,
        created_by=admin.id,
    )
    db.add(new_admin)

    audit = AuditLog(
        action="Admin User Created",
        target=body.full_name,
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={
            "email": body.email,
            "permissions": body.permissions,
            "department": body.department,
            "staff_role_id": str(body.staff_role_id) if body.staff_role_id else None,
        },
    )
    db.add(audit)
    await db.commit()

    # Re-fetch with staff_role eager-loaded (same MissingGreenlet guard as
    # the update/assign-role endpoints — see AdminUserResponse docstring).
    result = await db.execute(
        select(AdminUser)
        .options(selectinload(AdminUser.staff_role))
        .where(AdminUser.id == new_admin.id)
    )
    new_admin = result.scalar_one()

    logger.info(f"Junior admin created: {body.email} by {admin.email}")
    return new_admin


@router.patch(
    "/{admin_id}",
    response_model=AdminUserResponse,
    summary="Update admin user — Super Admin only",
)
async def update_admin_user(
    admin_id: UUID,
    body: AdminUserUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """
    Update a junior admin's name, permissions, or active status.
    Cannot modify the super admin account.
    """
    result = await db.execute(select(AdminUser).where(AdminUser.id == admin_id))
    target_admin = result.scalar_one_or_none()

    if not target_admin:
        raise HTTPException(status_code=404, detail="Admin user not found.")

    # Protect the super admin account from being modified
    if target_admin.role == "super_admin":
        raise HTTPException(
            status_code=403,
            detail="The Super Admin account cannot be modified through this endpoint."
        )

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(target_admin, field, value)

    action = "Admin User Updated"
    if "is_active" in update_data:
        action = f"Admin User {'Activated' if update_data['is_active'] else 'Deactivated'}"

    audit = AuditLog(
        action=action,
        target=target_admin.full_name,
        target_id=str(admin_id),
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details=update_data,
    )
    db.add(audit)
    await db.commit()

    # Re-fetch with staff_role eager-loaded — AdminUserResponse reads that
    # relationship, and async SQLAlchemy can't lazy-load it during
    # serialization (would raise MissingGreenlet if staff_role_id is set).
    result = await db.execute(
        select(AdminUser)
        .options(selectinload(AdminUser.staff_role))
        .where(AdminUser.id == admin_id)
    )
    return result.scalar_one()


@router.patch(
    "/{admin_id}/role",
    response_model=AdminUserResponse,
    summary="Assign or unassign a staff role — Super Admin only",
)
async def assign_staff_role(
    admin_id: UUID,
    body: AssignStaffRole,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """
    Assign a staff_role to an admin user, or unassign by passing null.
    Separate from update_admin_user so role assignment is its own auditable
    action, and so AdminUserUpdate doesn't need to carry a staff_role_id
    field that would bypass the "role must exist" check below.
    """
    result = await db.execute(select(AdminUser).where(AdminUser.id == admin_id))
    target_admin = result.scalar_one_or_none()
    if not target_admin:
        raise HTTPException(status_code=404, detail="Admin user not found.")

    if target_admin.role == "super_admin":
        raise HTTPException(
            status_code=403,
            detail="The Super Admin account cannot be modified through this endpoint."
        )

    role_name = None
    if body.staff_role_id is not None:
        role_result = await db.execute(
            select(StaffRole).where(StaffRole.id == body.staff_role_id)
        )
        staff_role = role_result.scalar_one_or_none()
        if not staff_role:
            raise HTTPException(status_code=404, detail="Staff role not found.")
        if not staff_role.is_active:
            raise HTTPException(status_code=400, detail="Staff role is inactive.")
        role_name = staff_role.name

    target_admin.staff_role_id = body.staff_role_id

    audit = AuditLog(
        action="Admin User Role Assigned" if role_name else "Admin User Role Unassigned",
        target=target_admin.full_name,
        target_id=str(admin_id),
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={"staff_role_id": str(body.staff_role_id) if body.staff_role_id else None,
                 "staff_role_name": role_name},
    )
    db.add(audit)
    await db.commit()

    result = await db.execute(
        select(AdminUser)
        .options(selectinload(AdminUser.staff_role))
        .where(AdminUser.id == admin_id)
    )
    return result.scalar_one()


@router.patch(
    "/{admin_id}/reset-password",
    response_model=PasswordResetResponse,
    summary="Reset a staff member's password — Super Admin only",
)
async def reset_admin_password(
    admin_id: UUID,
    body: AdminResetPassword,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """
    Resets a staff member's password when they've forgotten or lost it.
    Generates a secure temp password unless one is supplied, and flags the
    account so they must set their own at next login.

    The temp password is returned once and hashed immediately, so it cannot
    be retrieved again and must be passed to them now.
    """
    result = await db.execute(select(AdminUser).where(AdminUser.id == admin_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Staff account not found.")
    if target.id == admin.id:
        raise HTTPException(
            status_code=400,
            detail="Use Change Password to update your own password.",
        )

    temp_password = body.new_password or generate_temp_password()
    target.hashed_password = hash_password(temp_password)
    target.temp_password_active = True

    db.add(AuditLog(
        action="Staff Password Reset", target=target.full_name, target_id=str(admin_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"reset_by_super_admin": True},
    ))
    await db.commit()
    await db.refresh(target)

    send_temp_password(to=target.email, full_name=target.full_name,
                       temp_password=temp_password, is_staff=True)

    logger.info(f"Password reset for staff {target.email} by {admin.email}")
    return PasswordResetResponse(
        id=target.id, full_name=target.full_name, email=target.email,
        temp_password=temp_password,
    )


@router.delete(
    "/{admin_id}",
    response_model=MessageResponse,
    summary="Delete a junior admin — Super Admin only",
)
async def delete_admin_user(
    admin_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """
    Permanently delete a junior admin account.
    Cannot delete the super admin.
    Consider deactivating instead of deleting to preserve audit history.
    """
    result = await db.execute(select(AdminUser).where(AdminUser.id == admin_id))
    target_admin = result.scalar_one_or_none()

    if not target_admin:
        raise HTTPException(status_code=404, detail="Admin user not found.")

    if target_admin.role == "super_admin":
        raise HTTPException(
            status_code=403,
            detail="The Super Admin account cannot be deleted."
        )

    name = target_admin.full_name
    await db.delete(target_admin)

    audit = AuditLog(
        action="Admin User Deleted",
        target=name,
        target_id=str(admin_id),
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
    )
    db.add(audit)
    await db.commit()

    logger.info(f"Admin user {name} deleted by {admin.email}")
    return MessageResponse(message=f"Admin user '{name}' has been deleted.")
