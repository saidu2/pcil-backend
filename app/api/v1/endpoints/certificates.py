# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/certificates.py  (NEW)
#
# Real, backend-persisted investment certificates. Previously "issuing" a
# certificate only wrote to that admin's own browser localStorage — nothing
# was saved to the database, there was no way to edit a mistake after
# issuing, and different admins/devices never saw each other's certificates.
# This gives certificates the same real CRUD every other admin-managed
# record in this app already has.
#
# The actual certificate image (PNG) is still generated entirely client-side
# (see src/utils/certificateGenerator.js) from whatever data this endpoint
# returns — this file is only responsible for storing and editing that data
# correctly, not rendering the certificate itself.
# ─────────────────────────────────────────────────────────────────────────────

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.models import Certificate, AuditLog
from app.schemas.schemas import CertificateCreate, CertificateUpdate, CertificateResponse
from app.api.v1.deps import get_current_admin, require_staff_permission

logger = logging.getLogger(__name__)
admin_router = APIRouter()


async def _next_certificate_reference(db: AsyncSession) -> str:
    """
    PCIL/CERT/<year>/<sequential>. Sequential number is a simple total
    count + 1 — good enough for how rarely certificates are issued (nothing
    like the volume subscriptions/redemptions see), and the retry-on-
    IntegrityError in the create endpoint below covers the rare case of
    two admins issuing at the exact same moment.
    """
    year = datetime.now(timezone.utc).year
    count_result = await db.execute(select(func.count()).select_from(Certificate))
    n = (count_result.scalar() or 0) + 1
    return f"PCIL/CERT/{year}/{n:03d}"


@admin_router.get(
    "",
    summary="Admin: List all certificates",
)
async def admin_list_certificates(
    status_filter: str = None,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),  # any logged-in staff can VIEW — actions stay permission-gated
):
    """List all issued certificates, enriched with client name/email."""
    query = select(Certificate).options(selectinload(Certificate.user))
    if status_filter:
        query = query.where(Certificate.status == status_filter)
    query = query.order_by(Certificate.issued_at.desc())
    result = await db.execute(query)
    certs = result.scalars().all()

    return [
        {
            "id":               str(c.id),
            "user_id":          str(c.user_id),
            "subscription_id":  str(c.subscription_id) if c.subscription_id else None,
            "reference":        c.reference,
            "client_name":      c.user.full_name if c.user else "—",
            "client_email":     c.user.email if c.user else "—",
            "product_name":     c.product_name,
            "amount":           c.amount,
            "roi":              c.roi,
            "issue_date":       c.issue_date,
            "maturity_date":    c.maturity_date,
            "account_type":     c.account_type,
            "status":           c.status,
            "issued_at":        c.issued_at.isoformat() if c.issued_at else None,
            "emailed_at":       c.emailed_at.isoformat() if c.emailed_at else None,
        }
        for c in certs
    ]


@admin_router.post(
    "",
    response_model=CertificateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Admin: Issue a certificate",
)
async def admin_create_certificate(
    body: CertificateCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_staff_permission("can_manage_certificates")),
):
    """Issues a certificate — persisted for real, not just written to a browser."""
    cert = None
    for _attempt in range(3):
        try:
            reference = await _next_certificate_reference(db)
            cert = Certificate(
                user_id=body.user_id,
                subscription_id=body.subscription_id,
                reference=reference,
                product_name=body.product_name,
                amount=body.amount,
                roi=body.roi,
                issue_date=body.issue_date,
                maturity_date=body.maturity_date,
                account_type=body.account_type,
                status="issued",
            )
            db.add(cert)
            await db.flush()
            break
        except IntegrityError:
            await db.rollback()
            cert = None
            continue

    if cert is None:
        raise HTTPException(
            status_code=500,
            detail="Could not generate a unique certificate reference. Please try again.",
        )

    db.add(AuditLog(
        action="Certificate Issued", target=cert.reference, target_id=str(cert.id),
        action_type="certificate", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"product_name": cert.product_name, "account_type": cert.account_type},
    ))
    await db.commit()
    await db.refresh(cert)

    logger.info(f"Certificate issued: {cert.reference} by {admin.email}")
    return cert


@admin_router.patch(
    "/{certificate_id}",
    response_model=CertificateResponse,
    summary="Admin: Edit a certificate",
)
async def admin_update_certificate(
    certificate_id: UUID,
    body: CertificateUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_staff_permission("can_manage_certificates")),
):
    """
    Edits a previously issued certificate — the capability that didn't
    exist before, since certificates were never really saved anywhere.
    Every field is editable regardless of status; also used to revoke a
    certificate by setting status="revoked".
    """
    cert = await db.get(Certificate, certificate_id)
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found.")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes provided.")

    for field, value in changes.items():
        setattr(cert, field, value)

    db.add(AuditLog(
        action="Certificate Edited", target=cert.reference, target_id=str(cert.id),
        action_type="certificate", performed_by=admin.id, performed_by_name=admin.full_name,
        details=changes,
    ))
    await db.commit()
    await db.refresh(cert)

    logger.info(f"Certificate edited: {cert.reference} by {admin.email} — {list(changes.keys())}")
    return cert
