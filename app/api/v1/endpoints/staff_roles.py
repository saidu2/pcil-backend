# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/staff_roles.py
#
# Staff role management (NEW in v11) — Super Admin only for now.
# Admin panel → Roles & Permissions section.
#
# These are separate from the legacy AdminUser.role (super_admin/junior_admin)
# and AdminUser.permissions JSON, which are untouched. StaffRole is the new,
# granular permission system that v11 features (workflow engine, valuation
# entry, etc.) are built against going forward.
#
# SUPER ADMIN only (same protection level as admin_users.py for now — revisit
# if role-management itself should become its own permission flag):
#   GET    /api/v1/admin/staff-roles          — list all staff roles
#   POST   /api/v1/admin/staff-roles          — create a staff role
#   GET    /api/v1/admin/staff-roles/{id}     — get one staff role
#   PATCH  /api/v1/admin/staff-roles/{id}     — update a staff role
#   DELETE /api/v1/admin/staff-roles/{id}     — delete a staff role
#
# To assign a role to a staff member, see:
#   PATCH /api/v1/admin/users/{admin_id}/role   (in admin_users.py)
# ─────────────────────────────────────────────────────────────────────────────

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import StaffRole, AdminUser, AuditLog
from app.schemas.schemas import StaffRoleCreate, StaffRoleUpdate, StaffRoleResponse, MessageResponse
from app.api.v1.deps import require_super_admin

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "",
    response_model=list[StaffRoleResponse],
    summary="List all staff roles — Super Admin only",
)
async def list_staff_roles(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """List all staff roles. Admin panel → Roles & Permissions section."""
    result = await db.execute(select(StaffRole).order_by(StaffRole.name))
    return result.scalars().all()


@router.post(
    "",
    response_model=StaffRoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a staff role — Super Admin only",
)
async def create_staff_role(
    body: StaffRoleCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_super_admin),
):
    """
    Create a new staff role with a set of permission flags.
    e.g. name="Compliance Officer", department="Compliance",
         can_approve_kyc=True, can_view_audit_log=True
    """
    existing = await db.execute(select(StaffRole).where(StaffRole.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="A staff role with this name already exists."
        )

    new_role = StaffRole(**body.model_dump())
    db.add(new_role)

    audit = AuditLog(
        action="Staff Role Created",
        target=body.name,
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details=body.model_dump(),
    )
    db.add(audit)
    await db.commit()
    await db.refresh(new_role)

    logger.info(f"Staff role created: {body.name} by {admin.email}")
    return new_role


@router.get(
    "/{role_id}",
    response_model=StaffRoleResponse,
    summary="Get a single staff role — Super Admin only",
)
async def get_staff_role(
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    result = await db.execute(select(StaffRole).where(StaffRole.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Staff role not found.")
    return role


@router.patch(
    "/{role_id}",
    response_model=StaffRoleResponse,
    summary="Update a staff role — Super Admin only",
)
async def update_staff_role(
    role_id: UUID,
    body: StaffRoleUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_super_admin),
):
    """
    Update a staff role's name, department, description, permission flags,
    or active status. Changes apply immediately to every staff member
    assigned this role.
    """
    result = await db.execute(select(StaffRole).where(StaffRole.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Staff role not found.")

    update_data = body.model_dump(exclude_unset=True)

    if "name" in update_data and update_data["name"] != role.name:
        dupe = await db.execute(
            select(StaffRole).where(StaffRole.name == update_data["name"])
        )
        if dupe.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail="A staff role with this name already exists."
            )

    for field, value in update_data.items():
        setattr(role, field, value)

    audit = AuditLog(
        action="Staff Role Updated",
        target=role.name,
        target_id=str(role_id),
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details=update_data,
    )
    db.add(audit)
    await db.commit()
    await db.refresh(role)
    return role


@router.delete(
    "/{role_id}",
    response_model=MessageResponse,
    summary="Delete a staff role — Super Admin only",
)
async def delete_staff_role(
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_super_admin),
):
    """
    Delete a staff role. Any AdminUser currently assigned this role has
    their staff_role_id set to NULL automatically (ondelete="SET NULL" on
    the FK) — they are not deleted, just left without a staff role until
    reassigned.
    """
    result = await db.execute(select(StaffRole).where(StaffRole.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Staff role not found.")

    name = role.name
    await db.delete(role)

    audit = AuditLog(
        action="Staff Role Deleted",
        target=name,
        target_id=str(role_id),
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
    )
    db.add(audit)
    await db.commit()

    logger.info(f"Staff role '{name}' deleted by {admin.email}")
    return MessageResponse(message=f"Staff role '{name}' has been deleted.")
