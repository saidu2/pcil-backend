# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/redemptions.py
#
# Redemption (withdrawal) endpoints.
#
# CLIENT (auth required):
#   POST /api/v1/redemptions        — request a redemption
#   GET  /api/v1/redemptions        — list my redemption requests
#
# ADMIN (admin auth required):
#   GET   /api/v1/admin/redemptions            — list all redemptions
#   PATCH /api/v1/admin/redemptions/{id}/process — process a redemption
#
# ── Premature Redemption ──────────────────────────────────────────────────────
# If a client redeems before maturity, a 20% penalty on accrued profit applies.
# The penalty percentage is configured in fee_config table (admin-managed).
# ─────────────────────────────────────────────────────────────────────────────

import logging
import re as _re
import uuid as uuid_module
from uuid import UUID
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import (
    Redemption, Subscription, Product, FeeConfig, Notification, AuditLog, User,
    Workflow, WorkflowInstance,
)
from app.schemas.schemas import RedemptionCreate, RedemptionResponse, MessageResponse
from app.api.v1.deps import get_current_user, get_current_admin, require_permission

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


def generate_redemption_reference() -> str:
    """Generate unique redemption reference. e.g. PCIL/RED/2026/AB12CD"""
    suffix = str(uuid_module.uuid4()).upper()[:6]
    year = datetime.now().year
    return f"PCIL/RED/{year}/{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=RedemptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request a redemption (withdrawal)",
)
async def request_redemption(
    body: RedemptionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Submit a redemption request for an active subscription.

    Checks:
      - Subscription must belong to current user
      - Subscription must be 'active' or 'matured'
      - Amount cannot exceed subscription amount
      - If before maturity: premature penalty is calculated and shown

    Frontend: Called from Dashboard.jsx redeem button.
    """
    # Fetch subscription
    sub_result = await db.execute(
        select(Subscription).where(
            Subscription.id == body.subscription_id,
            Subscription.user_id == current_user.id,
        )
    )
    sub = sub_result.scalar_one_or_none()

    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")

    if sub.status not in ["active", "matured"]:
        raise HTTPException(
            status_code=400,
            detail=f"Only active or matured subscriptions can be redeemed. Current status: {sub.status}"
        )

    # Check no existing pending redemption for this subscription
    existing_redemption = await db.execute(
        select(Redemption).where(
            Redemption.subscription_id == body.subscription_id,
            Redemption.status == "pending",
        )
    )
    if existing_redemption.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="A redemption request is already pending for this subscription."
        )

    now = datetime.now(timezone.utc)

    # Calculate accrued returns using product ROI
    product_result = await db.execute(select(Product).where(Product.id == sub.product_id))
    product = product_result.scalar_one_or_none()

    roi_pct = 0.0
    if product and product.roi:
        nums = [n for n in _re.findall(r'[\d.]+', product.roi) if _re.match(r'^\d+\.?\d*$', n)]
        if nums:
            roi_pct = sum(float(n) for n in nums) / len(nums)

    activated = sub.activated_at or sub.submitted_at
    if activated:
        activated_aware = activated.replace(tzinfo=timezone.utc) if activated.tzinfo is None else activated
        months_active = max(0, int((now - activated_aware).days / 30))
    else:
        months_active = 0
    accrued_returns = round(sub.amount * (roi_pct / 100 / 12) * months_active, 2)

    # Subtract already completed/pending redemptions
    already_redeemed_result = await db.execute(
        select(func.coalesce(func.sum(Redemption.amount), 0.0)).where(
            Redemption.subscription_id == sub.id,
            Redemption.status.in_(["completed", "pending"]),
        )
    )
    already_redeemed = already_redeemed_result.scalar() or 0.0
    total_redeemable = round(sub.amount + accrued_returns - already_redeemed, 2)

    # Validate amount
    if body.amount > total_redeemable:
        raise HTTPException(
            status_code=400,
            detail=f"Redemption amount cannot exceed redeemable balance of {total_redeemable:,.2f} (principal + accrued returns)."
        )

    # ── Calculate premature penalty ────────────────────────────────────────
    is_premature = False
    penalty = 0.0

    if sub.maturity_date and now < sub.maturity_date:
        is_premature = True
        # Get penalty percentage from fee config
        fee_result = await db.execute(select(FeeConfig).where(FeeConfig.id == 1))
        fee_config = fee_result.scalar_one_or_none()
        penalty_pct = fee_config.premature_penalty_pct if fee_config else 20.0

        # Penalty is applied on estimated profit portion only
        # Simple estimate: penalty_pct% of the amount being redeemed
        # TODO: Calculate actual accrued profit from D365 or investment records
        # For now: penalty applied to full redemption amount as conservative estimate
        penalty = round((penalty_pct / 100) * body.amount, 2)

    net_amount = round(body.amount - penalty, 2)

    # Create redemption request
    redemption = Redemption(
        user_id=current_user.id,
        subscription_id=body.subscription_id,
        amount=body.amount,
        currency=sub.currency,
        penalty=penalty,
        net_amount=net_amount,
        reference=generate_redemption_reference(),
        is_premature=is_premature,
        note=body.note,
        status="pending",
    )
    db.add(redemption)

    # Notification
    if is_premature:
        notif_msg = (
            f"Your redemption request of {sub.currency} {body.amount:,.2f} has been submitted. "
            f"Note: Premature redemption penalty of {sub.currency} {penalty:,.2f} applies. "
            f"Net amount to be paid: {sub.currency} {net_amount:,.2f}."
        )
    else:
        notif_msg = (
            f"Your redemption request of {sub.currency} {body.amount:,.2f} has been submitted "
            f"and is being processed."
        )

    notification = Notification(
        user_id=current_user.id,
        title="Redemption Request Submitted",
        message=notif_msg,
        notification_type="redemption",
    )
    db.add(notification)
    await db.commit()
    await db.refresh(redemption)

    # Fire workflow trigger
    await _fire_workflow(
        trigger="redemption_requested",
        record_type="redemption",
        record_id=redemption.id,
        client_id=current_user.id,
        client_name=current_user.full_name,
        db=db,
    )
    await db.commit()

    logger.info(
        f"Redemption requested: {redemption.reference} by {current_user.email}. "
        f"Premature: {is_premature}. Penalty: {penalty}"
    )
    return redemption


@router.get(
    "",
    response_model=list[RedemptionResponse],
    summary="List my redemption requests",
)
async def list_my_redemptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all redemption requests for the current client."""
    result = await db.execute(
        select(Redemption)
        .where(Redemption.user_id == current_user.id)
        .order_by(Redemption.requested_at.desc())
    )
    return result.scalars().all()


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.get(
    "",
    summary="Admin: List all redemption requests",
)
async def admin_list_redemptions(
    status_filter: str = None,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),  # any logged-in staff can VIEW — actions stay permission-gated
):
    """List all redemption requests enriched with client name and product name."""
    from app.models.models import Product
    from sqlalchemy.orm import selectinload

    query = (
        select(Redemption)
        .options(
            selectinload(Redemption.subscription).selectinload(Subscription.product),
            selectinload(Redemption.user),
        )
    )
    if status_filter:
        query = query.where(Redemption.status == status_filter)
    query = query.order_by(Redemption.requested_at.desc())
    result = await db.execute(query)
    redemptions = result.scalars().all()

    return [
        {
            "id":             str(r.id),
            "reference":      r.reference,
            "user_id":        str(r.user_id),
            "client_name":    r.user.full_name if r.user else "—",
            "client_email":   r.user.email if r.user else "—",
            "subscription_id": str(r.subscription_id),
            "product_name":   r.subscription.product.name if r.subscription and r.subscription.product else "—",
            "amount":         r.amount,
            "currency":       r.currency,
            "penalty":        r.penalty,
            "net_amount":     r.net_amount,
            "is_premature":   r.is_premature,
            "status":         r.status,
            "note":           r.note,
            "requested_at":   r.requested_at,
            "processed_at":   r.processed_at,
            "processed_by":   r.processed_by,
        }
        for r in redemptions
    ]


@admin_router.patch(
    "/{redemption_id}/process",
    response_model=MessageResponse,
    summary="Admin: Process a redemption request",
)
async def process_redemption(
    redemption_id: UUID,
    action: str,  # complete | reject
    note: str = None,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("redemptions")),
):
    """
    Process a pending redemption request.
    Admin panel → Redemption Management section.

    action options:
      complete — payment has been made to client
      reject   — request rejected (with note explaining why)
    """
    if action not in ["complete", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'complete' or 'reject'.")

    result = await db.execute(
        select(Redemption).where(Redemption.id == redemption_id)
    )
    redemption = result.scalar_one_or_none()

    if not redemption:
        raise HTTPException(status_code=404, detail="Redemption request not found.")

    if redemption.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Only pending redemptions can be processed. Current status: {redemption.status}"
        )

    if action == "complete":
        redemption.status = "completed"
        redemption.processed_at = datetime.now(timezone.utc)
        redemption.processed_by = admin.full_name

        # Only mark fully redeemed if total redeemed >= subscription amount
        sub_result = await db.execute(
            select(Subscription).where(Subscription.id == redemption.subscription_id)
        )
        sub = sub_result.scalar_one_or_none()
        if sub:
            total_redeemed_result = await db.execute(
                select(func.coalesce(func.sum(Redemption.amount), 0.0)).where(
                    Redemption.subscription_id == sub.id,
                    Redemption.status == "completed",
                    Redemption.id != redemption.id,
                )
            )
            already_redeemed = total_redeemed_result.scalar() or 0.0
            if already_redeemed + redemption.amount >= sub.amount:
                sub.status = "redeemed"
            # else: partial redemption — subscription stays active

        notification = Notification(
            user_id=redemption.user_id,
            title="Redemption Processed ✓",
            message=(
                f"Your redemption of {redemption.currency} {redemption.net_amount:,.2f} "
                f"has been processed and payment made to your account."
            ),
            notification_type="redemption",
        )
        message = f"Redemption {redemption.reference} marked as completed."

    else:  # reject
        redemption.status = "rejected"
        redemption.note = note
        redemption.processed_at = datetime.now(timezone.utc)
        redemption.processed_by = admin.full_name

        notification = Notification(
            user_id=redemption.user_id,
            title="Redemption Request Update",
            message=f"Your redemption request was not processed. {note or 'Please contact support for details.'}",
            notification_type="redemption",
        )
        message = f"Redemption {redemption.reference} rejected."

    db.add(notification)

    audit = AuditLog(
        action=f"Redemption {action.title()}d",
        target=redemption.reference,
        target_id=str(redemption_id),
        action_type="redemption",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={"action": action, "note": note},
    )
    db.add(audit)
    await db.commit()

    logger.info(f"Redemption {redemption.reference} {action}d by {admin.email}")
    return MessageResponse(message=message)
