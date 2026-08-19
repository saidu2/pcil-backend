# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/workflows.py  (NEW in v11)
#
# Workflow engine — configuration and instance management.
#
# IT Admin only (can_configure_workflows) — workflow config:
#   GET    /api/v1/admin/workflows                    — list all workflows
#   POST   /api/v1/admin/workflows                    — create workflow + steps
#   GET    /api/v1/admin/workflows/{id}               — get single workflow
#   PATCH  /api/v1/admin/workflows/{id}               — update workflow
#   DELETE /api/v1/admin/workflows/{id}               — delete workflow
#   POST   /api/v1/admin/workflows/{id}/steps         — add a step
#   DELETE /api/v1/admin/workflows/{id}/steps/{step_id} — remove a step
#
# Any staff (own-permission gated) — My Tasks:
#   GET    /api/v1/admin/workflows/instances/my-tasks — tasks for current admin
#   PATCH  /api/v1/admin/workflows/instances/{id}/action — act on a task
#
# KYC PDF export (any staff with can_approve_kyc):
#   GET    /api/v1/admin/kyc/{kyc_id}/export-pdf     — download KYC as PDF
#   (note: mounted separately in main.py under /admin/kyc, not /admin/workflows)
# ─────────────────────────────────────────────────────────────────────────────

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.storage import read_local_path, read_file_bytes
from app.db.session import get_db
from app.models.models import (
    Workflow, WorkflowStep, WorkflowInstance,
    AdminUser, AuditLog, StaffRole,
    KycSubmission, User, Notification,
)
from app.schemas.schemas import (
    WorkflowCreate, WorkflowUpdate, WorkflowResponse,
    WorkflowStepCreate, WorkflowStepResponse, WorkflowStepUpdate,
    WorkflowInstanceResponse, WorkflowStepAction,
    MessageResponse,
)
from app.api.v1.deps import get_current_admin, require_staff_permission

logger = logging.getLogger(__name__)
router = APIRouter()     # mounted at /api/v1/admin/workflows


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _get_admin_staff_role(admin: AdminUser, db: AsyncSession) -> StaffRole | None:
    """Load the StaffRole for an admin — used for permission checks in tasks."""
    if not admin.staff_role_id:
        return None
    result = await db.execute(select(StaffRole).where(StaffRole.id == admin.staff_role_id))
    return result.scalar_one_or_none()


async def _admin_can_handle_step(admin: AdminUser, step: WorkflowStep, db: AsyncSession) -> bool:
    """True if admin has the permission flag required by this step, or is super_admin."""
    if admin.role == "super_admin":
        return True
    role = await _get_admin_staff_role(admin, db)
    if not role or not role.is_active:
        return False
    return bool(getattr(role, step.required_permission, False))


def _enrich_instance(inst: WorkflowInstance, workflow: Workflow) -> dict:
    """Add workflow_name and current_step_name to instance for the task list."""
    current_step_obj = next(
        (s for s in workflow.steps if s.step_order == inst.current_step), None
    )
    return {
        "id": str(inst.id),
        "workflow_id": str(inst.workflow_id),
        "workflow_name": workflow.name,
        "record_type": inst.record_type,
        "record_id": str(inst.record_id),
        "client_id": str(inst.client_id) if inst.client_id else None,
        "client_name": inst.client_name or "—",
        "current_step": inst.current_step,
        "current_step_name": current_step_obj.name if current_step_obj else "—",
        "current_step_actions": current_step_obj.available_actions if current_step_obj else "",
        "status": inst.status,
        "step_history": inst.step_history or [],
        "created_at": inst.created_at,
        "updated_at": inst.updated_at,
        "completed_at": inst.completed_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW CONFIG ENDPOINTS (IT Admin / can_configure_workflows)
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=list[WorkflowResponse],
    summary="List all workflows — can_configure_workflows",
)
async def list_workflows(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_staff_permission("can_configure_workflows")),
):
    result = await db.execute(
        select(Workflow).options(selectinload(Workflow.steps)).order_by(Workflow.created_at)
    )
    return result.scalars().all()


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow with steps — can_configure_workflows",
)
async def create_workflow(
    body: WorkflowCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_configure_workflows")),
):
    if body.trigger not in ["kyc_submitted", "subscription_created", "redemption_requested", "client_account_created"]:
        raise HTTPException(status_code=400, detail=f"Invalid trigger: {body.trigger}")

    workflow = Workflow(name=body.name, trigger=body.trigger, description=body.description)
    db.add(workflow)
    await db.flush()

    for s in body.steps:
        db.add(WorkflowStep(workflow_id=workflow.id, **s.model_dump()))

    audit = AuditLog(
        action="Workflow Created", target=body.name, target_id=str(workflow.id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"trigger": body.trigger, "steps": len(body.steps)},
    )
    db.add(audit)
    await db.commit()

    result = await db.execute(
        select(Workflow).options(selectinload(Workflow.steps)).where(Workflow.id == workflow.id)
    )
    logger.info(f"Workflow '{body.name}' created by {admin.email}")
    return result.scalar_one()


@router.get(
    "/instances/my-tasks",
    summary="My Tasks — workflow instances where current step matches my permissions",
)
async def my_tasks(
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Returns active workflow instances where the current admin has the
    permission required to handle the current step. Super admin sees all.
    Drives the My Tasks section in the Admin Panel.
    """
    result = await db.execute(
        select(WorkflowInstance)
        .options(selectinload(WorkflowInstance.workflow).selectinload(Workflow.steps))
        .where(WorkflowInstance.status.in_(["pending", "in_progress"]))
        .order_by(WorkflowInstance.created_at)
    )
    instances = result.scalars().all()

    tasks = []
    for inst in instances:
        workflow = inst.workflow
        current_step_obj = next(
            (s for s in workflow.steps if s.step_order == inst.current_step), None
        )
        if not current_step_obj:
            continue
        can_handle = await _admin_can_handle_step(admin, current_step_obj, db)
        if can_handle:
            tasks.append(_enrich_instance(inst, workflow))

    return tasks


@router.get(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Get a single workflow — can_configure_workflows",
)
async def get_workflow(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_staff_permission("can_configure_workflows")),
):
    result = await db.execute(
        select(Workflow).options(selectinload(Workflow.steps)).where(Workflow.id == workflow_id)
    )
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return wf


@router.patch(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Update a workflow — can_configure_workflows",
)
async def update_workflow(
    workflow_id: UUID,
    body: WorkflowUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_configure_workflows")),
):
    result = await db.execute(select(Workflow).where(Workflow.id == workflow_id))
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found.")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(wf, field, value)

    audit = AuditLog(
        action="Workflow Updated", target=wf.name, target_id=str(workflow_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details=body.model_dump(exclude_unset=True),
    )
    db.add(audit)
    await db.commit()

    result = await db.execute(
        select(Workflow).options(selectinload(Workflow.steps)).where(Workflow.id == workflow_id)
    )
    return result.scalar_one()


@router.delete(
    "/{workflow_id}",
    response_model=MessageResponse,
    summary="Delete a workflow — can_configure_workflows",
)
async def delete_workflow(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_configure_workflows")),
):
    result = await db.execute(select(Workflow).where(Workflow.id == workflow_id))
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found.")

    name = wf.name
    await db.delete(wf)
    audit = AuditLog(
        action="Workflow Deleted", target=name, target_id=str(workflow_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
    )
    db.add(audit)
    await db.commit()
    return MessageResponse(message=f"Workflow '{name}' deleted.")


@router.post(
    "/{workflow_id}/steps",
    response_model=WorkflowStepResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a step to a workflow — can_configure_workflows",
)
async def add_workflow_step(
    workflow_id: UUID,
    body: WorkflowStepCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_configure_workflows")),
):
    result = await db.execute(select(Workflow).where(Workflow.id == workflow_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Workflow not found.")

    step = WorkflowStep(workflow_id=workflow_id, **body.model_dump())
    db.add(step)
    await db.commit()
    await db.refresh(step)
    return step


@router.patch(
    "/{workflow_id}/steps/{step_id}",
    response_model=WorkflowStepResponse,
    summary="Edit a workflow step — can_configure_workflows",
)
async def update_workflow_step(
    workflow_id: UUID,
    step_id: UUID,
    body: WorkflowStepUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_configure_workflows")),
):
    """
    Corrects a step in place. Previously the only option was to delete and
    re-add it, which lost its position in the sequence and meant retyping
    everything to fix one field.

    Changes apply to instances already in progress, since the step definition
    is read fresh each time someone acts on a task. That's usually what you
    want (fixing a typo, adjusting an SLA), but be aware when changing
    required_permission: anyone mid-flow at that step will immediately need
    the new permission instead of the old one.
    """
    result = await db.execute(
        select(WorkflowStep).where(
            WorkflowStep.id == step_id, WorkflowStep.workflow_id == workflow_id
        )
    )
    step = result.scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found.")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")

    before = {k: getattr(step, k) for k in update_data}
    for field, value in update_data.items():
        setattr(step, field, value)

    db.add(AuditLog(
        action="Workflow Step Updated", target=step.name, target_id=str(step_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"before": before, "after": update_data},
    ))
    await db.commit()
    await db.refresh(step)
    return step


@router.delete(
    "/{workflow_id}/steps/{step_id}",
    response_model=MessageResponse,
    summary="Remove a step from a workflow — can_configure_workflows",
)
async def delete_workflow_step(
    workflow_id: UUID,
    step_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_configure_workflows")),
):
    result = await db.execute(
        select(WorkflowStep).where(
            WorkflowStep.id == step_id, WorkflowStep.workflow_id == workflow_id
        )
    )
    step = result.scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Step not found.")
    await db.delete(step)
    await db.commit()
    return MessageResponse(message="Step removed.")


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW INSTANCE ACTIONS (My Tasks — permission gated per step)
# ─────────────────────────────────────────────────────────────────────────────

@router.patch(
    "/instances/{instance_id}/action",
    summary="Act on a workflow task — approve/reject/escalate/request_info",
)
async def workflow_instance_action(
    instance_id: UUID,
    body: WorkflowStepAction,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Staff takes an action on the current step of a workflow instance.
    The available actions are defined per step (e.g. approve, reject,
    escalate, request_info). Advancing the step automatically moves to
    the next step; completing the last step marks the instance as done.
    """
    result = await db.execute(
        select(WorkflowInstance)
        .options(selectinload(WorkflowInstance.workflow).selectinload(Workflow.steps))
        .where(WorkflowInstance.id == instance_id)
    )
    inst = result.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="Workflow instance not found.")

    if inst.status not in ["pending", "in_progress"]:
        raise HTTPException(status_code=400, detail=f"Instance is already {inst.status}.")

    workflow = inst.workflow
    current_step_obj = next(
        (s for s in workflow.steps if s.step_order == inst.current_step), None
    )
    if not current_step_obj:
        raise HTTPException(status_code=400, detail="Current step not found in workflow definition.")

    # Permission check — must have the flag this step requires
    can_handle = await _admin_can_handle_step(admin, current_step_obj, db)
    if not can_handle:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You don't have permission to handle this step ({current_step_obj.name}).",
        )

    # Validate action is allowed for this step
    allowed = [a.strip() for a in current_step_obj.available_actions.split(",")]
    if body.action not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Action '{body.action}' is not available for this step. Allowed: {', '.join(allowed)}",
        )

    # Record the action in step history
    history_entry = {
        "step": inst.current_step,
        "step_name": current_step_obj.name,
        "action": body.action,
        "by": admin.full_name,
        "by_id": str(admin.id),
        "at": datetime.now(timezone.utc).isoformat(),
        "note": body.note or "",
    }
    inst.step_history = (inst.step_history or []) + [history_entry]

    if body.action in ["reject", "escalate"]:
        inst.status = body.action + "d" if body.action == "reject" else "escalated"
        inst.completed_at = datetime.now(timezone.utc)
    elif body.action == "request_info":
        inst.status = "in_progress"  # stays open, awaiting client response
    else:
        # approve — advance to next step or complete
        next_step_obj = next(
            (s for s in workflow.steps if s.step_order == inst.current_step + 1), None
        )
        if next_step_obj:
            inst.current_step += 1
            inst.status = "in_progress"
        else:
            inst.status = "completed"
            inst.completed_at = datetime.now(timezone.utc)

    inst.updated_at = datetime.now(timezone.utc)

    # ── Sync back to the underlying record (NEW — was missing) ─────────────
    # A workflow instance tracks the APPROVAL PROCESS, but the actual
    # KYC/subscription/redemption record needs its own status updated too,
    # or the rest of the app (KYC Management, client dashboard, etc.) never
    # finds out anything happened. Only syncs on a final, unambiguous
    # outcome — a rejection at any step, or approval of the LAST step.
    # Intermediate step approvals (e.g. Operations approves KYC step 1 of 3)
    # deliberately do NOT touch the record yet — it's still mid-process.
    if inst.record_type == "kyc":
        kyc_result = await db.execute(select(KycSubmission).where(KycSubmission.id == inst.record_id))
        kyc_record = kyc_result.scalar_one_or_none()
        if kyc_record:
            # BUGFIX: this previously only updated KycSubmission.status —
            # which is what staff/KYC Management reads — but never touched
            # User.kyc_status, which is the SEPARATE field the CLIENT's own
            # dashboard reads (via /auth/me). Direct KYC Management denial
            # (override_kyc, above) already updates both; a My Tasks
            # rejection was silently only updating one, so the client kept
            # seeing "under review" forever with no way to know they'd been
            # denied or that they needed to resubmit. Now updates both,
            # exactly matching override_kyc's behavior, plus the same
            # client notification.
            if body.action == "reject":
                reason = body.note or "Denied during workflow review."
                kyc_record.status = "denied"
                kyc_record.denial_reason = reason
                kyc_record.reviewed_at = datetime.now(timezone.utc)
                kyc_record.reviewed_by = f"{current_step_obj.name}: {admin.full_name}"

                await db.execute(
                    update(User).where(User.id == kyc_record.user_id).values(
                        kyc_status="denied", kyc_denied_reason=reason,
                    )
                )
                db.add(Notification(
                    user_id=kyc_record.user_id,
                    title="KYC Requires Attention",
                    message=f"Your KYC was not approved. Reason: {reason}. Please resubmit with the correct information.",
                    notification_type="kyc",
                ))
            elif body.action == "approve" and inst.status == "completed":
                kyc_record.status = "approved"
                kyc_record.denial_reason = None
                kyc_record.reviewed_at = datetime.now(timezone.utc)
                kyc_record.reviewed_by = f"{current_step_obj.name}: {admin.full_name}"

                await db.execute(
                    update(User).where(User.id == kyc_record.user_id).values(
                        kyc_status="approved", kyc_denied_reason=None,
                    )
                )
                db.add(Notification(
                    user_id=kyc_record.user_id,
                    title="KYC Approved ✓",
                    message="Your KYC verification has been approved. You can now subscribe to investment products.",
                    notification_type="kyc",
                ))
    # NOTE: subscription/redemption record sync is intentionally NOT done
    # here yet — activating a subscription needs a maturity_date the generic
    # workflow action doesn't collect, and completing a redemption has its
    # own payout bookkeeping (see subscription_action / process_redemption).
    # Replicating that safely needs more than a status flip — until that's
    # built, use the existing Subscriptions/Redemptions admin sections for
    # the actual activation/payout step; the workflow instance still tracks
    # and audits the approval chain correctly, it just doesn't yet trigger
    # the side effects those two record types need.

    audit = AuditLog(
        action=f"Workflow Step {body.action.title()}d",
        target=f"{inst.record_type}/{inst.record_id}",
        target_id=str(instance_id),
        action_type="admin",
        performed_by=admin.id,
        performed_by_name=admin.full_name,
        details=history_entry,
    )
    db.add(audit)
    await db.commit()

    logger.info(
        f"Workflow instance {instance_id} step {inst.current_step - 1} "
        f"{body.action}d by {admin.email}"
    )
    return _enrich_instance(inst, workflow)


# ─────────────────────────────────────────────────────────────────────────────
# KYC PDF EXPORT (mounted separately under /admin/kyc in main.py)
# ─────────────────────────────────────────────────────────────────────────────

kyc_export_router = APIRouter()

@kyc_export_router.get(
    "/{kyc_id}/export-pdf",
    summary="Download KYC submission as PDF — styled to match the official PCI account opening form",
)
async def export_kyc_pdf(
    kyc_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Generates a PDF styled to match Prime Capital's actual branded account
    opening form (gold section header bars, same field labels/order as the
    physical form) — so a portal submission and a scanned physical form
    look and read the same way to staff, regardless of which channel the
    client used.

    account type comes from extra_data['account_type'] (capitalized:
    'Individual'/'Minor'/'Joint'/'Corporate') — NOT the flat
    user.account_type column, which only ever distinguishes
    'individual'/'corporate' and can't tell Minor or Joint apart.

    Requires: pip install reportlab
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, Image, KeepTogether
    from reportlab.lib.utils import ImageReader
    from pypdf import PdfReader, PdfWriter
    from pathlib import Path
    import io

    result = await db.execute(
        select(KycSubmission, User)
        .join(User, KycSubmission.user_id == User.id)
        .where(KycSubmission.id == kyc_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="KYC submission not found.")
    kyc, user = row

    extra = kyc.extra_data or {}
    acct_type = extra.get("account_type") or (user.account_type or "Individual").capitalize()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
    )

    styles = getSampleStyleSheet()
    GOLD = colors.HexColor("#A67C1A")
    LIGHT_GOLD = colors.HexColor("#faf6ec")

    title_style = ParagraphStyle("Title", parent=styles["Title"], textColor=GOLD, fontSize=20, spaceAfter=2, fontName="Helvetica-Bold", alignment=1)  # alignment=1 (TA_CENTER) — logo, title, subtitle, and meta line are now a single centered stack
    subtitle_style = ParagraphStyle("Sub", parent=styles["Normal"], textColor=colors.grey, fontSize=8, spaceAfter=2, alignment=1)
    meta_style = ParagraphStyle("Meta", parent=styles["Normal"], textColor=colors.grey, fontSize=8, spaceAfter=10, alignment=1)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=9, leading=13)
    label_style = ParagraphStyle("Label", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#555555"), leading=11)
    value_style = ParagraphStyle("Value", parent=styles["Normal"], fontSize=9.5, textColor=colors.black, leading=13, fontName="Helvetica-Bold")
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7, textColor=colors.grey, spaceBefore=4)
    static_style = ParagraphStyle("Static", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#666666"), leading=12, spaceAfter=6)

    PAGE_W = 174 * mm  # usable width

    def gold_bar(text):
        """A filled gold header bar spanning the page width, matching the
        physical form's section headers (e.g. 'A. CLIENT PERSONAL DATA')."""
        t = Table([[Paragraph(f"<b>{text}</b>", ParagraphStyle("BarText", fontSize=10, textColor=colors.white, fontName="Helvetica-Bold"))]], colWidths=[PAGE_W])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), GOLD),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ]))
        return t

    def field_grid(pairs, col_widths=None):
        """
        pairs: list of rows, each row a list of (label, value) tuples —
        1, 2, or 3 fields per row, matching the physical form's field
        groupings (e.g. [("Title", x), ("Surname", y)] on one line).
        """
        n = max(len(r) for r in pairs)
        if not col_widths:
            col_widths = [PAGE_W / n] * n
        data = []
        for row_pairs in pairs:
            row = []
            for label, value in row_pairs:
                cell = [Paragraph(esc(label), label_style), Paragraph(esc(value), value_style)]
                row.append(cell)
            while len(row) < n:
                row.append("")
            data.append(row)
        t = Table(data, colWidths=col_widths[:n])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d9d0b8")),
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GOLD),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    def esc(value):
        """
        Escapes a user-supplied value before it goes into a ReportLab
        Paragraph. Paragraph parses a subset of HTML markup, so a client whose
        name or address contains something like "John <b>Doe" (unclosed tag)
        would otherwise crash the entire PDF export with a parse error, and
        staff would see only a failure with no explanation.
        """
        if value is None or value == "":
            return "—"
        return (str(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

    def choice_line(label, value):
        """One line showing a checkbox-style choice's selected value in bold, e.g. 'Gender: Male'"""
        return Paragraph(f"<b>{esc(label)}:</b> {esc(value)}", body_style)

    def format_amount(raw):
        """
        'Amount in figures' is a free-text field on the form, not strictly
        numeric — a client could type '5,000,000' with commas, or non-digit
        text. Never let a bad value crash the whole PDF export; just show
        it as typed if it can't be parsed as a number.
        """
        if not raw:
            return "—"
        try:
            cleaned = str(raw).replace(",", "").replace("₦", "").strip()
            return f"₦{float(cleaned):,.2f}"
        except (ValueError, TypeError):
            return str(raw)

    elements = []

    # ── Letterhead: logo, title, subtitle, meta — all centered, stacked ─────
    # Logo expected at app/static/pci_logo.png — falls back to text-only
    # header if it's not there yet, so this never crashes PDF generation.
    logo_path = Path(__file__).resolve().parents[3] / "static" / "pci_logo.png"
    if logo_path.exists():
        logo_img = Image(str(logo_path), width=22 * mm, height=22 * mm)
        logo_img.hAlign = "CENTER"
        elements.append(logo_img)
        elements.append(Spacer(1, 6))
    elements.append(Paragraph("PRIME CAPITAL &amp; INVESTMENT LTD", title_style))
    elements.append(Paragraph(f"{acct_type} Account Opening Form (Portal Submission)", subtitle_style))
    elements.append(Spacer(1, 4))
    elements.append(Paragraph(
        f"Generated {datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')} by {admin.full_name} &nbsp;|&nbsp; "
        f"Status: <b>{kyc.status.upper()}</b> &nbsp;|&nbsp; Submitted: {kyc.submitted_at.strftime('%d %b %Y') if kyc.submitted_at else '—'}",
        meta_style,
    ))
    elements.append(Spacer(1, 6))

    # ── Document helpers — defined here so every section below (personal
    # data, signatories, board resolution, etc.) can embed a photo or
    # signature INLINE, right next to the person/section it belongs to —
    # matching the physical form's layout (e.g. "Afix Passport Photograph"
    # sits directly beside that signatory's own details, not lumped
    # together in one block at the very end of the document). Certificates
    # (CAC, SCUML, TIN) are handled separately, later, since the physical
    # form doesn't have an inline box for those — they're attachments.
    temp_rendered_files = []  # PNGs rendered from PDF pages — cleaned up after build
    pdf_attachments = []      # (label, Path) — genuine certificates merged as extra pages

    def resolve_local_path(url: str):
        """
        Returns a readable local path for an uploaded file so it can be
        embedded into this PDF server-side.

        Local storage: maps the URL straight back to its path on disk.
        Cloud storage (Supabase): downloads to a temp file first, since
        there's no local copy to read. Without this, switching to cloud
        storage would silently produce PDFs with every image missing.

        Returns None if the file can't be reached, so callers fall back to
        showing a link rather than failing the whole export.
        """
        if not url:
            return None

        # Local file, if this deployment stores locally
        try:
            local = read_local_path(url)
            if local:
                return local
        except Exception:
            pass

        if "/static/" in url:
            # Local-style URL but the file is missing
            return None

        # Remote file: read via storage.py's authenticated download rather
        # than a plain HTTP fetch. KYC documents live in a PRIVATE Supabase
        # bucket by design (see storage.py) — a plain urllib fetch of the
        # stored URL always fails silently for a private bucket, regardless
        # of anything else, which is why documents were never actually
        # appearing in this PDF. read_file_bytes() is the same
        # authenticated path already used elsewhere in the app to view KYC
        # documents correctly.
        try:
            import tempfile
            content, _ = read_file_bytes(url)
            if not content:
                return None
            suffix = Path(url.split("?")[0]).suffix or ".bin"
            tmp = Path(tempfile.gettempdir()) / f"kycdl_{uuid4().hex[:10]}{suffix}"
            tmp.write_bytes(content)
            temp_rendered_files.append(tmp)  # cleaned up after the PDF is built
            return tmp
        except Exception as e:
            logger.warning(f"Could not fetch remote document for PDF embed ({url}): {e}")
            return None

    def render_pdf_first_page_to_png(pdf_path: Path):
        """
        Rasterizes a PDF's first page to a temporary PNG, so a
        photo/signature uploaded as a PDF scan can still be embedded
        inline like any other image. Uses PyMuPDF — a self-contained
        library with no external system dependency (unlike poppler),
        so it works the same on any machine without extra installs.
        Returns None (never raises) if rendering fails for any reason.
        """
        try:
            import pymupdf
            pdf_doc = pymupdf.open(str(pdf_path))
            page = pdf_doc[0]
            pix = page.get_pixmap(dpi=150)
            png_path = pdf_path.with_suffix(".rendered.png")
            pix.save(str(png_path))
            pdf_doc.close()
            return png_path
        except Exception as e:
            logger.warning(f"Could not rasterize PDF page for inline embed ({pdf_path}): {e}")
            return None

    def embed_image_inline(elements_list, label: str, url: str):
        """
        Resolves a document URL and appends it to elements_list as an
        embedded image, right where this is called — i.e. inline, next to
        whatever section is currently being built. Handles JPG/PNG
        directly; rasterizes PDFs on the fly since the client's actual
        upload format shouldn't determine the layout. Returns True if
        something was embedded/noted, False if there was no URL at all
        (caller decides how to show "Not uploaded" in its own layout).

        The label and its image are wrapped together in KeepTogether —
        without this, ReportLab is free to insert a page break between the
        label paragraph and the image that follows it (they're otherwise
        two independent flowables with no relationship), which is exactly
        what was producing a label on one page and its actual photo on the
        next. KeepTogether forces them to move to a fresh page as a single
        unit if they don't both fit on the current one.
        """
        if not url:
            return False
        local_path = resolve_local_path(url)
        if not local_path:
            elements_list.append(Paragraph(f"<b>{label}:</b> <a href=\"{url}\" color=\"#A67C1A\">Open document ↗</a>", body_style))
            return True
        ext = local_path.suffix.lower()
        embed_path = None
        if ext in (".jpg", ".jpeg", ".png"):
            embed_path = local_path
        elif ext == ".pdf":
            embed_path = render_pdf_first_page_to_png(local_path)
            if embed_path:
                temp_rendered_files.append(embed_path)
        if embed_path:
            try:
                img = ImageReader(str(embed_path))
                iw, ih = img.getSize()
                display_w = 35 * mm
                display_h = display_w * ih / iw
                elements_list.append(KeepTogether([
                    Paragraph(f"<b>{label}</b>", label_style),
                    Image(str(embed_path), width=display_w, height=display_h),
                    Spacer(1, 6),
                ]))
                return True
            except Exception as e:
                logger.warning(f"Could not embed image {label}: {e}")
                elements_list.append(choice_line(label, "(image could not be loaded)"))
                return True
        return False

    def add_certificate(elements_list, label: str, url: str):
        """
        For genuine multi-page certificates (CAC, SCUML, TIN) — no inline
        box for these on the physical form, so they stay as merged extra
        pages at the end rather than embedded thumbnails.
        """
        if not url:
            elements_list.append(choice_line(label, "Not uploaded"))
            return
        local_path = resolve_local_path(url)
        if not local_path:
            elements_list.append(Paragraph(f"<b>{label}:</b> <a href=\"{url}\" color=\"#A67C1A\">Open document ↗</a>", body_style))
            return
        ext = local_path.suffix.lower()
        if ext in (".jpg", ".jpeg", ".png"):
            embed_image_inline(elements_list, label, url)
        elif ext == ".pdf":
            pdf_attachments.append((label, local_path))
            elements_list.append(choice_line(label, "Attached as additional page(s), see end of this document"))

    # ═══════════════════════════════════════════════════════════════════════
    # INDIVIDUAL / MINOR / JOINT — matches pci_individual_acc_opening_form.pdf
    # ═══════════════════════════════════════════════════════════════════════
    if acct_type in ("Individual", "Minor", "Joint"):

        elements.append(gold_bar("A. CLIENT PERSONAL DATA — INDIVIDUAL OR GUARDIAN OF A MINOR"))
        elements.append(field_grid([
            [("BVN", extra.get("bvn")), ("Title", extra.get("title")), ("Surname", extra.get("surname"))],
            [("First Name", extra.get("first_name")), ("Other Name", extra.get("other_name"))],
            [("DOB", kyc.date_of_birth), ("Gender", extra.get("gender"))],
            [("Marital Status", extra.get("marital_status")), ("Residential Address", kyc.address)],
            [("Mobile Phone", user.phone), ("E-mail", user.email)],
            [("Mother's Maiden Name", extra.get("mother_maiden")), ("Nationality", kyc.nationality)],
            [("State", kyc.state), ("LGA", kyc.lga)],
        ]))
        elements.append(Spacer(1, 4))
        elements.append(choice_line("ID Type", extra.get("id_type")))
        elements.append(choice_line("ID Number", extra.get("id_number")))
        elements.append(choice_line("Politically Exposed Person?", "Yes" if kyc.pep_status else "No"))
        if kyc.pep_status:
            elements.append(choice_line("PEP Details", extra.get("pep_details")))
        elements.append(Spacer(1, 8))

        if not embed_image_inline(elements, "Passport Photograph", kyc.passport_photo_url):
            elements.append(choice_line("Passport Photograph", "Not uploaded"))
        if not embed_image_inline(elements, "ID Document", kyc.id_document_url):
            elements.append(choice_line("ID Document", "Not uploaded"))
        elements.append(Spacer(1, 8))

        if acct_type == "Minor":
            minor = extra.get("minor") or {}
            elements.append(gold_bar("A1. MINOR — GUARDIAN'S DEPENDENT DETAILS"))
            elements.append(field_grid([
                [("DOB", minor.get("dob")), ("Surname", minor.get("surname"))],
                [("First Name", minor.get("first_name")), ("Other Name", minor.get("other_name"))],
                [("Nationality", minor.get("nationality")), ("BVN", minor.get("bvn"))],
            ]))
            elements.append(Spacer(1, 4))
            elements.append(choice_line("Mandate Authorization", minor.get("mandate_auth")))
            if not embed_image_inline(elements, "Minor's Passport Photo", kyc.minor_passport_photo_url):
                elements.append(choice_line("Minor's Passport Photo", "Not uploaded"))
            if not embed_image_inline(elements, "Minor's Birth Certificate", kyc.minor_birth_certificate_url):
                elements.append(choice_line("Minor's Birth Certificate", "Not uploaded"))
            elements.append(Spacer(1, 8))

        if acct_type == "Joint":
            joint = extra.get("joint") or {}
            elements.append(gold_bar("A2. JOINT ACCOUNT — PARTNER DETAILS"))
            elements.append(field_grid([
                [("BVN", joint.get("bvn")), ("Title", joint.get("title")), ("Surname", joint.get("surname"))],
                [("First Name", joint.get("first_name")), ("Other Name", joint.get("other_name"))],
                [("DOB", joint.get("dob")), ("Gender", joint.get("gender"))],
                [("Marital Status", joint.get("marital_status")), ("Residential Address", joint.get("address"))],
                [("Mobile Phone", joint.get("phone")), ("E-mail", joint.get("email"))],
                [("Mother's Maiden Name", joint.get("mother_maiden")), ("Nationality", joint.get("nationality"))],
                [("State", joint.get("state")), ("LGA", joint.get("lga"))],
            ]))
            elements.append(Spacer(1, 4))
            elements.append(choice_line("ID Type", joint.get("id_type")))
            elements.append(choice_line("ID Number", joint.get("id_number")))
            elements.append(choice_line("Politically Exposed Person?", joint.get("pep")))
            if joint.get("pep") == "Yes":
                elements.append(choice_line("PEP Details", joint.get("pep_details")))
            elements.append(choice_line("Mandate Authorization", joint.get("mandate_auth")))
            if not embed_image_inline(elements, "Partner's Passport Photo", kyc.joint_passport_photo_url):
                elements.append(choice_line("Partner's Passport Photo", "Not uploaded"))
            if not embed_image_inline(elements, "Partner's ID Document", kyc.joint_id_document_url):
                elements.append(choice_line("Partner's ID Document", "Not uploaded"))
            elements.append(Spacer(1, 8))

        emp = extra.get("employment") or {}
        elements.append(gold_bar("B. EMPLOYMENT DETAILS"))
        elements.append(choice_line("Employment Status", kyc.occupation))
        elements.append(field_grid([
            [("Employer's Name", kyc.employer), ("Employer's Address", emp.get("employer_address"))],
            [("Nature Of Business", emp.get("nature_of_business")), ("Source Of Funds", kyc.annual_income)],
        ]))
        elements.append(Spacer(1, 8))

        nok = extra.get("next_of_kin") or {}
        elements.append(gold_bar("C. NEXT OF KIN"))
        elements.append(field_grid([
            [("Surname", nok.get("surname")), ("First Name", nok.get("first_name"))],
            [("Other Name", nok.get("other_name")), ("DOB", nok.get("dob"))],
            [("Residential Address", nok.get("address"))],
            [("Relationship", nok.get("relationship")), ("Gender", nok.get("gender"))],
            [("E-mail", nok.get("email")), ("Phone Number", nok.get("phone"))],
        ]))
        elements.append(Spacer(1, 8))

        inv = extra.get("investment") or {}
        elements.append(gold_bar("D. INVESTMENT DETAILS"))
        elements.append(field_grid([
            [("Amount In Figures", format_amount(inv.get("amount_figures")))],
            [("Amount In Words", inv.get("amount_words"))],
            [("Duration", inv.get("duration")), ("Profit/Interest Payments", inv.get("profit_payment"))],
        ]))
        elements.append(Spacer(1, 4))
        elements.append(choice_line("Portfolio Management", kyc.investment_experience))
        elements.append(choice_line("Type of Investment", kyc.risk_profile))
        elements.append(choice_line("Investment Decision", inv.get("investment_decision")))
        elements.append(Spacer(1, 8))

        bank = extra.get("bank") or {}
        elements.append(gold_bar("E. BANK ACCOUNT DETAILS"))
        elements.append(Paragraph(
            "I/We hereby instruct Prime Capital and Investment Limited to transfer all payment due to my/our account:",
            static_style,
        ))
        elements.append(field_grid([
            [("Bank Name", bank.get("bank_name")), ("Account Number", bank.get("account_number"))],
            [("Account Name", bank.get("account_name")), ("BVN", bank.get("bvn"))],
        ]))
        elements.append(Spacer(1, 10))

    # ═══════════════════════════════════════════════════════════════════════
    # CORPORATE — matches pci_corporate_acc_opening_form.pdf
    # ═══════════════════════════════════════════════════════════════════════
    elif acct_type == "Corporate":
        corp = extra.get("corporate") or {}

        elements.append(gold_bar("COMPANY DETAILS"))
        elements.append(field_grid([
            [("Company Name", kyc.company_name)],
            [("Registration Number", kyc.rc_number), ("Date of Incorporation", corp.get("date_of_incorporation"))],
        ]))
        elements.append(Spacer(1, 4))
        elements.append(choice_line("Company Category", corp.get("company_category")))
        elements.append(field_grid([
            [("Nature of Business", corp.get("nature_of_business"))],
            [("Sector/Industry", corp.get("sector")), ("TIN", corp.get("tin"))],
            [("Business Address", kyc.company_address)],
            [("Phone Number 1", corp.get("phone1")), ("Phone Number 2", corp.get("phone2"))],
            [("E-Mail", corp.get("email"))],
            [("SCUML", corp.get("scuml"))],
        ]))
        elements.append(Spacer(1, 4))
        add_certificate(elements, "Certificate of Incorporation (CAC)", kyc.cac_certificate_url)
        add_certificate(elements, "SCUML Certificate", kyc.scuml_certificate_url)
        add_certificate(elements, "TIN Certificate", kyc.tin_certificate_url)
        elements.append(Spacer(1, 8))

        inv = extra.get("investment") or {}
        elements.append(gold_bar("INVESTMENT DETAILS"))
        elements.append(field_grid([
            [("Amount in Figures", format_amount(inv.get("amount_figures")))],
            [("Amount in Words", inv.get("amount_words"))],
            [("Duration", inv.get("duration"))],
            [("Profit/Interest Payment", inv.get("profit_payment"))],
        ]))
        elements.append(Spacer(1, 4))
        elements.append(choice_line("Portfolio Management", kyc.investment_experience))
        elements.append(choice_line("Types of Investments", kyc.risk_profile))
        elements.append(choice_line("Investment Decision", inv.get("investment_decision")))
        elements.append(Spacer(1, 8))

        corp_bank = corp.get("corp_bank") or {}
        elements.append(gold_bar("CORPORATE BANK DETAILS"))
        elements.append(field_grid([
            [("Bank Name", corp_bank.get("bank_name")), ("Account Number", corp_bank.get("account_number"))],
            [("Account Name", corp_bank.get("account_name")), ("BVN", corp_bank.get("bvn"))],
            [("Tax Identification (TIN)", corp_bank.get("tin"))],
        ]))
        elements.append(Paragraph(
            "I certify that all investment principal and profits/interests should be paid into the bank account details provided above.",
            static_style,
        ))
        elements.append(Spacer(1, 8))

        signatories = kyc.signatories or []
        elements.append(gold_bar("DETAILS OF SIGNATORIES / DIRECTORS / EXECUTIVES / TRUSTEES"))
        if not signatories:
            elements.append(Paragraph("No signatories recorded.", body_style))
        for i, sig in enumerate(signatories[:4]):
            letter = ["A", "B", "C", "D"][i]
            elements.append(Paragraph(f"<b>Signatory ({letter})</b>", ParagraphStyle("SigLabel", parent=body_style, textColor=GOLD, spaceBefore=6, spaceAfter=2, fontName="Helvetica-Bold")))
            elements.append(field_grid([
                [("Title", sig.get("title")), ("Surname", sig.get("surname"))],
                [("First Name", sig.get("firstName", sig.get("first_name"))), ("Other Name", sig.get("otherName", sig.get("other_name")))],
                [("DOB", sig.get("dob")), ("Gender", sig.get("gender"))],
                [("Marital Status", sig.get("maritalStatus", sig.get("marital_status"))), ("Residential Address", sig.get("residentialAddress", sig.get("address")))],
                [("Mobile Phone", sig.get("phone")), ("E-mail", sig.get("email"))],
                [("State", sig.get("state")), ("LGA", sig.get("lga"))],
                [("Nationality", sig.get("nationality")), ("BVN", sig.get("bvn"))],
                [("ID Type", sig.get("idType", sig.get("id_type"))), ("ID Number", sig.get("idNumber", sig.get("id_number")))],
            ]))
            if not embed_image_inline(elements, f"Signatory {letter} — Passport Photo", sig.get("passport_photo_url")):
                elements.append(choice_line("Passport Photo", "Not uploaded"))
            if not embed_image_inline(elements, f"Signatory {letter} — Signature", sig.get("signature_url")):
                elements.append(choice_line("Signature", "Not uploaded"))
            elements.append(Spacer(1, 6))
        elements.append(Spacer(1, 4))

        elements.append(gold_bar("ACCOUNT MANDATE"))
        elements.append(choice_line("Mandate Authorization Instruction", corp.get("mandate")))
        elements.append(Spacer(1, 8))

        board = corp.get("board_resolution") or {}
        elements.append(gold_bar("BOARD RESOLUTION"))
        elements.append(field_grid([
            [("Company Name", board.get("company_name"))],
            [("Meeting Date", board.get("meeting_date")), ("Meeting Location", board.get("meeting_location"))],
            [("Director Name", board.get("director_name")), ("Director/Secretary Name", board.get("secretary_name"))],
        ]))
        if not embed_image_inline(elements, "Director Signature", kyc.board_resolution_director_signature_url):
            elements.append(choice_line("Director Signature", "Not uploaded"))
        if not embed_image_inline(elements, "Secretary Signature", kyc.board_resolution_secretary_signature_url):
            elements.append(choice_line("Secretary Signature", "Not uploaded"))
        elements.append(Spacer(1, 10))

    else:
        elements.append(Paragraph(f"Unrecognized account type: {acct_type}", body_style))

    # ── Documents ─────────────────────────────────────────────────────────
    # NEW: previously this just printed the raw URL as plain (non-clickable)
    # text, and even if it had been a real link, viewing meant leaving the
    # PDF entirely. Now: image documents (photos, signatures) are embedded
    # directly in the PDF; PDF documents (CAC, SCUML, TIN, etc.) are merged
    # in as extra pages at the end — so downloading ONE file gives staff the
    # completed form AND every attachment together, matching how the
    # physical form works with documents stapled to it.
    # (Documents are now embedded inline within their own sections above —
    # passport photo/ID under Personal Data, each signatory's photo/
    # signature within their own block, director/secretary signatures
    # within Board Resolution, and CAC/SCUML/TIN certificates within
    # Company Details. Nothing left to do here except finalize the PDF
    # and merge in any certificate attachments queued along the way.)

    # ── Terms & Conditions acknowledgment (NEW — all account types) ────────
    # The client's acceptance was tracked on the form (a required checkbox
    # before Submit was even clickable) but never actually recorded or
    # shown anywhere — no audit trail of it at all. Now shown here for
    # every account type, sourced from extra_data.terms_accepted(_at).
    elements.append(Spacer(1, 6))
    elements.append(gold_bar("CLIENT DECLARATION"))
    terms_accepted = extra.get("terms_accepted")
    terms_accepted_at = extra.get("terms_accepted_at")
    if terms_accepted:
        accepted_display = terms_accepted_at
        if terms_accepted_at:
            try:
                accepted_display = datetime.fromisoformat(terms_accepted_at.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")
            except (ValueError, AttributeError):
                pass
        elements.append(Paragraph(
            f"✓ The client confirmed they have read and agree to Prime Capital & Investment Ltd's "
            f"Terms and Conditions{f' (accepted {accepted_display})' if accepted_display else ''}.",
            body_style,
        ))
    else:
        elements.append(Paragraph(
            "⚠ No record of Terms and Conditions acceptance found for this submission.",
            ParagraphStyle("Warn", parent=body_style, textColor=colors.HexColor("#c0392b")),
        ))

    elements.append(Spacer(1, 10 * mm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    elements.append(Paragraph(
        "This document is generated from the Prime Capital Investor Portal for internal D365 onboarding use only. "
        "Do not distribute externally.",
        footer_style,
    ))

    doc.build(elements)
    buffer.seek(0)

    # Clean up temporary rendered PNGs now that the build has read them —
    # they're scratch files, not meant to accumulate in kyc-documents/.
    for temp_path in temp_rendered_files:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ── Merge in any PDF attachments as extra pages ─────────────────────────
    if pdf_attachments:
        try:
            writer = PdfWriter()
            form_reader = PdfReader(buffer)
            for page in form_reader.pages:
                writer.add_page(page)
            for label, path in pdf_attachments:
                try:
                    attachment_reader = PdfReader(str(path))
                    for page in attachment_reader.pages:
                        writer.add_page(page)
                except Exception as e:
                    logger.warning(f"Could not merge PDF attachment '{label}' for KYC {kyc_id}: {e}")
            merged_buffer = io.BytesIO()
            writer.write(merged_buffer)
            merged_buffer.seek(0)
            buffer = merged_buffer
        except Exception as e:
            # If merging fails for any reason, fall back to the form-only
            # PDF rather than losing the whole export.
            logger.warning(f"PDF attachment merge failed for KYC {kyc_id}, falling back to form-only PDF: {e}")
            buffer.seek(0)

    safe_name = user.full_name.replace(" ", "_")
    filename = f"KYC_{safe_name}_{kyc_id}.pdf"

    logger.info(f"KYC PDF exported ({acct_type}, form-styled, {len(pdf_attachments)} attachment(s) merged) for {user.email} by {admin.email}")

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
