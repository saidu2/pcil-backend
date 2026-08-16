# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/nav.py
#
# Daily NAV calculator for Prime Capital's unitised fund model.
#
# HOW THE UNITISED MODEL WORKS:
#   1. Each product has an initial_nav set at launch (e.g. ₦100.00 per unit)
#   2. When admin activates a client subscription, they enter today's NAV
#      → units_allocated = amount ÷ nav_at_entry  (stored on subscription)
#   3. Finance team enters a new NAV every day per product
#      → Every client's current value = their units × today's NAV  (auto)
#   4. Client dashboard reads /nav/my-portfolio → sees real gain/loss
#
# ADMIN endpoints  (registered at /api/v1/admin/nav in main.py):
#   POST   /api/v1/admin/nav/daily              — submit today's NAV (main workflow)
#   GET    /api/v1/admin/nav/today              — today's status for all products
#   GET    /api/v1/admin/nav/portfolio-summary  — every client's current value
#   GET    /api/v1/admin/nav                    — full history (paginated)
#   PATCH  /api/v1/admin/nav/{record_id}        — correct a submitted NAV
#   DELETE /api/v1/admin/nav/{record_id}        — delete (super_admin only)
#
# PUBLIC endpoints  (registered at /api/v1/nav in main.py):
#   GET    /api/v1/nav/my-portfolio             — client's current NAV-based values
#   GET    /api/v1/nav/{product_id}/latest      — latest NAV for one product
#   GET    /api/v1/nav/{product_id}/history     — history for charting
#
# D365 NOTE:
#   When D365 is configured, webhooks.py calls upsert_nav_from_d365()
#   which writes to the same nav_records table with source="d365".
#   No changes needed here at that point.
# ─────────────────────────────────────────────────────────────────────────────

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import NavRecord, Product, Subscription, AdminUser, User
from app.api.v1.deps import get_current_admin, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()           # admin routes  — mounted at /api/v1/admin/nav
public_router = APIRouter()    # public routes — mounted at /api/v1/nav


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class DailyNavEntry(BaseModel):
    """One product's NAV for today."""
    product_id:   UUID
    nav_per_unit: float = Field(..., gt=0, description="Today's NAV per unit e.g. 102.45")
    notes:        Optional[str] = None


class DailyNavSubmit(BaseModel):
    """
    Finance team submits today's NAV for one or more products in one call.
    Example:
    {
      "entries": [
        {"product_id": "...", "nav_per_unit": 102.45},
        {"product_id": "...", "nav_per_unit": 1.023}
      ]
    }
    """
    entries: list[DailyNavEntry] = Field(..., min_length=1)


class NavCorrection(BaseModel):
    """Correct a previously submitted NAV record."""
    nav_per_unit: Optional[float] = Field(None, gt=0)
    notes:        Optional[str]   = None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _to_dict(r: NavRecord) -> dict:
    return {
        "id":             str(r.id),
        "product_id":     str(r.product_id),
        "period_label":   r.period_label,
        "period_start":   r.period_start.isoformat() if r.period_start else None,
        "period_end":     r.period_end.isoformat()   if r.period_end   else None,
        "nav_per_unit":   r.nav_per_unit,
        "return_pct":     r.return_pct,
        "cumulative_pct": r.cumulative_pct,
        "total_aum":      r.total_aum,
        "notes":          r.notes,
        "source":         r.source,
        "entered_by":     str(r.entered_by) if r.entered_by else None,
        "created_at":     r.created_at.isoformat() if r.created_at else None,
        "updated_at":     r.updated_at.isoformat() if r.updated_at else None,
    }


async def _compute_metrics(
    product_id: UUID,
    new_nav: float,
    db: AsyncSession,
    exclude_today: bool = True,
) -> dict:
    """
    Auto-compute return_pct, cumulative_pct, and total_aum for a new NAV value.

    return_pct     = daily change vs most recent previous NAV
    cumulative_pct = total change vs product's initial_nav
    total_aum      = sum of (units_allocated x new_nav) for all active clients
    """
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y")

    # Most recent previous NAV (exclude today so corrections work correctly)
    q = (
        select(NavRecord)
        .where(NavRecord.product_id == product_id)
        .order_by(NavRecord.period_end.desc())
        .limit(1)
    )
    if exclude_today:
        q = q.where(NavRecord.period_label != today_str)

    prev_result = await db.execute(q)
    prev = prev_result.scalar_one_or_none()
    prev_nav = prev.nav_per_unit if prev and prev.nav_per_unit else None

    # Product initial_nav for cumulative %
    prod = await db.get(Product, product_id)
    initial_nav = prod.initial_nav if prod and prod.initial_nav else new_nav

    # Daily return %
    return_pct = None
    if prev_nav and prev_nav > 0:
        return_pct = round(((new_nav - prev_nav) / prev_nav) * 100, 4)

    # Cumulative return % since inception
    cumulative_pct = None
    if initial_nav and initial_nav > 0:
        cumulative_pct = round(((new_nav - initial_nav) / initial_nav) * 100, 4)

    # Total AUM across all active clients
    subs_res = await db.execute(
        select(Subscription).where(
            Subscription.product_id == product_id,
            Subscription.status == "active",
            Subscription.units_allocated.isnot(None),
        )
    )
    active_subs = subs_res.scalars().all()
    total_aum = None
    if active_subs:
        total_aum = round(sum((s.units_allocated or 0) * new_nav for s in active_subs), 2)

    return {
        "return_pct":     return_pct,
        "cumulative_pct": cumulative_pct,
        "total_aum":      total_aum,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — DAILY NAV SUBMISSION (main workflow)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/daily",
    status_code=status.HTTP_201_CREATED,
    summary="Submit today's NAV for one or more products",
)
async def submit_daily_nav(
    body: DailyNavSubmit,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Main daily workflow for the finance team.
    Submit today's NAV per unit for one or more products in a single call.
    The system automatically computes daily return %, cumulative %, and total AUM.
    If today's record already exists for a product, it is corrected (upsert).
    """
    today_str   = datetime.now(timezone.utc).strftime("%d %b %Y")
    today_start = datetime.now(timezone.utc).replace(hour=0,  minute=0,  second=0,  microsecond=0)
    today_end   = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)

    results = []

    for entry in body.entries:
        prod = await db.get(Product, entry.product_id)
        if not prod:
            results.append({"product_id": str(entry.product_id), "error": "Product not found"})
            continue

        metrics = await _compute_metrics(entry.product_id, entry.nav_per_unit, db)

        # Upsert: update today's record if it already exists
        existing_res = await db.execute(
            select(NavRecord)
            .where(NavRecord.product_id == entry.product_id)
            .where(NavRecord.period_label == today_str)
        )
        existing = existing_res.scalar_one_or_none()

        if existing:
            existing.nav_per_unit   = entry.nav_per_unit
            existing.return_pct     = metrics["return_pct"]
            existing.cumulative_pct = metrics["cumulative_pct"]
            existing.total_aum      = metrics["total_aum"]
            existing.notes          = entry.notes or existing.notes
            existing.updated_at     = datetime.now(timezone.utc)
            action = "updated"
        else:
            db.add(NavRecord(
                product_id=     entry.product_id,
                period_label=   today_str,
                period_start=   today_start,
                period_end=     today_end,
                nav_per_unit=   entry.nav_per_unit,
                return_pct=     metrics["return_pct"],
                cumulative_pct= metrics["cumulative_pct"],
                total_aum=      metrics["total_aum"],
                notes=          entry.notes,
                source=         "manual",
                entered_by=     admin.id,
            ))
            action = "created"

        await db.flush()
        results.append({
            "product_name":   prod.name,
            "nav_per_unit":   entry.nav_per_unit,
            "return_pct":     metrics["return_pct"],
            "cumulative_pct": metrics["cumulative_pct"],
            "total_aum":      metrics["total_aum"],
            "action":         action,
        })
        logger.info(
            f"Daily NAV {action}: product={prod.name} "
            f"nav={entry.nav_per_unit} by admin={admin.email}"
        )

    await db.commit()
    return {"date": today_str, "entered_by": admin.full_name, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — TODAY'S STATUS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/today", summary="Today's NAV status for all active products")
async def get_today_nav(
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Shows today's submitted NAV for each active product alongside the
    previous NAV and client summary. Used by the admin NAV panel to show
    what has been entered today and what still needs to be submitted.
    """
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y")

    prods_res = await db.execute(
        select(Product).where(Product.is_active == True).order_by(Product.name)
    )
    products = prods_res.scalars().all()

    out = []
    for prod in products:
        today_res = await db.execute(
            select(NavRecord)
            .where(NavRecord.product_id == prod.id)
            .where(NavRecord.period_label == today_str)
        )
        today_rec = today_res.scalar_one_or_none()

        prev_res = await db.execute(
            select(NavRecord)
            .where(NavRecord.product_id == prod.id)
            .where(NavRecord.period_label != today_str)
            .order_by(NavRecord.period_end.desc())
            .limit(1)
        )
        prev_rec = prev_res.scalar_one_or_none()

        subs_res = await db.execute(
            select(Subscription).where(
                Subscription.product_id == prod.id,
                Subscription.status == "active",
            )
        )
        active_subs = subs_res.scalars().all()
        total_units = sum(s.units_allocated or 0 for s in active_subs)

        out.append({
            "product_id":       str(prod.id),
            "product_name":     prod.name,
            "currency":         prod.currency,
            "initial_nav":      prod.initial_nav,
            "active_clients":   len(active_subs),
            "total_units":      round(total_units, 4),
            "prev_nav":         prev_rec.nav_per_unit if prev_rec else None,
            "prev_date":        prev_rec.period_label if prev_rec else None,
            "today_submitted":  today_rec is not None,
            "today_nav":        today_rec.nav_per_unit   if today_rec else None,
            "today_return_pct": today_rec.return_pct     if today_rec else None,
            "today_total_aum":  today_rec.total_aum      if today_rec else None,
        })

    return {"date": today_str, "products": out}


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — PORTFOLIO SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/portfolio-summary", summary="All clients' current portfolio values")
async def portfolio_summary(
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Every active subscription with its current NAV-based value, gain in
    currency, and gain %. Only includes subscriptions where units have
    been allocated (i.e. activated with a NAV).
    """
    subs_res = await db.execute(
        select(Subscription, Product, User)
        .outerjoin(Product, Subscription.product_id == Product.id)
        .outerjoin(User, Subscription.user_id == User.id)
        .where(Subscription.status == "active")
        .where(Subscription.units_allocated.isnot(None))
    )
    rows = subs_res.all()

    nav_cache: dict[str, float] = {}
    out = []

    for sub, prod, user in rows:
        pid = str(sub.product_id)
        if pid not in nav_cache:
            nav_res = await db.execute(
                select(NavRecord)
                .where(NavRecord.product_id == sub.product_id)
                .order_by(NavRecord.period_end.desc())
                .limit(1)
            )
            nav_rec = nav_res.scalar_one_or_none()
            nav_cache[pid] = nav_rec.nav_per_unit if nav_rec else (sub.nav_at_entry or 1.0)

        current_nav   = nav_cache[pid]
        units         = sub.units_allocated or 0
        current_value = round(units * current_nav, 2)
        gain          = round(current_value - sub.amount, 2)
        gain_pct      = round((gain / sub.amount * 100) if sub.amount else 0, 4)

        out.append({
            "subscription_id": str(sub.id),
            "reference":       sub.reference,
            "client_name":     user.full_name if user else "Unknown",
            "client_email":    user.email     if user else "Unknown",
            "product_name":    prod.name      if prod  else "Unknown",
            "currency":        prod.currency  if prod  else "NGN",
            "amount_invested": sub.amount,
            "nav_at_entry":    sub.nav_at_entry,
            "units_allocated": round(units, 4),
            "current_nav":     current_nav,
            "current_value":   current_value,
            "gain":            gain,
            "gain_pct":        gain_pct,
        })

    return out


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — HISTORY + CORRECTIONS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", summary="List all NAV records (paginated)")
async def list_nav(
    product_id: Optional[UUID] = Query(default=None),
    page:       int            = Query(default=1,  ge=1),
    limit:      int            = Query(default=60, le=365),
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """List all NAV records newest first. Filter by product_id if needed."""
    q = select(NavRecord).order_by(NavRecord.period_end.desc())
    if product_id:
        q = q.where(NavRecord.product_id == product_id)
    q = q.offset((page - 1) * limit).limit(limit)
    result = await db.execute(q)
    return [_to_dict(r) for r in result.scalars().all()]


@router.patch("/{record_id}", summary="Correct a NAV record")
async def correct_nav(
    record_id: UUID,
    body: NavCorrection,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Correct a previously submitted NAV entry.
    Automatically recomputes return_pct, cumulative_pct, and total_aum.
    """
    record = await db.get(NavRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="NAV record not found.")

    if body.nav_per_unit is not None:
        metrics = await _compute_metrics(
            record.product_id, body.nav_per_unit, db, exclude_today=False
        )
        record.nav_per_unit   = body.nav_per_unit
        record.return_pct     = metrics["return_pct"]
        record.cumulative_pct = metrics["cumulative_pct"]
        record.total_aum      = metrics["total_aum"]

    if body.notes is not None:
        record.notes = body.notes

    record.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(record)
    logger.info(f"NAV corrected: id={record_id} by admin={admin.email}")
    return _to_dict(record)


@router.delete(
    "/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a NAV record (super_admin only)",
)
async def delete_nav(
    record_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Delete a NAV record. Restricted to super_admin."""
    if admin.role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required.")
    record = await db.get(NavRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="NAV record not found.")
    await db.delete(record)
    await db.commit()
    logger.info(f"NAV record deleted: id={record_id} by admin={admin.email}")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC — CLIENT DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@public_router.get("/my-portfolio", summary="My current NAV-based portfolio values")
async def my_portfolio(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns current NAV-based value for all active subscriptions of the
    logged-in client. Falls back gracefully if no NAV entered yet.
    Includes nav_history array for the performance chart (last 90 days).
    """
    subs_res = await db.execute(
        select(Subscription, Product)
        .outerjoin(Product, Subscription.product_id == Product.id)
        .where(Subscription.user_id == current_user.id)
        .where(Subscription.status == "active")
    )
    rows = subs_res.all()
    out = []

    for sub, prod in rows:
        nav_res = await db.execute(
            select(NavRecord)
            .where(NavRecord.product_id == sub.product_id)
            .order_by(NavRecord.period_end.desc())
            .limit(1)
        )
        latest = nav_res.scalar_one_or_none()
        current_nav = latest.nav_per_unit if latest else sub.nav_at_entry

        if sub.units_allocated and current_nav:
            current_value = sub.units_allocated * current_nav
            gain          = current_value - sub.amount
            gain_pct      = (gain / sub.amount * 100) if sub.amount else 0
            has_nav_data  = True
        else:
            current_value = sub.amount
            gain = gain_pct = 0.0
            has_nav_data  = False

        hist_res = await db.execute(
            select(NavRecord)
            .where(NavRecord.product_id == sub.product_id)
            .order_by(NavRecord.period_end.asc())
            .limit(90)
        )
        history = hist_res.scalars().all()

        out.append({
            "subscription_id":  str(sub.id),
            "reference":        sub.reference,
            "product_id":       str(sub.product_id),
            "product_name":     prod.name     if prod else "Investment",
            "product_category": prod.category if prod else None,
            "product_currency": prod.currency if prod else "NGN",
            "amount_invested":  sub.amount,
            "nav_at_entry":     sub.nav_at_entry,
            "units_allocated":  round(sub.units_allocated, 4) if sub.units_allocated else None,
            "current_nav":      current_nav,
            "current_value":    round(current_value, 2),
            "gain":             round(gain, 2),
            "gain_pct":         round(gain_pct, 4),
            "has_nav_data":     has_nav_data,
            "activated_at":     sub.activated_at.isoformat()  if sub.activated_at  else None,
            "maturity_date":    sub.maturity_date.isoformat()  if sub.maturity_date else None,
            "nav_history": [
                {
                    "date":           r.period_label,
                    "nav":            r.nav_per_unit,
                    "return_pct":     r.return_pct,
                    "cumulative_pct": r.cumulative_pct,
                }
                for r in history
            ],
        })

    return out


@public_router.get("/{product_id}/latest", summary="Latest NAV for a product")
async def get_latest_nav(
    product_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Latest NAV record for a product. Used by subscription activation flow."""
    result = await db.execute(
        select(NavRecord)
        .where(NavRecord.product_id == product_id)
        .order_by(NavRecord.period_end.desc())
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if not record:
        return {"product_id": str(product_id), "nav": None}
    return {"product_id": str(product_id), "nav": _to_dict(record)}


@public_router.get("/{product_id}/history", summary="NAV history for charting")
async def get_nav_history(
    product_id: UUID,
    limit: int = Query(default=90, le=365),
    db: AsyncSession = Depends(get_db),
):
    """NAV history oldest-first. Default 90 entries (3 months of daily data)."""
    result = await db.execute(
        select(NavRecord)
        .where(NavRecord.product_id == product_id)
        .order_by(NavRecord.period_end.asc())
        .limit(limit)
    )
    return [_to_dict(r) for r in result.scalars().all()]


# ─────────────────────────────────────────────────────────────────────────────
# D365 INTEGRATION HELPER  (called from webhooks.py)
# ─────────────────────────────────────────────────────────────────────────────

async def upsert_nav_from_d365(payload: dict, db: AsyncSession):
    """
    Called by webhooks.py when D365 fires a NAV update webhook.
    Writes to nav_records with source="d365". Metrics are auto-computed.

    D365 field mapping (confirm when D365 entity is set up):
        pcil_productid     -> product_id
        pcil_periodlabel   -> period_label  (e.g. "09 Mar 2026")
        pcil_periodstart   -> period_start  (ISO datetime)
        pcil_periodend     -> period_end    (ISO datetime)
        pcil_navperunit    -> nav_per_unit
    """
    try:
        product_id   = UUID(payload["pcil_productid"])
        period_label = payload["pcil_periodlabel"]
        new_nav      = float(payload["pcil_navperunit"])
        metrics      = await _compute_metrics(product_id, new_nav, db)
        now          = datetime.now(timezone.utc)

        existing_res = await db.execute(
            select(NavRecord)
            .where(NavRecord.product_id == product_id)
            .where(NavRecord.period_label == period_label)
        )
        existing = existing_res.scalar_one_or_none()

        if existing:
            existing.nav_per_unit   = new_nav
            existing.return_pct     = metrics["return_pct"]
            existing.cumulative_pct = metrics["cumulative_pct"]
            existing.total_aum      = metrics["total_aum"]
            existing.source         = "d365"
            existing.updated_at     = now
        else:
            db.add(NavRecord(
                product_id=     product_id,
                period_label=   period_label,
                period_start=   datetime.fromisoformat(
                    payload.get("pcil_periodstart", now.isoformat())
                ),
                period_end=     datetime.fromisoformat(
                    payload.get("pcil_periodend", now.isoformat())
                ),
                nav_per_unit=   new_nav,
                return_pct=     metrics["return_pct"],
                cumulative_pct= metrics["cumulative_pct"],
                total_aum=      metrics["total_aum"],
                source=         "d365",
            ))

        await db.commit()
        logger.info(
            f"D365 NAV upsert: product={product_id} "
            f"period={period_label} nav={new_nav}"
        )

    except Exception as e:
        logger.error(f"D365 NAV upsert failed: {e}")
        raise
