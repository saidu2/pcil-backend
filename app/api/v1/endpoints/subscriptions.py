# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/subscriptions.py
#
# Investment Subscription endpoints.
#
# CLIENT (auth required):
#   POST  /api/v1/subscriptions                    — create new subscription
#   GET   /api/v1/subscriptions                    — list my subscriptions
#   GET   /api/v1/subscriptions/{id}               — get single subscription
#   POST  /api/v1/subscriptions/{id}/receipt       — upload payment receipt
#
# ADMIN (admin auth required):
#   GET   /api/v1/admin/subscriptions              — list all subscriptions
#   PATCH /api/v1/admin/subscriptions/{id}/action  — activate or deny
#
# ── Subscription Flow ─────────────────────────────────────────────────────────
# 1. Client selects product → POST /subscriptions (status: pending_payment)
# 2. Client sees bank details → transfers money
# 3. Client uploads receipt → POST /subscriptions/{id}/receipt (status: pending_review)
# 4. Admin reviews in admin panel → activates or denies
# 5. On activation → D365 push + certificate can be issued
# ─────────────────────────────────────────────────────────────────────────────

import logging
import uuid as uuid_module
from uuid import UUID
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import (
    Subscription, Product, User, Notification, AuditLog, PaymentAccount,
    Workflow, WorkflowInstance,
)
from app.schemas.schemas import (
    SubscriptionCreate, SubscriptionResponse,
    SubscriptionAdminAction, MessageResponse
)
from app.api.v1.deps import get_current_user, get_current_verified_user, get_current_admin, require_permission

logger = logging.getLogger(__name__)
router = APIRouter()
admin_router = APIRouter()


async def _fire_workflow(trigger: str, record_type: str, record_id, client_id, client_name: str, db: AsyncSession):
    """Fire an active workflow for this trigger. Silent if none configured. See kyc.py's version for full details on the DB-level duplicate guard."""
    try:
        existing = await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.record_type == record_type,
                WorkflowInstance.record_id == record_id,
                WorkflowInstance.status.in_(["pending", "in_progress"]),
            )
        )
        if existing.scalars().first():
            return

        result = await db.execute(
            select(Workflow).where(Workflow.trigger == trigger, Workflow.is_active == True)
        )
        workflow = result.scalars().first()
        if not workflow:
            return
        instance = WorkflowInstance(
            workflow_id=workflow.id, record_type=record_type, record_id=record_id,
            client_id=client_id, client_name=client_name, current_step=1,
            status="pending", step_history=[],
        )
        try:
            async with db.begin_nested():
                db.add(instance)
                await db.flush()
        except IntegrityError:
            logger.info(f"Duplicate workflow instance prevented at DB level for {record_type}/{record_id}.")
    except Exception as e:
        logger.warning(f"Workflow trigger failed silently ({trigger}): {e}")


def generate_reference() -> str:
    """Generate a unique subscription reference. e.g. PCIL/SUB/2026/AB12CD"""
    suffix = str(uuid_module.uuid4()).upper()[:6]
    year = datetime.now().year
    return f"PCIL/SUB/{year}/{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe to an investment product",
)
async def create_subscription(
    body: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_verified_user),  # KYC must be approved
):
    """
    Create a new investment subscription.

    Requires KYC to be approved (get_current_verified_user dependency).
    Frontend: Called from Products.jsx subscribe modal on form submit.

    Returns subscription with status='pending_payment' and bank details
    to show the client where to transfer money.
    """
    # Verify product exists and is active
    result = await db.execute(
        select(Product).where(Product.id == body.product_id, Product.is_active == True)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found or is no longer active.")

    # Check minimum investment amount
    if product.min_amount > 0 and body.amount < product.min_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum investment for {product.name} is {product.min_amount_display}."
        )

    # Create subscription
    subscription = Subscription(
        user_id=current_user.id,
        product_id=body.product_id,
        amount=body.amount,
        currency=body.currency,
        reference=generate_reference(),
        status="pending_payment",
    )
    db.add(subscription)

    # Notification
    notification = Notification(
        user_id=current_user.id,
        title="Subscription Initiated",
        message=(
            f"Your subscription to {product.name} has been initiated. "
            f"Please complete payment and upload your receipt to proceed."
        ),
        notification_type="subscription",
    )
    db.add(notification)

    await db.commit()
    await db.refresh(subscription)

    logger.info(f"Subscription created: {subscription.reference} by {current_user.email}")
    return subscription


@router.get(
    "",
    summary="List my subscriptions",
)
async def list_my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get all subscriptions for the current client, enriched with product info.
    Frontend: Used by Dashboard.jsx to show active investments.
    """
    result = await db.execute(
        select(Subscription, Product)
        .outerjoin(Product, Subscription.product_id == Product.id)
        .where(Subscription.user_id == current_user.id)
        .order_by(Subscription.submitted_at.desc())
    )
    rows = result.all()
    out = []
    for sub, prod in rows:
        out.append({
            "id":            str(sub.id),
            "user_id":       str(sub.user_id),
            "product_id":    str(sub.product_id),
            "product_name":  prod.name if prod else "Investment Product",
            "product_roi":   prod.roi if prod else None,
            "product_category": prod.category if prod else None,
            "amount":        sub.amount,
            "currency":      sub.currency,
            "reference":     sub.reference,
            "status":        sub.status,
            "receipt_url":   sub.receipt_url if hasattr(sub, "receipt_url") else None,
            "activated_at":  sub.activated_at.isoformat() if sub.activated_at else None,
            "maturity_date": sub.maturity_date.isoformat() if sub.maturity_date else None,
            "submitted_at":  sub.submitted_at.isoformat(),
        })
    return out


@router.get(
    "/{subscription_id}",
    response_model=SubscriptionResponse,
    summary="Get a single subscription",
)
async def get_subscription(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get details of a single subscription (must belong to current user)."""
    result = await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.user_id == current_user.id,
        )
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")
    return sub


@router.post(
    "/{subscription_id}/receipt",
    response_model=MessageResponse,
    summary="Upload payment receipt",
)
async def upload_receipt(
    subscription_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Upload payment receipt after bank transfer.
    Moves subscription from pending_payment → pending_review.

    Frontend: Called from Products.jsx receipt upload step.

    ── Azure Blob Storage Integration Point ─────────────────────────────────
    TODO: Upload file to Azure Blob Storage (payment-receipts container).
    Same pattern as KYC document upload in kyc.py.
    For now: saves a placeholder URL.
    ─────────────────────────────────────────────────────────────────────────
    """
    result = await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.user_id == current_user.id,
        )
    )
    sub = result.scalar_one_or_none()

    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")
    if sub.status not in ["pending_payment", "pending_review"]:
        raise HTTPException(status_code=400, detail="Receipt can only be uploaded for pending subscriptions.")

    # Validate file
    allowed_types = ["application/pdf", "image/jpeg", "image/png", "image/jpg"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid file type. PDF, JPG, or PNG only.")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Max 5MB.")

    # Save base64 content directly in DB so admin can view it immediately.
    # When Azure Blob Storage is configured, also upload there and store the URL.
    import base64
    b64 = base64.b64encode(content).decode("utf-8")

    # ── TODO: Upload to Azure Blob Storage ────────────────────────────────
    # blob_url = await upload_to_azure(content, file.filename, container="payment-receipts")
    # sub.receipt_url = blob_url

    # Update subscription
    sub.receipt_data      = b64
    sub.receipt_filename  = file.filename
    sub.receipt_mime_type = file.content_type
    sub.receipt_uploaded_at = datetime.now(timezone.utc)
    sub.status = "pending_review"

    # Notify client
    notification = Notification(
        user_id=current_user.id,
        title="Receipt Uploaded ✓",
        message="Your payment receipt has been received. Our team will review and activate your investment shortly.",
        notification_type="subscription",
    )
    db.add(notification)
    await db.commit()

    # Fire workflow trigger — subscription now has a receipt and is
    # actionable by staff. Firing here (not at initial creation) so the
    # task only appears once there's actually something to review.
    await _fire_workflow(
        trigger="subscription_created",
        record_type="subscription",
        record_id=sub.id,
        client_id=current_user.id,
        client_name=current_user.full_name,
        db=db,
    )
    await db.commit()

    return MessageResponse(message="Receipt uploaded successfully. Your subscription is now under review.")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.get(
    "",
    summary="Admin: List all subscriptions",
)
async def admin_list_subscriptions(
    status_filter: str = None,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),  # any logged-in staff can VIEW — actions stay permission-gated
):
    """List all client subscriptions enriched with client and product names."""
    from app.models.models import User
    query = (
        select(Subscription, User, Product)
        .outerjoin(User, Subscription.user_id == User.id)
        .outerjoin(Product, Subscription.product_id == Product.id)
    )
    if status_filter:
        query = query.where(Subscription.status == status_filter)
    query = query.order_by(Subscription.submitted_at.desc())
    result = await db.execute(query)
    rows = result.all()

    out = []
    for sub, user, prod in rows:
        out.append({
            "id":            str(sub.id),
            "user_id":       str(sub.user_id),
            "product_id":    str(sub.product_id),
            "client_name":   user.full_name if user else "—",
            "client_email":  user.email if user else "—",
            "product_name":  prod.name if prod else "—",
            "amount":        sub.amount,
            "currency":      sub.currency,
            "reference":     sub.reference,
            "status":        sub.status,
            "denial_reason": sub.denial_reason,
            "receipt_url":   sub.receipt_url,
            "has_receipt":   bool(sub.receipt_data),
            "receipt_uploaded_at": sub.receipt_uploaded_at.isoformat() if sub.receipt_uploaded_at else None,
            "activated_at":  sub.activated_at.isoformat() if sub.activated_at else None,
            "maturity_date": sub.maturity_date.isoformat() if sub.maturity_date else None,
            "submitted_at":  sub.submitted_at.isoformat(),
        })
    return out


@admin_router.get(
    "/{subscription_id}/receipt",
    summary="Admin: Fetch receipt file for a subscription",
)
async def admin_get_receipt(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),  # any logged-in staff can VIEW — actions stay permission-gated
):
    """
    Returns the base64-encoded receipt file so the admin can view it inline.
    ReceiptCell in AdminPanel.jsx calls this endpoint.
    Response: { data: "<base64>", mime_type: "image/jpeg", filename: "receipt.jpg" }
    """
    result = await db.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")
    if not sub.receipt_data:
        raise HTTPException(status_code=404, detail="No receipt uploaded for this subscription.")

    return {
        "data":      sub.receipt_data,
        "mime_type": sub.receipt_mime_type or "application/octet-stream",
        "filename":  sub.receipt_filename or "receipt",
    }


@admin_router.patch(
    "/{subscription_id}/action",
    response_model=MessageResponse,
    summary="Admin: Activate or deny a subscription",
)
async def subscription_action(
    subscription_id: UUID,
    body: SubscriptionAdminAction,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("subscriptions")),
):
    """
    Activate or deny a pending_review subscription.
    Admin panel → Subscription Management section.

    On activation:
      - Status → active
      - activated_at set to now
      - maturity_date set (required in body)
      - D365 push triggered (TODO)
      - Client notification sent

    On denial:
      - Status → denied with reason
      - Client notification sent
    """
    if body.action not in ["activate", "deny"]:
        raise HTTPException(status_code=400, detail="Action must be 'activate' or 'deny'.")

    result = await db.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")

    if sub.status != "pending_review":
        raise HTTPException(
            status_code=400,
            detail=f"Only pending_review subscriptions can be actioned. Current status: {sub.status}"
        )

    # Get product name for notifications
    product_result = await db.execute(select(Product).where(Product.id == sub.product_id))
    product = product_result.scalar_one_or_none()
    product_name = product.name if product else "your investment"

    if body.action == "activate":
        if not body.maturity_date:
            raise HTTPException(status_code=400, detail="maturity_date is required when activating.")

        sub.status = "active"
        sub.activated_at = datetime.now(timezone.utc)
        sub.maturity_date = body.maturity_date

        notification = Notification(
            user_id=sub.user_id,
            title="Investment Activated ✓",
            message=f"Your investment in {product_name} is now active. Maturity date: {body.maturity_date.strftime('%d %B %Y')}.",
            notification_type="subscription",
        )

        # ── TODO: Push to D365 ────────────────────────────────────────────
        # from app.tasks.d365_tasks import push_subscription_to_d365
        # push_subscription_to_d365.delay(str(sub.id), ...)

        # ── TODO: Send activation email via SendGrid ───────────────────────
        # send_subscription_activated_email.delay(...)

        message = f"Subscription {sub.reference} has been activated."
        logger.info(f"Subscription {sub.reference} activated by {admin.email}")

    else:  # deny
        if not body.reason:
            raise HTTPException(status_code=400, detail="A reason is required when denying.")

        sub.status = "denied"
        sub.denial_reason = body.reason

        notification = Notification(
            user_id=sub.user_id,
            title="Subscription Update",
            message=f"Your subscription to {product_name} was not approved. Reason: {body.reason}. Please contact support.",
            notification_type="subscription",
        )
        message = f"Subscription {sub.reference} has been denied."
        logger.info(f"Subscription {sub.reference} denied by {admin.email}. Reason: {body.reason}")

    db.add(notification)

    # Audit log
    audit = AuditLog(
        action=f"Subscription {body.action.title()}d",
        target=sub.reference,
        target_id=str(subscription_id),
        action_type="subscription",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={"action": body.action, "reason": body.reason},
    )
    db.add(audit)
    await db.commit()

    return MessageResponse(message=message)
