# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/webhooks.py
#
# Inbound webhook receiver for Microsoft Dynamics 365.
#
# ── How This Works ────────────────────────────────────────────────────────────
#  When the D365 compliance/operations team approves or denies a KYC or
#  subscription in their D365 interface, D365 fires an HTTP POST to this
#  endpoint with the decision details.
#
#  This endpoint then:
#    1. Verifies the request is genuinely from D365 (via shared secret)
#    2. Updates the relevant database record
#    3. Updates the client's status on their account
#    4. Sends a notification to the client (in-app + email)
#    5. Logs the action to the audit log
#
# ── D365 Setup Steps ─────────────────────────────────────────────────────────
#  In D365: Settings → Customizations → Webhooks → New
#    Name:    PCIL Backend Webhook
#    URL:     https://yourbackend.azurewebsites.net/api/v1/webhooks/d365
#    Auth:    HttpHeader
#    Name:    X-D365-Secret
#    Value:   (same as D365_WEBHOOK_SECRET in .env)
#
#  Triggers (register separately for each):
#    Entity: pcil_kycsubmissions    → Create/Update
#    Entity: pcil_subscriptions     → Create/Update
# ─────────────────────────────────────────────────────────────────────────────

import logging
import hmac
import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.models import (
    KycSubmission, Subscription, User, Notification, AuditLog
)
from app.schemas.schemas import D365WebhookPayload, MessageResponse

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Webhook Signature Verification ───────────────────────────────────────────

def verify_d365_signature(request_secret: str) -> bool:
    """
    Verify that the webhook request is genuinely from D365.
    D365 sends the shared secret in the X-D365-Secret header.

    ── TODO: Upgrade to HMAC signature verification ─────────────────────────
    For production, D365 can be configured to send an HMAC-SHA256 signature
    of the request body instead of a plain shared secret.
    Ask your D365 developer to configure this for better security.
    ────────────────────────────────────────────────────────────────────────
    """
    if not settings.D365_WEBHOOK_SECRET:
        # If no secret configured, log a warning but allow (for development)
        logger.warning(
            "D365_WEBHOOK_SECRET not configured. Webhook security is disabled. "
            "Set this in .env before going to production!"
        )
        return True

    return hmac.compare_digest(request_secret, settings.D365_WEBHOOK_SECRET)


# ── Main Webhook Endpoint ─────────────────────────────────────────────────────

@router.post(
    "/d365",
    response_model=MessageResponse,
    summary="Receive decisions from Microsoft Dynamics 365",
)
async def receive_d365_webhook(
    payload: D365WebhookPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_d365_secret: str = Header(default="", alias="X-D365-Secret"),
):
    """
    Receives webhook events from Microsoft Dynamics 365.

    Handles these event types (defined in D365WebhookPayload schema):
      KYC_APPROVED  — compliance team approved a KYC
      KYC_DENIED    — compliance team denied a KYC
      SUB_APPROVED  — operations team activated a subscription
      SUB_DENIED    — operations team denied a subscription
      SUB_MATURED   — investment has reached its maturity date

    ── IMPORTANT NOTE FOR D365 DEVELOPER ────────────────────────────────────
    The payload structure (payload.event, payload.record_id, etc.) is defined
    in app/schemas/schemas.py → D365WebhookPayload.
    The D365 plugin/workflow that fires this webhook must send a JSON body
    matching that schema exactly.
    ────────────────────────────────────────────────────────────────────────
    """
    # ── Step 1: Verify the request is from D365 ───────────────────────────────
    if not verify_d365_signature(x_d365_secret):
        logger.warning(
            f"D365 webhook received with invalid secret. "
            f"IP: {request.client.host}. Possible spoofing attempt."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret.",
        )

    logger.info(f"D365 webhook received: event={payload.event}, record_id={payload.record_id}")

    # ── Step 2: Route to the appropriate handler ───────────────────────────────
    try:
        if payload.event == "KYC_APPROVED":
            await _handle_kyc_approved(payload, db)
        elif payload.event == "KYC_DENIED":
            await _handle_kyc_denied(payload, db)
        elif payload.event == "SUB_APPROVED":
            await _handle_subscription_approved(payload, db)
        elif payload.event == "SUB_DENIED":
            await _handle_subscription_denied(payload, db)
        elif payload.event == "SUB_MATURED":
            await _handle_subscription_matured(payload, db)
        else:
            # Unknown event — log it but don't error (D365 may add new events)
            logger.warning(f"Unknown D365 webhook event: {payload.event}")

        return MessageResponse(message=f"Event {payload.event} processed successfully.")

    except Exception as e:
        logger.error(f"Error processing D365 webhook {payload.event}: {e}", exc_info=True)
        # Return 200 to D365 even on error — otherwise D365 will retry repeatedly
        # Log the error and investigate manually
        return MessageResponse(message="Webhook received. Processing error logged.", success=False)


# ── Event Handlers ────────────────────────────────────────────────────────────

async def _handle_kyc_approved(payload: D365WebhookPayload, db: AsyncSession):
    """
    D365 compliance team approved a KYC submission.
    1. Find the KYC record by D365 record ID
    2. Update KYC status to 'approved'
    3. Update user's kyc_status to 'approved'
    4. Send in-app notification to client
    5. Log to audit trail
    TODO: Trigger SendGrid email (KYC approved template)
    """
    # Find KYC submission by D365 record ID
    result = await db.execute(
        select(KycSubmission).where(KycSubmission.d365_record_id == payload.record_id)
    )
    kyc = result.scalar_one_or_none()

    if not kyc:
        logger.error(f"KYC with D365 ID {payload.record_id} not found in database.")
        return

    # Update KYC record
    kyc.status = "approved"
    kyc.reviewed_at = datetime.now(timezone.utc)
    kyc.reviewed_by = "D365 Compliance Team"

    # Update user KYC status
    await db.execute(
        update(User)
        .where(User.id == kyc.user_id)
        .values(kyc_status="approved")
    )

    # Send in-app notification
    notification = Notification(
        user_id=kyc.user_id,
        title="KYC Approved ✓",
        message=(
            "Your KYC verification has been approved. "
            "You can now subscribe to investment products."
        ),
        notification_type="kyc",
    )
    db.add(notification)

    # Audit log
    audit = AuditLog(
        action="KYC Approved (D365)",
        target_id=str(kyc.id),
        action_type="kyc",
        performed_by_name="D365 Compliance Team",
    )
    db.add(audit)

    await db.commit()

    # TODO: Send KYC approval email via SendGrid
    # from app.tasks.email_tasks import send_kyc_approved_email
    # send_kyc_approved_email.delay(user_email, user_full_name)

    logger.info(f"KYC {kyc.id} approved via D365 webhook.")


async def _handle_kyc_denied(payload: D365WebhookPayload, db: AsyncSession):
    """
    D365 compliance team denied a KYC submission.
    payload.reason should contain the denial reason from D365.
    """
    result = await db.execute(
        select(KycSubmission).where(KycSubmission.d365_record_id == payload.record_id)
    )
    kyc = result.scalar_one_or_none()

    if not kyc:
        logger.error(f"KYC with D365 ID {payload.record_id} not found.")
        return

    kyc.status = "denied"
    kyc.denial_reason = payload.reason or "Not specified"
    kyc.reviewed_at = datetime.now(timezone.utc)
    kyc.reviewed_by = "D365 Compliance Team"

    await db.execute(
        update(User)
        .where(User.id == kyc.user_id)
        .values(kyc_status="denied", kyc_denied_reason=payload.reason)
    )

    notification = Notification(
        user_id=kyc.user_id,
        title="KYC Requires Attention",
        message=(
            f"Your KYC verification was not approved. "
            f"Reason: {payload.reason or 'Please contact support for details.'}. "
            f"You may resubmit with corrected documents."
        ),
        notification_type="kyc",
    )
    db.add(notification)

    audit = AuditLog(
        action="KYC Denied (D365)",
        target_id=str(kyc.id),
        action_type="kyc",
        performed_by_name="D365 Compliance Team",
        details={"reason": payload.reason},
    )
    db.add(audit)

    await db.commit()

    # TODO: Send KYC denial email via SendGrid
    # send_kyc_denied_email.delay(user_email, user_full_name, payload.reason)

    logger.info(f"KYC {kyc.id} denied via D365 webhook. Reason: {payload.reason}")


async def _handle_subscription_approved(payload: D365WebhookPayload, db: AsyncSession):
    """D365 operations team activated a subscription."""
    result = await db.execute(
        select(Subscription).where(Subscription.d365_record_id == payload.record_id)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        logger.error(f"Subscription with D365 ID {payload.record_id} not found.")
        return

    sub.status = "active"
    sub.activated_at = datetime.now(timezone.utc)

    notification = Notification(
        user_id=sub.user_id,
        title="Investment Activated ✓",
        message="Your investment subscription has been activated and is now live.",
        notification_type="subscription",
    )
    db.add(notification)

    audit = AuditLog(
        action="Subscription Activated (D365)",
        target_id=str(sub.id),
        action_type="subscription",
        performed_by_name="D365 Operations Team",
    )
    db.add(audit)

    await db.commit()
    logger.info(f"Subscription {sub.id} activated via D365 webhook.")


async def _handle_subscription_denied(payload: D365WebhookPayload, db: AsyncSession):
    """D365 operations team denied a subscription."""
    result = await db.execute(
        select(Subscription).where(Subscription.d365_record_id == payload.record_id)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        logger.error(f"Subscription with D365 ID {payload.record_id} not found.")
        return

    sub.status = "denied"
    sub.denial_reason = payload.reason

    notification = Notification(
        user_id=sub.user_id,
        title="Subscription Update",
        message=(
            f"Your subscription was not approved. "
            f"Reason: {payload.reason or 'Please contact support.'}"
        ),
        notification_type="subscription",
    )
    db.add(notification)

    await db.commit()
    logger.info(f"Subscription {sub.id} denied via D365 webhook.")


async def _handle_subscription_matured(payload: D365WebhookPayload, db: AsyncSession):
    """Investment has reached its maturity date."""
    result = await db.execute(
        select(Subscription).where(Subscription.d365_record_id == payload.record_id)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        return

    sub.status = "matured"
    sub.matured_at = datetime.now(timezone.utc)

    notification = Notification(
        user_id=sub.user_id,
        title="Investment Matured 🎉",
        message=(
            "Your investment has reached its maturity date. "
            "Please log in to your dashboard to initiate redemption."
        ),
        notification_type="subscription",
    )
    db.add(notification)

    await db.commit()
    logger.info(f"Subscription {sub.id} matured via D365 webhook.")
