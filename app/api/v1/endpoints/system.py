# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/system.py
#
# System-wide endpoints — notifications, announcements, system alert banner,
# admin settings, reports, and audit log.
#
# PUBLIC:
#   GET  /api/v1/system/alert          — system alert banner state (no auth)
#   GET  /api/v1/system/announcements  — active announcements for client dashboard
#
# CLIENT (auth required):
#   GET   /api/v1/notifications           — my notifications
#   PATCH /api/v1/notifications/{id}/read — mark as read
#
# ADMIN:
#   POST  /api/v1/admin/notifications/send     — broadcast notification
#   POST  /api/v1/admin/announcements          — create announcement
#   PATCH /api/v1/admin/announcements/{id}     — toggle active/hidden
#   PATCH /api/v1/admin/settings/alert         — update system alert banner
#   PATCH /api/v1/admin/settings/company       — update company info
#   PATCH /api/v1/admin/settings/fees          — update fee configuration
#   GET   /api/v1/admin/audit-log              — view audit trail
#   GET   /api/v1/admin/reports/export         — export CSV reports
# ─────────────────────────────────────────────────────────────────────────────

import logging
import csv
import io
from uuid import UUID
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import (
    Notification, Announcement, SystemSettings, FeeConfig,
    AuditLog, User, Subscription, Redemption, PaymentAccount
)
from app.schemas.schemas import (
    NotificationResponse, AnnouncementCreate, AnnouncementResponse,
    SystemAlertResponse, SystemAlertUpdate, SystemSettingsUpdate,
    FeeConfigUpdate, AdminSendNotification, MessageResponse
)
from app.api.v1.deps import get_current_user, require_permission, require_super_admin

logger = logging.getLogger(__name__)

# Routers
public_router = APIRouter()
client_router = APIRouter()
admin_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENDPOINTS (No auth required)
# ─────────────────────────────────────────────────────────────────────────────

@public_router.get(
    "/alert",
    response_model=SystemAlertResponse,
    summary="Get system alert banner state — polled by frontend on every page load",
)
async def get_system_alert(db: AsyncSession = Depends(get_db)):
    """
    Returns the current system alert banner state.

    Frontend: SystemAlertBanner.jsx calls this on mount.
    Replace the current sessionStorage approach with this API call.

    Example frontend fetch:
        useEffect(() => {
            fetch('/api/v1/system/alert')
              .then(r => r.json())
              .then(data => {
                if (data.active) showBanner(data.message, data.severity)
              })
        }, [])

    No authentication required — banner must be visible to ALL visitors
    including non-logged-in users.
    """
    result = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()

    if not settings:
        # Return default (no alert) if settings not seeded yet
        return SystemAlertResponse(
            active=False, message=None, severity="info", updated_at=None
        )

    return SystemAlertResponse(
        active=settings.alert_active,
        message=settings.alert_message,
        severity=settings.alert_severity,
        updated_at=settings.alert_updated_at,
    )


@public_router.get(
    "/announcements",
    response_model=list[AnnouncementResponse],
    summary="Get active announcements for client dashboard",
)
async def get_announcements(db: AsyncSession = Depends(get_db)):
    """
    Returns all active announcements.
    Frontend: Dashboard.jsx shows these in the announcements/market updates section.
    """
    result = await db.execute(
        select(Announcement)
        .where(Announcement.is_active == True)
        .order_by(Announcement.published_at.desc())
        .limit(10)
    )
    return result.scalars().all()


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@client_router.get(
    "",
    response_model=list[NotificationResponse],
    summary="Get my notifications",
)
async def get_my_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get all notifications for the current user, newest first.
    Frontend: Dashboard.jsx notification bell icon.
    """
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    )
    return result.scalars().all()


@client_router.patch(
    "/{notification_id}/read",
    response_model=MessageResponse,
    summary="Mark notification as read",
)
async def mark_notification_read(
    notification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a single notification as read."""
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found.")
    notif.is_read = True
    await db.commit()
    return MessageResponse(message="Notification marked as read.")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS — NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.post(
    "/notifications/send",
    response_model=MessageResponse,
    summary="Admin: Broadcast notification to clients",
)
async def send_broadcast_notification(
    body: AdminSendNotification,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("notifications")),
):
    """
    Send an in-app notification to a group of clients.
    Admin panel → Notifications section.

    recipient options:
      'all'       — all active clients
      'approved'  — only KYC-approved clients
      'pending'   — only clients with pending KYC
      UUID string — single specific client
    """
    # Build query based on recipient
    if body.recipient == "all":
        result = await db.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()
    elif body.recipient == "approved":
        result = await db.execute(
            select(User).where(User.is_active == True, User.kyc_status == "approved")
        )
        users = result.scalars().all()
    elif body.recipient == "pending":
        result = await db.execute(
            select(User).where(User.is_active == True, User.kyc_status == "pending")
        )
        users = result.scalars().all()
    else:
        # Try as specific user UUID
        try:
            user_id = UUID(body.recipient)
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            users = [user] if user else []
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid recipient value.")

    if not users:
        return MessageResponse(message="No matching recipients found.")

    # Create notification for each user
    notifications = [
        Notification(
            user_id=user.id,
            title=body.title,
            message=body.message,
            notification_type="general",
        )
        for user in users
    ]
    for n in notifications:
        db.add(n)

    # Audit log
    audit = AuditLog(
        action="Notification Broadcast",
        target=f"{len(users)} recipients ({body.recipient})",
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={"title": body.title, "recipient": body.recipient},
    )
    db.add(audit)
    await db.commit()

    logger.info(f"Notification broadcast to {len(users)} users by {admin.email}")
    return MessageResponse(message=f"Notification sent to {len(users)} client(s).")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS — ANNOUNCEMENTS
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.post(
    "/announcements",
    response_model=AnnouncementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Admin: Create a new announcement",
)
async def create_announcement(
    body: AnnouncementCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("announcements")),
):
    """Create a new market update / announcement."""
    announcement = Announcement(
        title=body.title,
        body=body.body,
        audience=body.audience,
        is_active=True,
        created_by=admin.id,
    )
    db.add(announcement)

    audit = AuditLog(
        action="Announcement Created",
        target=body.title,
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
    )
    db.add(audit)
    await db.commit()
    await db.refresh(announcement)
    return announcement


@admin_router.patch(
    "/announcements/{announcement_id}",
    response_model=MessageResponse,
    summary="Admin: Toggle announcement active/hidden",
)
async def toggle_announcement(
    announcement_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("announcements")),
):
    """Toggle an announcement between active and hidden."""
    result = await db.execute(
        select(Announcement).where(Announcement.id == announcement_id)
    )
    announcement = result.scalar_one_or_none()
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found.")

    announcement.is_active = not announcement.is_active
    state = "published" if announcement.is_active else "hidden"

    audit = AuditLog(
        action=f"Announcement {state.title()}",
        target=announcement.title,
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
    )
    db.add(audit)
    await db.commit()
    return MessageResponse(message=f"Announcement is now {state}.")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS — SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.patch(
    "/settings/alert",
    response_model=MessageResponse,
    summary="Admin: Update system alert banner — Super Admin only",
)
async def update_system_alert(
    body: SystemAlertUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),  # Only super admin controls the banner
):
    """
    Update the system-wide alert banner.
    Admin panel → System Alert section.

    When active=True, the banner immediately appears on ALL client portal pages
    for ALL visitors — logged in, logged out, and mobile.

    Frontend: After calling this, SystemAlertBanner.jsx will pick up the
    change on next poll (or page refresh).
    """
    result = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()

    if not settings:
        raise HTTPException(status_code=500, detail="System settings not initialized. Run seed script.")

    settings.alert_active = body.active
    settings.alert_message = body.message
    settings.alert_severity = body.severity
    settings.alert_updated_at = datetime.now(timezone.utc)
    settings.updated_by = admin.full_name

    audit = AuditLog(
        action=f"System Alert {'Activated' if body.active else 'Deactivated'}",
        target=body.severity,
        action_type="settings",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={"active": body.active, "message": body.message, "severity": body.severity},
    )
    db.add(audit)
    await db.commit()

    state = "activated" if body.active else "deactivated"
    return MessageResponse(message=f"System alert banner has been {state}.")


@admin_router.get(
    "/settings/company",
    summary="Admin: Get company information and dashboard visibility settings",
)
async def get_company_settings(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """
    Returns the singleton settings record so the admin panel can populate
    its System Settings form with current values (previously there was no
    GET here at all, so the form had nothing to read its initial state from).
    """
    result = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=500, detail="System settings not initialized.")
    return {
        "company_name": settings.company_name,
        "short_name": settings.short_name,
        "email": settings.email,
        "phone": settings.phone,
        "whatsapp": settings.whatsapp,
        "address": settings.address,
        "city": settings.city,
        "website": settings.website,
        "regulator": settings.regulator,
        "show_portfolio_breakdown": settings.show_portfolio_breakdown,
        "updated_at": settings.updated_at,
        "updated_by": settings.updated_by,
    }


@admin_router.patch(
    "/settings/company",
    response_model=MessageResponse,
    summary="Admin: Update company information",
)
async def update_company_settings(
    body: SystemSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """Update company info from admin panel → System Settings section."""
    result = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=500, detail="System settings not initialized.")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(settings, field, value)
    settings.updated_by = admin.full_name

    audit = AuditLog(
        action="Company Settings Updated",
        action_type="settings",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details=update_data,
    )
    db.add(audit)
    await db.commit()
    return MessageResponse(message="Company settings updated successfully.")


@admin_router.patch(
    "/settings/fees",
    response_model=MessageResponse,
    summary="Admin: Update fee and penalty configuration",
)
async def update_fee_config(
    body: FeeConfigUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_super_admin),
):
    """
    Update fee configuration from admin panel → Fees & Penalties section.
    Changes take effect immediately for new subscriptions.
    Existing subscriptions retain the fee rate at time of activation.
    """
    result = await db.execute(select(FeeConfig).where(FeeConfig.id == 1))
    fee_config = result.scalar_one_or_none()
    if not fee_config:
        raise HTTPException(status_code=500, detail="Fee config not initialized.")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(fee_config, field, value)
    fee_config.updated_by = admin.full_name

    audit = AuditLog(
        action="Fee Configuration Updated",
        action_type="settings",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details=update_data,
    )
    db.add(audit)
    await db.commit()
    return MessageResponse(message="Fee configuration updated successfully.")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS — AUDIT LOG
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.get(
    "/audit-log",
    summary="Admin: View audit log",
)
async def get_audit_log(
    action_type: str = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("audit")),
):
    """
    View the admin audit log.
    Every admin action (KYC overrides, subscription activations, settings changes)
    is logged here. Append-only — cannot be deleted.
    Required for SEC compliance.
    """
    query = select(AuditLog)
    if action_type:
        query = query.where(AuditLog.action_type == action_type)
    query = query.order_by(AuditLog.created_at.desc()).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS — REPORTS & EXPORT
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.get(
    "/reports/export",
    summary="Admin: Export data as CSV",
)
async def export_report(
    report_type: str,  # clients | subscriptions | redemptions | aum
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("reports")),
):
    """
    Export data as CSV file.
    Admin panel → Reports & Analytics section.

    report_type options:
      clients       — all client accounts with KYC status
      subscriptions — all subscriptions with amounts and status
      redemptions   — all redemption requests
      aum           — AUM summary by product
    """
    output = io.StringIO()
    writer = csv.writer(output)

    if report_type == "clients":
        writer.writerow(["Full Name", "Email", "Account Type", "KYC Status", "Joined"])
        result = await db.execute(select(User).order_by(User.created_at.desc()))
        for user in result.scalars().all():
            writer.writerow([
                user.full_name, user.email, user.account_type,
                user.kyc_status, user.created_at.strftime("%Y-%m-%d"),
            ])
        filename = "pcil_clients_report.csv"

    elif report_type == "subscriptions":
        writer.writerow(["Reference", "Client ID", "Product ID", "Amount", "Currency", "Status", "Submitted", "Activated", "Maturity"])
        result = await db.execute(select(Subscription).order_by(Subscription.submitted_at.desc()))
        for sub in result.scalars().all():
            writer.writerow([
                sub.reference, str(sub.user_id), str(sub.product_id),
                sub.amount, sub.currency, sub.status,
                sub.submitted_at.strftime("%Y-%m-%d"),
                sub.activated_at.strftime("%Y-%m-%d") if sub.activated_at else "",
                sub.maturity_date.strftime("%Y-%m-%d") if sub.maturity_date else "",
            ])
        filename = "pcil_subscriptions_report.csv"

    elif report_type == "redemptions":
        writer.writerow(["Reference", "Client ID", "Amount", "Currency", "Penalty", "Net Amount", "Premature", "Status", "Requested"])
        result = await db.execute(select(Redemption).order_by(Redemption.requested_at.desc()))
        for r in result.scalars().all():
            writer.writerow([
                r.reference, str(r.user_id), r.amount, r.currency,
                r.penalty, r.net_amount, r.is_premature, r.status,
                r.requested_at.strftime("%Y-%m-%d"),
            ])
        filename = "pcil_redemptions_report.csv"

    elif report_type == "aum":
        # AUM Summary — total active investment per product
        writer.writerow(["Product ID", "Total Active Subscriptions", "Total AUM (NGN)", "Total AUM (USD)"])
        result = await db.execute(
            select(Subscription).where(Subscription.status == "active")
        )
        subs = result.scalars().all()
        # Group by product
        from collections import defaultdict
        aum_by_product = defaultdict(lambda: {"count": 0, "ngn": 0.0, "usd": 0.0})
        for sub in subs:
            pid = str(sub.product_id)
            aum_by_product[pid]["count"] += 1
            if sub.currency == "NGN":
                aum_by_product[pid]["ngn"] += sub.amount
            else:
                aum_by_product[pid]["usd"] += sub.amount
        for pid, data in aum_by_product.items():
            writer.writerow([pid, data["count"], data["ngn"], data["usd"]])
        filename = "pcil_aum_report.csv"

    else:
        raise HTTPException(status_code=400, detail=f"Invalid report_type: {report_type}. Use: clients, subscriptions, redemptions, aum")

    # Audit log
    audit = AuditLog(
        action=f"Report Exported: {report_type}",
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
    )
    db.add(audit)
    await db.commit()

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT / CUSTODIAN ACCOUNTS  (admin CRUD)
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.get(
    "/payment-accounts",
    summary="List all custodian/payment accounts",
)
async def list_payment_accounts(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_permission("payments")),
):
    result = await db.execute(select(PaymentAccount).order_by(PaymentAccount.updated_at.desc()))
    accounts = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "bank": a.bank,
            "account_name": a.account_name,
            "account_number": a.account_number,
            "currency": a.currency,
            "label": a.label,
            "instruction": a.instruction,
            "is_active": a.is_active,
        }
        for a in accounts
    ]


@admin_router.post(
    "/payment-accounts",
    status_code=status.HTTP_201_CREATED,
    summary="Create a custodian/payment account",
)
async def create_payment_account(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_permission("payments")),
):
    if not body.get("bank") or not body.get("account_number"):
        raise HTTPException(status_code=400, detail="bank and account_number are required.")
    account = PaymentAccount(
        bank=body["bank"],
        account_name=body.get("account_name", ""),
        account_number=body["account_number"],
        currency=body.get("currency", "NGN"),
        label=body.get("label", ""),
        instruction=body.get("instruction"),
        is_active=True,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return {
        "id": str(account.id),
        "bank": account.bank,
        "account_name": account.account_name,
        "account_number": account.account_number,
        "currency": account.currency,
        "label": account.label,
        "instruction": account.instruction,
        "is_active": account.is_active,
    }


@admin_router.patch(
    "/payment-accounts/{account_id}",
    summary="Update a custodian/payment account",
)
async def update_payment_account(
    account_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_permission("payments")),
):
    result = await db.execute(select(PaymentAccount).where(PaymentAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Payment account not found.")
    for field in ("bank", "account_name", "account_number", "currency", "label", "instruction", "is_active"):
        if field in body:
            setattr(account, field, body[field])
    await db.commit()
    return {"message": "Updated successfully."}


@admin_router.delete(
    "/payment-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a custodian/payment account",
)
async def delete_payment_account(
    account_id: UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_super_admin),
):
    result = await db.execute(select(PaymentAccount).where(PaymentAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Payment account not found.")
    await db.delete(account)
    await db.commit()
