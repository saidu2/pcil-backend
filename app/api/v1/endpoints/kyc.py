# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/kyc.py
#
# KYC (Know Your Customer) endpoints.
#
# CLIENT (auth required):
#   POST  /api/v1/kyc/submit              — submit KYC form data
#   GET   /api/v1/kyc/status             — get current KYC status
#   POST  /api/v1/kyc/documents/upload   — upload KYC document to Azure Blob
#
# ADMIN (admin auth required):
#   GET   /api/v1/admin/kyc              — list all KYC submissions
#   PATCH /api/v1/admin/kyc/{id}/override — manually approve or deny KYC
#
# ── D365 Integration Point ────────────────────────────────────────────────────
# After KYC is submitted, a Celery background task pushes the record to D365.
# D365 compliance team reviews and fires a webhook back (see webhooks.py).
# Until D365 is configured, admin can manually override via the admin endpoint.
# ─────────────────────────────────────────────────────────────────────────────

import io
import logging
import re
import uuid
from pathlib import Path
from uuid import UUID
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import KycSubmission, User, Notification, AuditLog, Workflow, WorkflowInstance, AdminUser, StaffRole
from app.schemas.schemas import (
    KycSubmit, KycResponse, KycAdminOverride, MessageResponse
)
from app.core.storage import save_file, build_filename, read_file_bytes
from app.core.mailer import send_kyc_decision, send_kyc_submitted_alert
from app.api.v1.deps import get_current_user, get_current_admin, require_permission

logger = logging.getLogger(__name__)
router = APIRouter()
admin_router = APIRouter()


async def _fire_workflow(trigger: str, record_type: str, record_id, client_id, client_name: str, db: AsyncSession):
    """
    Find the active workflow for this trigger and create a new instance.
    Silent — never blocks the main flow if no workflow is configured.

    Idempotency: the SELECT below is a fast-path check (avoids a DB round
    trip to the constraint in the common case), but the real guarantee
    against duplicates is the partial unique index on WorkflowInstance
    (record_type, record_id) WHERE status IN ('pending','in_progress') —
    see models.py. Two near-simultaneous calls (e.g. a double-click, or a
    dev-mode double effect fire) could both pass the SELECT check before
    either commits; the DB constraint is what actually closes that race.
    The insert runs inside a savepoint so a caught duplicate-key violation
    only undoes THIS insert, not the caller's whole transaction (which
    still needs to save the actual KYC/subscription/redemption record).
    """
    try:
        existing = await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.record_type == record_type,
                WorkflowInstance.record_id == record_id,
                WorkflowInstance.status.in_(["pending", "in_progress"]),
            )
        )
        if existing.scalars().first():
            logger.info(f"Workflow instance already exists for {record_type}/{record_id} — skipping duplicate.")
            return

        result = await db.execute(
            select(Workflow).where(Workflow.trigger == trigger, Workflow.is_active == True)
        )
        workflow = result.scalars().first()
        if not workflow:
            return  # No active workflow for this trigger — that's fine

        instance = WorkflowInstance(
            workflow_id=workflow.id,
            record_type=record_type,
            record_id=record_id,
            client_id=client_id,
            client_name=client_name,
            current_step=1,
            status="pending",
            step_history=[],
        )
        try:
            async with db.begin_nested():
                db.add(instance)
                await db.flush()
            logger.info(f"Workflow instance created: {trigger} for {record_type}/{record_id}")
        except IntegrityError:
            logger.info(f"Duplicate workflow instance prevented at DB level for {record_type}/{record_id}.")
    except Exception as e:
        logger.warning(f"Workflow trigger failed silently ({trigger}): {e}")


async def _notify_staff_kyc_submitted(client_name: str, client_email: str, is_resubmission: bool, db: AsyncSession):
    """
    Emails every active staff member qualified to approve KYC — super admins,
    plus anyone whose staff_role has can_approve_kyc=True — the moment a
    client submits. Previously nothing proactively told staff a submission
    had arrived; they had to actively open KYC Management or My Tasks to
    notice one. This closes that gap without needing a schema change: it
    reuses the existing Resend/SMTP mailer, not an in-app notification (the
    Notification table is a hard FK to users.id, client-only by design — an
    in-app staff notification bell would need its own new table and is a
    separate piece of work).

    Silent on failure, same as _fire_workflow above: a mail server hiccup
    must never roll back the KYC submission itself. One bad send doesn't
    stop the rest — each recipient is emailed independently.
    """
    try:
        result = await db.execute(
            select(AdminUser).outerjoin(StaffRole, AdminUser.staff_role_id == StaffRole.id).where(
                AdminUser.is_active == True,
                (AdminUser.role == "super_admin") | (StaffRole.can_approve_kyc == True),
            )
        )
        staff = result.scalars().unique().all()
        if not staff:
            logger.info("No staff qualified for KYC-submitted alerts (no super admin or can_approve_kyc role found).")
            return

        for admin in staff:
            try:
                send_kyc_submitted_alert(
                    to=admin.email, staff_name=admin.full_name,
                    client_name=client_name, client_email=client_email,
                    is_resubmission=is_resubmission,
                )
            except Exception as e:
                logger.warning(f"Could not send KYC-submitted alert to {admin.email}: {e}")
    except Exception as e:
        logger.warning(f"Staff KYC-submitted notification failed silently: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/submit",
    response_model=KycResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit KYC form",
)
async def submit_kyc(
    body: KycSubmit,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Submit KYC form data.

    Frontend: Called from KYC.jsx on form submission.
    Documents are uploaded separately via POST /api/v1/kyc/documents/upload.

    Flow after submission:
      1. KYC record created with status='pending'
      2. User's kyc_status updated to 'pending'
      3. TODO: Celery task pushes to D365 for compliance review
      4. D365 fires webhook back with decision (see webhooks.py)
      5. OR admin manually overrides via admin endpoint below
    """
    # Check if user already has a KYC submission
    existing = await db.execute(
        select(KycSubmission).where(KycSubmission.user_id == current_user.id)
    )
    kyc = existing.scalar_one_or_none()
    is_resubmission = kyc is not None

    if kyc and kyc.status == "approved":
        raise HTTPException(
            status_code=400,
            detail="Your KYC is already approved. No need to resubmit."
        )

    # account_type belongs on User, not KycSubmission (that model has no
    # such column) — pull it out before the generic field-copy below, then
    # apply it to the user explicitly alongside kyc_status.
    submitted_data = body.model_dump(exclude_unset=True)
    submitted_account_type = submitted_data.pop("account_type", None)

    if kyc:
        # Resubmission — update existing record (e.g. after denial)
        for field, value in submitted_data.items():
            setattr(kyc, field, value)
        kyc.status = "pending"
        kyc.denial_reason = None
        kyc.d365_synced = False
        # BUGFIX: submitted_at only had a server_default (INSERT-time), no
        # onupdate — so a resubmission kept the ORIGINAL submission date.
        # Staff scanning KYC Management (sorted by submitted_at desc) would
        # never see the resubmission surface to the top, and the displayed
        # "Submitted At" date was misleadingly stale. Bump it explicitly.
        kyc.submitted_at = datetime.now(timezone.utc)
    else:
        # First submission
        kyc = KycSubmission(
            user_id=current_user.id,
            status="pending",
            **submitted_data,
        )
        db.add(kyc)

    # Update user's kyc_status — and account_type, if the client actually
    # chose one on this submission. Previously this only updated
    # kyc_status, so account_type silently never changed from whatever it
    # was at signup, regardless of what was submitted here (the root cause
    # of the admin panel always showing "Individual").
    user_update_values = {"kyc_status": "pending"}
    if submitted_account_type:
        user_update_values["account_type"] = submitted_account_type

    await db.execute(
        update(User)
        .where(User.id == current_user.id)
        .values(**user_update_values)
    )

    await db.flush()

    # ── TODO: Push to D365 via Celery background task ─────────────────────
    # Uncomment when Celery + D365 are configured:
    # from app.tasks.d365_tasks import push_kyc_to_d365
    # push_kyc_to_d365.delay(
    #     kyc_id=str(kyc.id),
    #     user_full_name=current_user.full_name,
    #     user_email=current_user.email,
    #     account_type=current_user.account_type,
    #     submitted_at=kyc.submitted_at.isoformat(),
    # )
    # Until then, admin manually reviews via admin panel → KYC Management

    await db.commit()
    await db.refresh(kyc)

    # Fire workflow trigger — creates a WorkflowInstance if KYC Approval
    # workflow is configured and active. Silent if not configured yet.
    await _fire_workflow(
        trigger="kyc_submitted",
        record_type="kyc",
        record_id=kyc.id,
        client_id=current_user.id,
        client_name=current_user.full_name,
        db=db,
    )

    # Email staff qualified to approve KYC. Independent of whether a
    # workflow is configured — this fires regardless, so submissions are
    # never silently invisible to staff even if My Tasks has nothing set up.
    await _notify_staff_kyc_submitted(
        client_name=current_user.full_name,
        client_email=current_user.email,
        is_resubmission=is_resubmission,
        db=db,
    )

    await db.commit()

    logger.info(f"KYC submitted by {current_user.email}")
    return kyc


@router.get(
    "/status",
    response_model=KycResponse,
    summary="Get current KYC status",
)
async def get_kyc_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the current user's KYC submission status.
    Frontend: Used by KycBanner.jsx and Dashboard to show KYC status.
    """
    result = await db.execute(
        select(KycSubmission).where(KycSubmission.user_id == current_user.id)
    )
    kyc = result.scalar_one_or_none()

    if not kyc:
        raise HTTPException(
            status_code=404,
            detail="No KYC submission found. Please submit your KYC."
        )
    return kyc


@router.post(
    "/documents/upload",
    response_model=MessageResponse,
    summary="Upload a KYC document",
)
async def upload_kyc_document(
    request: Request,
    document_type: str,  # id_document | utility_bill | passport_photo | cac_certificate | board_resolution | memorandum | scuml_certificate | tin_certificate
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Upload a KYC document (ID, utility bill, passport photo, etc.).

    Files are saved to local disk (app/static/kyc-documents/) and served
    via the /static mount — a real, working solution for local/dev use.
    NOT yet swapped for cloud storage (Azure Blob, S3, etc.) — that's a
    Phase 2 (deployment) task. When that happens, only this function needs
    to change; every URL already saved/returned stays a normal URL either
    way, so nothing downstream (KYC Management, PDF export, etc.) needs to
    know or care where the file actually lives.
    """
    # Validate file type
    allowed_types = ["application/pdf", "image/jpeg", "image/png", "image/jpg"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Please upload PDF, JPG, or PNG only."
        )

    # Validate file size (max 5MB)
    max_size = 5 * 1024 * 1024  # 5MB
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(
            status_code=400,
            detail="File too large. Maximum size is 5MB."
        )

    # ── Save via the storage layer ──────────────────────────────────────────
    # Routes to local disk in development and cloud storage in production
    # (see app/core/storage.py). Previously wrote straight to local disk,
    # which is wiped on redeploy on most cloud hosts, so real client KYC
    # documents would have vanished after any deployment.
    safe_filename = build_filename(f"{current_user.id}_{document_type}", file.filename, file.content_type)
    blob_url = save_file(
        content=content, folder="kyc-documents", filename=safe_filename,
        content_type=file.content_type, request_base_url=str(request.base_url),
    )

    # Save URL to KYC submission
    valid_types = {
        "id_document": "id_document_url",
        "utility_bill": "utility_bill_url",
        "passport_photo": "passport_photo_url",
        "cac_certificate": "cac_certificate_url",
        "board_resolution": "board_resolution_url",
        "memorandum": "memorandum_url",
        "scuml_certificate": "scuml_certificate_url",  # NEW — was collected on the form but had nowhere to go
        "tin_certificate": "tin_certificate_url",      # NEW — same as above
        "board_resolution_director_signature": "board_resolution_director_signature_url",  # NEW
        "board_resolution_secretary_signature": "board_resolution_secretary_signature_url",  # NEW
        "minor_passport_photo": "minor_passport_photo_url",  # NEW
        "minor_birth_certificate": "minor_birth_certificate_url",  # NEW
        "joint_passport_photo": "joint_passport_photo_url",  # NEW
        "joint_id_document": "joint_id_document_url",  # NEW
    }

    # Per-signatory uploads (photo/signature for Signatory A-D) — these
    # don't map to a flat column since there can be up to 4 signatories.
    # NEW — document_type looks like "signatory_0_photo" or
    # "signatory_2_signature"; stored inside the existing signatories JSON
    # array instead of a dedicated column. Handled separately below rather
    # than via the flat valid_types lookup.
    signatory_match = re.match(r"^signatory_(\d)_(photo|signature)$", document_type)

    if document_type not in valid_types and not signatory_match:
        raise HTTPException(status_code=400, detail=f"Invalid document type: {document_type}")

    result = await db.execute(
        select(KycSubmission).where(KycSubmission.user_id == current_user.id)
    )
    kyc = result.scalar_one_or_none()

    if not kyc:
        raise HTTPException(status_code=404, detail="Please submit your KYC form first.")

    if signatory_match:
        idx, kind = int(signatory_match.group(1)), signatory_match.group(2)
        signatories = list(kyc.signatories or [])
        while len(signatories) <= idx:
            signatories.append({})
        signatories[idx] = {
            **signatories[idx],
            ("passport_photo_url" if kind == "photo" else "signature_url"): blob_url,
        }
        kyc.signatories = signatories
    else:
        setattr(kyc, valid_types[document_type], blob_url)

    await db.commit()

    logger.info(f"KYC document uploaded: {document_type} for {current_user.email}")
    return MessageResponse(message=f"{document_type} uploaded successfully.")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.get(
    "",
    summary="Admin: List all KYC submissions",
)
async def list_kyc_submissions(
    status_filter: str = None,  # pending | approved | denied
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),  # any logged-in staff can VIEW — actions stay permission-gated
):
    """
    List all KYC submissions with client details joined.
    Admin panel → KYC Management section.
    """
    query = (
        select(KycSubmission, User)
        .join(User, KycSubmission.user_id == User.id)
    )
    if status_filter:
        query = query.where(KycSubmission.status == status_filter)
    query = query.order_by(KycSubmission.submitted_at.desc())

    result = await db.execute(query)
    rows = result.all()

    # Build enriched response
    enriched = []
    for kyc, user in rows:
        enriched.append({
            # KYC meta
            "id":             str(kyc.id),
            "kyc_id":         str(kyc.id),
            "user_id":        str(kyc.user_id),
            "status":         kyc.status,
            "kyc_status":     kyc.status,
            "denial_reason":  kyc.denial_reason,
            "d365_synced":    kyc.d365_synced,
            "d365_synced_at": str(kyc.d365_synced_at) if kyc.d365_synced_at else None,
            "submitted_at":   kyc.submitted_at,
            "reviewed_at":    kyc.reviewed_at,
            "reviewed_by":    kyc.reviewed_by,
            # Client info
            "full_name":    user.full_name,
            "email":        user.email,
            "account_type": user.account_type,
            "phone":        user.phone,
            # Personal KYC fields
            "date_of_birth":         str(kyc.date_of_birth) if kyc.date_of_birth else None,
            "nationality":           kyc.nationality,
            "state":                 kyc.state,
            "lga":                   kyc.lga,
            "address":               kyc.address,
            "occupation":            kyc.occupation,
            "employer":              kyc.employer,
            "annual_income":         kyc.annual_income,
            "investment_experience": kyc.investment_experience,
            "risk_profile":          kyc.risk_profile,
            "pep_status":            kyc.pep_status,
            # Corporate fields
            "company_name":    kyc.company_name,
            "rc_number":       kyc.rc_number,
            "company_address": kyc.company_address,
            "signatories":     kyc.signatories,
            # Document URLs
            "id_document_url":      kyc.id_document_url,
            "utility_bill_url":     kyc.utility_bill_url,
            "passport_photo_url":   kyc.passport_photo_url,
            "cac_certificate_url":  kyc.cac_certificate_url,
            "board_resolution_url": kyc.board_resolution_url,
            "memorandum_url":       kyc.memorandum_url,
            "scuml_certificate_url": kyc.scuml_certificate_url,
            "tin_certificate_url":   kyc.tin_certificate_url,
            "board_resolution_director_signature_url": kyc.board_resolution_director_signature_url,
            "board_resolution_secretary_signature_url": kyc.board_resolution_secretary_signature_url,
            "minor_passport_photo_url": kyc.minor_passport_photo_url,
            "minor_birth_certificate_url": kyc.minor_birth_certificate_url,
            "joint_passport_photo_url": kyc.joint_passport_photo_url,
            "joint_id_document_url": kyc.joint_id_document_url,
            # Extra data — all additional form fields
            "extra_data": kyc.extra_data,
        })
    return enriched


@admin_router.get(
    "/{kyc_id}/document",
    summary="Stream a KYC document through an authenticated request",
)
async def get_kyc_document(
    kyc_id: UUID,
    doc_type: str,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """
    Serves a KYC document only to an authenticated staff member.

    SECURITY: previously the stored file URL was handed straight to the
    browser. With cloud storage that URL is publicly reachable by anyone who
    has it, with no login required, which for passports and utility bills is
    not acceptable. Streaming through this endpoint means the file is only
    ever released to a logged-in staff member, and the underlying storage
    location is never exposed.

    Works the same for local disk and cloud storage, so no behaviour changes
    between development and production.
    """
    valid = {
        "id_document": "id_document_url",
        "utility_bill": "utility_bill_url",
        "passport_photo": "passport_photo_url",
        "cac_certificate": "cac_certificate_url",
        "board_resolution": "board_resolution_url",
        "memorandum": "memorandum_url",
        "scuml_certificate": "scuml_certificate_url",
        "tin_certificate": "tin_certificate_url",
        "board_resolution_director_signature": "board_resolution_director_signature_url",
        "board_resolution_secretary_signature": "board_resolution_secretary_signature_url",
        "minor_passport_photo": "minor_passport_photo_url",
        "minor_birth_certificate": "minor_birth_certificate_url",
        "joint_passport_photo": "joint_passport_photo_url",
        "joint_id_document": "joint_id_document_url",
    }

    result = await db.execute(select(KycSubmission).where(KycSubmission.id == kyc_id))
    kyc = result.scalar_one_or_none()
    if not kyc:
        raise HTTPException(status_code=404, detail="KYC submission not found.")

    # Per-signatory documents live in the signatories JSON rather than a column
    sig_match = re.match(r"^signatory_(\d)_(photo|signature)$", doc_type)
    if sig_match:
        idx, kind = int(sig_match.group(1)), sig_match.group(2)
        sigs = kyc.signatories or []
        if idx >= len(sigs):
            raise HTTPException(status_code=404, detail="Signatory not found.")
        url = sigs[idx].get("passport_photo_url" if kind == "photo" else "signature_url")
    elif doc_type in valid:
        url = getattr(kyc, valid[doc_type], None)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown document type: {doc_type}")

    if not url:
        raise HTTPException(status_code=404, detail="That document has not been uploaded.")

    content, content_type = read_file_bytes(url)
    if content is None:
        raise HTTPException(status_code=404, detail="The stored file could not be found.")

    logger.info(f"KYC document '{doc_type}' viewed by {admin.email} for submission {kyc_id}")
    return StreamingResponse(
        io.BytesIO(content),
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{doc_type}{Path(url.split("?")[0]).suffix}"'},
    )


@admin_router.patch(
    "/{kyc_id}/override",
    response_model=MessageResponse,
    summary="Admin: Manually approve or deny a KYC submission",
)
async def override_kyc(
    kyc_id: UUID,
    body: KycAdminOverride,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("kyc")),
):
    """
    Manually override KYC status.

    Used when:
      - D365 is not yet configured (admin manually approves/denies)
      - Admin needs to override a D365 decision
      - Correcting a mistaken approval or denial

    This is the main action in admin panel → KYC Management section.
    Every override is logged to the audit trail.
    """
    if body.status not in ["approved", "denied"]:
        raise HTTPException(status_code=400, detail="Status must be 'approved' or 'denied'.")

    if body.status == "denied" and not body.reason:
        raise HTTPException(status_code=400, detail="A reason is required when denying KYC.")

    # Find KYC submission
    result = await db.execute(select(KycSubmission).where(KycSubmission.id == kyc_id))
    kyc = result.scalar_one_or_none()
    if not kyc:
        raise HTTPException(status_code=404, detail="KYC submission not found.")

    # Update KYC record
    kyc.status = body.status
    kyc.denial_reason = body.reason if body.status == "denied" else None
    kyc.reviewed_at = datetime.now(timezone.utc)
    kyc.reviewed_by = f"Admin: {admin.full_name}"

    # Update user's kyc_status
    await db.execute(
        update(User)
        .where(User.id == kyc.user_id)
        .values(
            kyc_status=body.status,
            kyc_denied_reason=body.reason if body.status == "denied" else None,
        )
    )

    # Send in-app notification to client
    if body.status == "approved":
        notif_title = "KYC Approved ✓"
        notif_message = "Your KYC verification has been approved. You can now subscribe to investment products."
    else:
        notif_title = "KYC Requires Attention"
        notif_message = f"Your KYC was not approved. Reason: {body.reason}. Please resubmit with the correct information."

    notification = Notification(
        user_id=kyc.user_id,
        title=notif_title,
        message=notif_message,
        notification_type="kyc",
    )
    db.add(notification)

    # Also email the client. Previously this was in-portal only, so someone
    # denied at KYC found out only if they happened to log in again.
    client_result = await db.execute(select(User).where(User.id == kyc.user_id))
    client = client_result.scalar_one_or_none()
    if client:
        send_kyc_decision(
            to=client.email, full_name=client.full_name,
            approved=(body.status == "approved"), reason=body.reason or "",
        )

    # Audit log — every admin action is recorded
    audit = AuditLog(
        action=f"KYC {body.status.title()} (Admin Override)",
        target_id=str(kyc_id),
        action_type="kyc",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details={"status": body.status, "reason": body.reason},
    )
    db.add(audit)

    # ── Sync the workflow instance (was missing) ────────────────────────────
    # A direct override here bypasses the step-by-step workflow entirely, but
    # if a WorkflowInstance exists and is still pending/in_progress for this
    # KYC, it needs to be closed out too — otherwise it just sits in My Tasks
    # forever showing "still needs approval" even though it's already been
    # resolved. This is the mirror-image of the sync added to the workflow
    # engine's own approve/reject action (see workflows.py).
    wf_result = await db.execute(
        select(WorkflowInstance).where(
            WorkflowInstance.record_type == "kyc",
            WorkflowInstance.record_id == kyc_id,
            WorkflowInstance.status.in_(["pending", "in_progress"]),
        )
    )
    wf_instance = wf_result.scalar_one_or_none()
    if wf_instance:
        wf_instance.status = "completed" if body.status == "approved" else "rejected"
        wf_instance.completed_at = datetime.now(timezone.utc)
        wf_instance.step_history = (wf_instance.step_history or []) + [{
            "step": wf_instance.current_step,
            "step_name": "Direct Override",
            "action": "approve" if body.status == "approved" else "reject",
            "by": admin.full_name,
            "by_id": str(admin.id),
            "at": datetime.now(timezone.utc).isoformat(),
            "note": f"Resolved directly via KYC Management (bypassed remaining workflow steps). {body.reason or ''}".strip(),
        }]

    await db.commit()

    # TODO: Send email via SendGrid
    # from app.tasks.email_tasks import send_kyc_decision_email
    # send_kyc_decision_email.delay(user_email, user_name, body.status, body.reason)

    logger.info(f"KYC {kyc_id} {body.status} by admin {admin.email}")
    return MessageResponse(message=f"KYC has been {body.status}.")
