# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/portfolio.py  (NEW)
#
# Private Portfolio valuation — holdings, instrument prices, and the
# "Run Valuation" engine. See models.py's PortfolioHolding / InstrumentPrice
# / PortfolioValuation for the full design rationale.
#
# ADMIN endpoints (Investment Manager / IT Admin — can_manage_nav):
#   POST   /api/v1/admin/portfolio/subscriptions/{id}/holdings   — add a holding
#   GET    /api/v1/admin/portfolio/subscriptions/{id}/holdings   — list holdings
#   PATCH  /api/v1/admin/portfolio/holdings/{id}/redeem          — partial/full sell
#   POST   /api/v1/admin/portfolio/prices/batch                  — submit closing prices
#   POST   /api/v1/admin/portfolio/run-valuation                 — revalue portfolios
#   GET    /api/v1/admin/portfolio/subscriptions/{id}/valuations — history (admin view)
#
# PUBLIC endpoint (client dashboard):
#   GET    /api/v1/portfolio/my-holdings                         — holdings + value + chart data
# ─────────────────────────────────────────────────────────────────────────────

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import (
    PortfolioHolding, InstrumentPrice, PortfolioValuation,
    Subscription, AdminUser, AuditLog, User, SystemSettings, Redemption,
)
from app.schemas.schemas import (
    PortfolioHoldingCreate, PortfolioHoldingResponse, PortfolioHoldingRedeem,
    PortfolioHoldingUpdate,
    InstrumentPriceBatchSubmit, RunValuationRequest, PortfolioValuationResponse,
    InstrumentRename,
    MessageResponse,
)
from app.api.v1.deps import get_current_admin, get_current_user, require_staff_permission

logger = logging.getLogger(__name__)
router = APIRouter()          # admin routes — mounted at /api/v1/admin/portfolio
public_router = APIRouter()   # client routes — mounted at /api/v1/portfolio


# ─────────────────────────────────────────────────────────────────────────────
# VALUATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _latest_price(instrument_name: str, db: AsyncSession):
    result = await db.execute(
        select(InstrumentPrice)
        .where(InstrumentPrice.instrument_name == instrument_name)
        .order_by(InstrumentPrice.price_date.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row.price if row else None


def _fixed_income_value(holding: PortfolioHolding, as_of: datetime) -> float:
    """
    Simple accrual to maturity: principal + (principal * roi% * elapsed_fraction).
    Elapsed fraction is capped at 1.0 (fully accrued once past maturity) — the
    Investment Manager is expected to redeem/roll over matured placements,
    this is just a safety cap so valuations don't overstate past maturity.
    """
    if not holding.principal or not holding.roi_pct or not holding.start_date or not holding.maturity_date:
        return holding.principal or 0.0
    total_days = (holding.maturity_date - holding.start_date).total_seconds() / 86400
    if total_days <= 0:
        return holding.principal
    elapsed_days = (as_of - holding.start_date).total_seconds() / 86400
    fraction = max(0.0, min(1.0, elapsed_days / total_days))
    return round(holding.principal * (1 + (holding.roi_pct / 100) * fraction), 2)


async def _value_holding(holding: PortfolioHolding, as_of: datetime, db: AsyncSession) -> dict:
    """Returns a value + detail dict for one holding — used by run-valuation."""
    if holding.holding_type == "equity":
        price = await _latest_price(holding.instrument_name, db)
        value = round((holding.units or 0) * price, 2) if price else 0.0
        return {
            "holding_id": str(holding.id), "name": holding.instrument_name, "type": "equity",
            "units": holding.units, "price": price, "value": value,
        }
    else:  # fixed_income
        value = _fixed_income_value(holding, as_of)
        return {
            "holding_id": str(holding.id), "name": holding.instrument_name, "type": "fixed_income",
            "principal": holding.principal, "roi_pct": holding.roi_pct, "value": value,
        }


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — HOLDINGS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/subscriptions/{subscription_id}/holdings",
    response_model=PortfolioHoldingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a holding to a client's portfolio — can_manage_nav",
)
async def add_holding(
    subscription_id: UUID,
    body: PortfolioHoldingCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    sub = await db.get(Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription (portfolio) not found.")

    if body.holding_type not in ("equity", "fixed_income"):
        raise HTTPException(status_code=400, detail="holding_type must be 'equity' or 'fixed_income'.")
    if body.holding_type == "equity" and not (body.units and body.cost_price):
        raise HTTPException(status_code=400, detail="Equity holdings require units and cost_price.")
    if body.holding_type == "fixed_income" and not (body.principal and body.roi_pct and body.maturity_date):
        raise HTTPException(status_code=400, detail="Fixed income holdings require principal, roi_pct, and maturity_date.")

    holding = PortfolioHolding(
        subscription_id=subscription_id,
        holding_type=body.holding_type,
        instrument_name=body.instrument_name,
        units=body.units, cost_price=body.cost_price,
        principal=body.principal, roi_pct=body.roi_pct,
        start_date=body.start_date or datetime.now(timezone.utc),
        maturity_date=body.maturity_date,
        status="active",
        created_by=admin.id,
    )
    db.add(holding)

    db.add(AuditLog(
        action="Portfolio Holding Added", target=body.instrument_name, target_id=str(subscription_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details=body.model_dump(mode="json"),
    ))
    await db.commit()
    await db.refresh(holding)
    return holding


@router.get(
    "/subscriptions/{subscription_id}/holdings",
    response_model=list[PortfolioHoldingResponse],
    summary="List a portfolio's holdings — can_manage_nav",
)
async def list_holdings(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    result = await db.execute(
        select(PortfolioHolding)
        .where(PortfolioHolding.subscription_id == subscription_id)
        .order_by(PortfolioHolding.created_at)
    )
    return result.scalars().all()


@router.patch(
    "/holdings/{holding_id}",
    response_model=PortfolioHoldingResponse,
    summary="Edit a holding — can_manage_nav",
)
async def update_holding(
    holding_id: UUID,
    body: PortfolioHoldingUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Corrects any field on an existing holding (wrong units, wrong cost
    price, wrong purchase date, misspelled instrument name, etc).

    Note: editing does NOT retroactively change past valuation snapshots —
    those are a historical record of what was calculated at the time. Run
    a new valuation afterward to reflect the corrected figures going
    forward.
    """
    holding = await db.get(PortfolioHolding, holding_id)
    if not holding:
        raise HTTPException(status_code=404, detail="Holding not found.")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")

    before = {k: getattr(holding, k) for k in update_data}
    for field, value in update_data.items():
        setattr(holding, field, value)

    db.add(AuditLog(
        action="Portfolio Holding Edited", target=holding.instrument_name, target_id=str(holding_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"before": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in before.items()},
                 "after": body.model_dump(mode="json", exclude_unset=True)},
    ))
    await db.commit()
    await db.refresh(holding)
    return holding


@router.delete(
    "/holdings/{holding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Permanently delete a holding — can_manage_nav",
)
async def delete_holding(
    holding_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Permanently removes a holding. This exists ONLY to correct a genuine
    data-entry mistake — wrong instrument, wrong client, duplicate entry —
    made before the holding was ever real to the client. It is NOT a way
    to close out a real position; that is what redeem_holding is for,
    since redemption leaves a proper record (sale price, realized gain,
    a Redemption row) and this does not.

    To guarantee that, deletion is blocked once the underlying
    subscription's portfolio has ANY valuation history at all — not
    per-holding, since valuations only store portfolio-level totals, not
    which specific holdings they included. A holding added after the
    portfolio's last valuation run could theoretically still be safe to
    delete, but there is currently no per-holding record precise enough
    to prove that, so this errs toward blocking rather than risking a
    deletion that silently invalidates a number the client already saw.

    This check is intentionally enforced here, not just in the frontend —
    the Delete button there is hidden under the same condition, but that
    is a UX convenience, not a guarantee. A direct API call must be
    stopped independently of whatever the UI happens to show.
    """
    holding = await db.get(PortfolioHolding, holding_id)
    if not holding:
        raise HTTPException(status_code=404, detail="Holding not found.")

    has_valuation_history = (await db.execute(
        select(PortfolioValuation.id)
        .where(PortfolioValuation.subscription_id == holding.subscription_id)
        .limit(1)
    )).scalar_one_or_none()

    if has_valuation_history:
        raise HTTPException(
            status_code=400,
            detail=(
                "This portfolio already has valuation history, so this holding "
                "may have contributed to a value the client has seen. It can no "
                "longer be deleted — use Redeem instead to close it out with a "
                "proper record."
            ),
        )

    db.add(AuditLog(
        action="Portfolio Holding Deleted", target=holding.instrument_name, target_id=str(holding_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={
            "holding_type": holding.holding_type,
            "instrument_name": holding.instrument_name,
            "subscription_id": str(holding.subscription_id),
            "reason": "data_entry_correction",
        },
    ))
    await db.delete(holding)
    await db.commit()
    return None


def _generate_redemption_reference() -> str:
    """
    Unique, human-scannable reference for a redemption record, e.g.
    RDM-8F2C9A1B. Kept local rather than importing the subscription
    reference generator, since redemptions live in a different module
    and don't need to share its exact format.
    """
    return f"RDM-{secrets.token_hex(4).upper()}"


@router.patch(
    "/holdings/{holding_id}/redeem",
    response_model=PortfolioHoldingResponse,
    summary="Partially or fully redeem a holding — can_manage_nav",
)
async def redeem_holding(
    holding_id: UUID,
    body: PortfolioHoldingRedeem,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Reduces a holding's units (equity) or principal (fixed income) by the
    given amount. Marks it 'redeemed' if that brings it to zero, otherwise
    'partially_redeemed'.

    For equities, this now also creates a Redemption record capturing what
    the sale actually executed at and the resulting realized gain/loss —
    previously the units were silently reduced with no transaction record
    at all. sale_price is REQUIRED for equity redemptions; without it there
    is no way to know what the units were sold for, and no way to ever show
    a client what they made or lost on the sale.

    The Redemption created here is recorded as already 'completed', not
    'pending' — this endpoint IS the admin's authoritative execution of the
    sale (can_manage_nav), so there is nothing left to approve afterward.
    This does not yet route through the separate client-initiated
    /redemptions request-and-approval flow (RedemptionCreate) — that
    remains a distinct path for clients requesting an early exit. Wiring
    the two together, if desired, is a separate decision.

    For fixed income, principal is reduced the same as before; no sale
    price/realized-gain concept applies, so no Redemption record is
    created for that branch (unchanged from prior behaviour).
    """
    holding = await db.get(PortfolioHolding, holding_id)
    if not holding:
        raise HTTPException(status_code=404, detail="Holding not found.")
    if holding.status == "redeemed":
        raise HTTPException(status_code=400, detail="This holding has already been fully redeemed.")

    redemption_record = None

    if holding.holding_type == "equity":
        if body.units_or_amount > (holding.units or 0):
            raise HTTPException(status_code=400, detail=f"Cannot redeem more than the {holding.units} units held.")
        if not body.sale_price:
            raise HTTPException(
                status_code=400,
                detail="sale_price is required when redeeming an equity holding, to record what the units sold for.",
            )

        sub = await db.get(Subscription, holding.subscription_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Underlying subscription not found for this holding.")

        cost_price_at_sale = holding.cost_price or 0.0
        proceeds = round(body.sale_price * body.units_or_amount, 2)
        realized_gain = round((body.sale_price - cost_price_at_sale) * body.units_or_amount, 2)

        holding.units -= body.units_or_amount
        holding.status = "redeemed" if holding.units <= 0 else "partially_redeemed"

        redemption_record = Redemption(
            user_id=sub.user_id,
            subscription_id=holding.subscription_id,
            holding_id=holding.id,
            amount=proceeds,
            currency=sub.currency,
            penalty=0.0,
            net_amount=proceeds,
            reference=_generate_redemption_reference(),
            is_premature=False,
            units_sold=body.units_or_amount,
            sale_price=body.sale_price,
            cost_price_at_sale=cost_price_at_sale,
            realized_gain=realized_gain,
            status="completed",
            note=body.note,
            processed_at=datetime.now(timezone.utc),
            processed_by=admin.full_name,
        )
        db.add(redemption_record)
    else:
        if body.units_or_amount > (holding.principal or 0):
            raise HTTPException(status_code=400, detail=f"Cannot redeem more than the principal of {holding.principal}.")
        holding.principal -= body.units_or_amount
        holding.status = "redeemed" if holding.principal <= 0 else "partially_redeemed"

    db.add(AuditLog(
        action="Portfolio Holding Redeemed", target=holding.instrument_name, target_id=str(holding_id),
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={
            "units_or_amount": body.units_or_amount, "note": body.note, "new_status": holding.status,
            **({
                "sale_price": redemption_record.sale_price,
                "realized_gain": redemption_record.realized_gain,
                "redemption_reference": redemption_record.reference,
            } if redemption_record else {}),
        },
    ))

    # Without this, the client's dashboard would keep showing whatever the
    # last full valuation run computed — silently ignoring this redemption
    # until someone happens to manually re-run valuation for everyone.
    await _revalue_subscription(holding.subscription_id, db, created_by=admin.id)

    await db.commit()
    await db.refresh(holding)
    return holding


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — INSTRUMENT PRICES
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/prices/batch",
    summary="Submit today's closing prices — can_manage_nav",
)
async def submit_prices(
    body: InstrumentPriceBatchSubmit,
    confirm: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Submits closing prices for one or more stocks in one call — one price
    entry revalues every client holding that stock, not per client.

    Two-step confirmation: called without confirm=true, this returns a
    preview showing which instrument names matched an existing holding and
    which didn't (possible typo, or a genuinely new stock nobody holds yet)
    — nothing is saved yet. Call again with confirm=true to actually save,
    after the Investment Manager has reviewed the unmatched list.
    """
    price_date = body.price_date or datetime.now(timezone.utc)

    known_result = await db.execute(
        select(PortfolioHolding.instrument_name).where(PortfolioHolding.holding_type == "equity").distinct()
    )
    known_instruments = {row[0] for row in known_result.all()}

    matched, unmatched = [], []
    for entry in body.entries:
        (matched if entry.instrument_name in known_instruments else unmatched).append(entry.instrument_name)

    if not confirm:
        return {
            "preview": True,
            "matched": matched,
            "unmatched": unmatched,
            "message": (
                f"{len(matched)} matched an existing holding. "
                f"{len(unmatched)} did not, please double check these for typos before confirming."
                if unmatched else f"All {len(matched)} instrument names matched existing holdings."
            ),
        }

    saved = 0
    for entry in body.entries:
        existing = await db.execute(
            select(InstrumentPrice).where(
                InstrumentPrice.instrument_name == entry.instrument_name,
                InstrumentPrice.price_date == price_date,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.price = entry.price
            row.entered_by = admin.id
        else:
            db.add(InstrumentPrice(
                instrument_name=entry.instrument_name, price=entry.price,
                price_date=price_date, source="manual", entered_by=admin.id,
            ))
        saved += 1

    db.add(AuditLog(
        action="Instrument Prices Submitted", target=f"{saved} instrument(s)",
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"price_date": price_date.isoformat(), "entries": [e.model_dump() for e in body.entries]},
    ))
    await db.commit()
    logger.info(f"Instrument prices submitted: {saved} entries by {admin.email}")
    return {"preview": False, "saved": saved, "message": f"{saved} price(s) saved."}


@router.get(
    "/prices/template",
    summary="Download the Excel template for bulk price upload — can_manage_nav",
)
async def download_price_template(
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Returns a ready-to-fill .xlsx with the correct column headers, so the
    Investment Manager doesn't have to guess the format. Two columns:
    Instrument Name, Closing Price.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    import io

    wb = Workbook()
    ws = wb.active
    ws.title = "Closing Prices"

    headers = ["Instrument Name", "Closing Price"]
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="A67C1A")
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 18

    # A couple of example rows to make the expected format obvious. The
    # upload endpoint skips any row whose price isn't a valid number, so
    # leaving these in by accident won't corrupt a real upload.
    ws.append(["MTN Nigeria (example - delete this row)", 210.50])
    ws.append(["Dangote Cement (example - delete this row)", 485.00])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="closing_prices_template.xlsx"'},
    )


@router.post(
    "/prices/upload",
    summary="Bulk upload closing prices from Excel — can_manage_nav",
)
async def upload_prices(
    file: UploadFile = File(...),
    price_date: Optional[datetime] = None,
    confirm: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Parses an uploaded .xlsx of closing prices and feeds it through the
    same preview/confirm path as manual entry, so unmatched instrument
    names still get flagged before anything saves.

    Expected columns (matching the downloadable template): Instrument
    Name, Closing Price. The header row is skipped, as is any row with a
    blank name or a non-numeric price.
    """
    from openpyxl import load_workbook
    import io

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file. Use the template for the correct format.")

    content = await file.read()
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read that file as an Excel workbook: {e}")

    ws = wb.active
    entries, skipped = [], []
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or len(row) < 2:
            continue
        name, price = row[0], row[1]
        if name is None or str(name).strip() == "":
            continue
        try:
            price_val = float(price)
            if price_val <= 0:
                raise ValueError("price must be positive")
        except (TypeError, ValueError):
            skipped.append({"row": row_idx, "name": str(name), "reason": "price is missing or not a valid number"})
            continue
        entries.append({"instrument_name": str(name).strip(), "price": price_val})

    if not entries:
        raise HTTPException(
            status_code=400,
            detail=f"No valid rows found in that file. {len(skipped)} row(s) were skipped.",
        )

    effective_date = price_date or datetime.now(timezone.utc)

    known_result = await db.execute(
        select(PortfolioHolding.instrument_name).where(PortfolioHolding.holding_type == "equity").distinct()
    )
    known_instruments = {row[0] for row in known_result.all()}
    matched = [e["instrument_name"] for e in entries if e["instrument_name"] in known_instruments]
    unmatched = [e["instrument_name"] for e in entries if e["instrument_name"] not in known_instruments]

    if not confirm:
        return {
            "preview": True, "parsed": len(entries), "skipped": skipped,
            "matched": matched, "unmatched": unmatched, "entries": entries,
            "message": (
                f"Parsed {len(entries)} price(s). {len(matched)} matched an existing holding, "
                f"{len(unmatched)} did not. {len(skipped)} row(s) skipped."
            ),
        }

    saved = 0
    for entry in entries:
        existing = await db.execute(
            select(InstrumentPrice).where(
                InstrumentPrice.instrument_name == entry["instrument_name"],
                InstrumentPrice.price_date == effective_date,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.price = entry["price"]
            row.entered_by = admin.id
            row.source = "excel"
        else:
            db.add(InstrumentPrice(
                instrument_name=entry["instrument_name"], price=entry["price"],
                price_date=effective_date, source="excel", entered_by=admin.id,
            ))
        saved += 1

    db.add(AuditLog(
        action="Instrument Prices Uploaded (Excel)", target=f"{saved} instrument(s)",
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"price_date": effective_date.isoformat(), "filename": file.filename,
                 "saved": saved, "skipped": skipped},
    ))
    await db.commit()
    logger.info(f"Prices uploaded from Excel: {saved} entries by {admin.email}")
    return {"preview": False, "saved": saved, "skipped": skipped, "message": f"{saved} price(s) saved from {file.filename}."}


@router.get(
    "/instruments",
    summary="List every instrument known to the system — can_manage_nav",
)
async def list_instruments(
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    A master list of instrument names, gathered from both holdings and price
    records. Instrument names are stored as free text on each holding rather
    than in a dedicated table, so "MTN Nigeria" and "MTN Nigeria PLC" are two
    separate instruments as far as the system is concerned, and a price
    entered for one won't value the other. This endpoint surfaces every name
    in use so those mismatches can be spotted and corrected.

    For each name: how many holdings use it, how many clients hold it, its
    latest price, and whether it has any price history at all. An instrument
    held by clients but with no price will never be valued, which is exactly
    the kind of problem this list is meant to make visible.
    """
    holdings_result = await db.execute(
        select(PortfolioHolding).where(PortfolioHolding.status != "redeemed")
    )
    holdings = holdings_result.scalars().all()

    prices_result = await db.execute(select(InstrumentPrice))
    prices = prices_result.scalars().all()

    names = {h.instrument_name for h in holdings} | {p.instrument_name for p in prices}

    out = []
    for name in sorted(names):
        matching = [h for h in holdings if h.instrument_name == name]
        name_prices = sorted(
            [p for p in prices if p.instrument_name == name],
            key=lambda p: p.price_date,
        )
        latest = name_prices[-1] if name_prices else None
        out.append({
            "name": name,
            "holding_count": len(matching),
            "client_count": len({str(h.subscription_id) for h in matching}),
            "holding_type": matching[0].holding_type if matching else "equity",
            "latest_price": latest.price if latest else None,
            "latest_price_date": latest.price_date.isoformat() if latest else None,
            "price_records": len(name_prices),
            # Flags a real problem: clients hold this, but it has no price,
            # so it currently values at zero in their portfolio.
            "needs_price": bool(matching and any(h.holding_type == "equity" for h in matching) and not name_prices),
        })
    return out


@router.patch(
    "/instruments/rename",
    summary="Rename an instrument everywhere it appears — can_manage_nav",
)
async def rename_instrument(
    body: InstrumentRename,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Corrects an instrument's name across BOTH holdings and price records in
    one action. Because the name is the only thing linking a price to a
    holding, renaming one without the other would silently break that link
    and the holding would stop being valued. This updates both together.

    If the new name already exists, the two are effectively merged: all
    holdings and prices end up under the new name. That's usually the intent
    (fixing "MTN Nigeria PLC" to match the existing "MTN Nigeria"), but it
    can't be undone automatically, so the response reports exactly what moved.
    """
    old_name = body.old_name.strip()
    new_name = body.new_name.strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="Both old and new names are required.")
    if old_name == new_name:
        raise HTTPException(status_code=400, detail="The new name is the same as the current one.")

    holdings_result = await db.execute(
        select(PortfolioHolding).where(PortfolioHolding.instrument_name == old_name)
    )
    holdings = holdings_result.scalars().all()

    prices_result = await db.execute(
        select(InstrumentPrice).where(InstrumentPrice.instrument_name == old_name)
    )
    prices = prices_result.scalars().all()

    if not holdings and not prices:
        raise HTTPException(status_code=404, detail=f"No holdings or prices found under '{old_name}'.")

    # Check whether this rename merges into an existing instrument
    existing_result = await db.execute(
        select(PortfolioHolding).where(PortfolioHolding.instrument_name == new_name).limit(1)
    )
    merges_into_existing = existing_result.scalar_one_or_none() is not None

    for h in holdings:
        h.instrument_name = new_name

    # Prices are unique per (name, date). If a price already exists under the
    # new name for the same date, keep that one and drop the duplicate rather
    # than letting the rename violate the constraint.
    skipped_dates = []
    for p in prices:
        clash = await db.execute(
            select(InstrumentPrice).where(
                InstrumentPrice.instrument_name == new_name,
                InstrumentPrice.price_date == p.price_date,
            )
        )
        if clash.scalar_one_or_none():
            skipped_dates.append(p.price_date.isoformat())
            await db.delete(p)
        else:
            p.instrument_name = new_name

    db.add(AuditLog(
        action="Instrument Renamed", target=f"{old_name} → {new_name}",
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={
            "old_name": old_name, "new_name": new_name,
            "holdings_updated": len(holdings), "prices_updated": len(prices) - len(skipped_dates),
            "duplicate_prices_dropped": skipped_dates,
            "merged_into_existing": merges_into_existing,
        },
    ))
    await db.commit()

    logger.info(f"Instrument renamed '{old_name}' -> '{new_name}' by {admin.email}")
    return {
        "old_name": old_name,
        "new_name": new_name,
        "holdings_updated": len(holdings),
        "prices_updated": len(prices) - len(skipped_dates),
        "duplicate_prices_dropped": len(skipped_dates),
        "merged_into_existing": merges_into_existing,
        "message": (
            f"Renamed across {len(holdings)} holding(s) and {len(prices) - len(skipped_dates)} price record(s)."
            + (f" Merged into the existing '{new_name}'." if merges_into_existing else "")
            + (f" {len(skipped_dates)} duplicate price(s) for dates already covered were removed." if skipped_dates else "")
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — RUN VALUATION
# ─────────────────────────────────────────────────────────────────────────────

async def _revalue_subscription(sub_id, db: AsyncSession, valuation_date=None, created_by=None) -> Optional[dict]:
    """
    Creates one fresh PortfolioValuation snapshot for a single subscription,
    from the CURRENT state of its holdings and latest known prices.

    Shared by the admin "Run Valuation" action below and anything that
    changes a holding's units directly (equity redemptions, in both
    portfolio.py and redemptions.py) — without this, a client's displayed
    portfolio value stays frozen at whatever the last full valuation run
    computed, silently ignoring any trade made since, until someone
    happens to manually re-run valuation for everyone.

    Does NOT commit — the caller commits as part of its own transaction,
    so a redemption and its resulting valuation land together atomically.
    Returns the snapshot summary, or None if there are no active holdings
    left to value (e.g. the position was fully redeemed).
    """
    valuation_date = valuation_date or datetime.now(timezone.utc)
    holdings_result = await db.execute(
        select(PortfolioHolding).where(
            PortfolioHolding.subscription_id == sub_id,
            PortfolioHolding.status != "redeemed",
        )
    )
    holdings = holdings_result.scalars().all()
    if not holdings:
        return None

    breakdown = [await _value_holding(h, valuation_date, db) for h in holdings]
    equities_value = round(sum(b["value"] for b in breakdown if b["type"] == "equity"), 2)
    fixed_income_value = round(sum(b["value"] for b in breakdown if b["type"] == "fixed_income"), 2)
    total_value = round(equities_value + fixed_income_value, 2)

    snapshot = PortfolioValuation(
        subscription_id=sub_id, valuation_date=valuation_date,
        equities_value=equities_value, fixed_income_value=fixed_income_value,
        total_value=total_value, breakdown=breakdown, created_by=created_by,
    )
    db.add(snapshot)
    return {"subscription_id": str(sub_id), "total_value": total_value}


@router.post(
    "/run-valuation",
    summary="Revalue portfolios using the latest prices — can_manage_nav",
)
async def run_valuation(
    body: RunValuationRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    """
    Recalculates every active holding for the given portfolios (or every
    portfolio with active holdings, if none specified) using the latest
    instrument prices / accrual formulas, and saves one snapshot per
    portfolio. This is what the client's historical chart is built from.
    """
    valuation_date = body.valuation_date or datetime.now(timezone.utc)

    if body.subscription_ids:
        sub_ids = body.subscription_ids
    else:
        result = await db.execute(
            select(PortfolioHolding.subscription_id).where(PortfolioHolding.status != "redeemed").distinct()
        )
        sub_ids = [row[0] for row in result.all()]

    snapshots = []
    for sub_id in sub_ids:
        result = await _revalue_subscription(sub_id, db, valuation_date, admin.id)
        if result:
            snapshots.append(result)

    db.add(AuditLog(
        action="Portfolio Valuation Run", target=f"{len(snapshots)} portfolio(s)",
        action_type="admin", performed_by=admin.id, performed_by_name=admin.full_name,
        details={"valuation_date": valuation_date.isoformat(), "results": snapshots},
    ))
    await db.commit()
    logger.info(f"Valuation run: {len(snapshots)} portfolio(s) revalued by {admin.email}")
    return {"revalued": len(snapshots), "results": snapshots}


@router.get(
    "/subscriptions/{subscription_id}/valuations",
    response_model=list[PortfolioValuationResponse],
    summary="Valuation history for one portfolio — can_manage_nav",
)
async def get_valuation_history_admin(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_staff_permission("can_manage_nav")),
):
    result = await db.execute(
        select(PortfolioValuation)
        .where(PortfolioValuation.subscription_id == subscription_id)
        .order_by(PortfolioValuation.valuation_date.asc())
    )
    return result.scalars().all()


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC — CLIENT DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

def _group_equity_holdings(holdings: list[PortfolioHolding]) -> list[dict]:
    """
    Groups equity holdings by instrument_name for client-facing display,
    so someone who bought MTN N twice at different prices sees ONE "MTN N"
    line instead of two separate rows. Purely a display transform — the
    underlying PortfolioHolding rows are untouched, so admin tooling,
    per-lot redemption, and audit history all keep working exactly as
    before against the individual lots.

    Fixed income holdings are passed through unchanged (one row each) —
    each placement has its own maturity date and rate, so merging them
    would lose information a client actually needs to see.

    Weighted-average cost_price is units-weighted across the lots being
    grouped, purely for display. lot_ids/lot_count are included so the
    frontend can, if useful, indicate "bought in 2 purchases".
    """
    equity = [h for h in holdings if h.holding_type == "equity"]
    fixed_income = [h for h in holdings if h.holding_type != "equity"]

    grouped: dict[str, dict] = {}
    for h in equity:
        g = grouped.setdefault(h.instrument_name, {
            "id": None,  # synthetic group, not a single holding id — see lot_ids
            "type": "equity",
            "name": h.instrument_name,
            "units": 0.0,
            "_cost_total": 0.0,   # units * cost_price, summed — divided out below
            "lot_ids": [],
            "statuses": set(),
        })
        units = h.units or 0.0
        g["units"] += units
        g["_cost_total"] += units * (h.cost_price or 0.0)
        g["lot_ids"].append(str(h.id))
        g["statuses"].add(h.status)

    out = []
    for g in grouped.values():
        cost_price = round(g["_cost_total"] / g["units"], 4) if g["units"] else None
        out.append({
            "id": g["lot_ids"][0] if len(g["lot_ids"]) == 1 else None,
            "type": "equity",
            "name": g["name"],
            "units": g["units"],
            "cost_price": cost_price,
            "principal": None,
            "roi_pct": None,
            "maturity_date": None,
            "status": "partially_redeemed" if "partially_redeemed" in g["statuses"] else "active",
            "lot_ids": g["lot_ids"],
            "lot_count": len(g["lot_ids"]),
        })

    out.extend({
        "id": str(h.id), "type": h.holding_type, "name": h.instrument_name,
        "units": h.units, "cost_price": h.cost_price,
        "principal": h.principal, "roi_pct": h.roi_pct,
        "maturity_date": h.maturity_date.isoformat() if h.maturity_date else None,
        "status": h.status,
        "lot_ids": [str(h.id)], "lot_count": 1,
    } for h in fixed_income)

    return out


def _group_equity_breakdown(breakdown: Optional[list]) -> list:
    """
    Same grouping as _group_equity_holdings, applied to a valuation
    snapshot's per-holding breakdown (see PortfolioValuation.breakdown /
    _value_holding), so the current value shown against the merged
    display row is the combined value of all its underlying lots, not
    just one of them.
    """
    if not breakdown:
        return []

    equity = [b for b in breakdown if b.get("type") == "equity"]
    other = [b for b in breakdown if b.get("type") != "equity"]

    grouped: dict[str, dict] = {}
    for b in equity:
        g = grouped.setdefault(b["name"], {
            "name": b["name"], "type": "equity",
            "units": 0.0, "value": 0.0, "price": b.get("price"), "lot_ids": [],
        })
        g["units"] += b.get("units") or 0.0
        g["value"] += b.get("value") or 0.0
        g["lot_ids"].append(b.get("holding_id"))

    return other + [
        {
            "holding_id": g["lot_ids"][0] if len(g["lot_ids"]) == 1 else None,
            "lot_ids": g["lot_ids"],
            "name": g["name"], "type": "equity",
            "units": round(g["units"], 4),
            "price": g["price"],
            "value": round(g["value"], 2),
        }
        for g in grouped.values()
    ]


@public_router.get("/my-holdings", summary="My portfolio holdings, current value, and history")
async def my_holdings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns every active portfolio (Subscription) the client holds, with
    its current holdings breakdown and valuation history for charting.
    Falls back gracefully if no valuation has been run yet.

    Equity holdings are grouped by instrument name for display (see
    _group_equity_holdings) — a client who bought the same stock twice at
    different prices sees one combined line, not two. This is a display-
    only transform; nothing about how holdings/redemptions/audit history
    are stored on the backend changes.
    """
    subs_result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == current_user.id,
            Subscription.status == "active",
        )
    )
    subs = subs_result.scalars().all()

    # Global admin setting: when disabled, clients see their total value and
    # chart but NOT the position-by-position detail. Enforced here rather than
    # only hidden in the UI, so the holdings genuinely aren't sent to the
    # browser at all.
    settings_result = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    sys_settings = settings_result.scalar_one_or_none()
    show_breakdown = sys_settings.show_portfolio_breakdown if sys_settings else True

    out = []
    for sub in subs:
        holdings_result = await db.execute(
            select(PortfolioHolding).where(
                PortfolioHolding.subscription_id == sub.id,
                PortfolioHolding.status != "redeemed",
            )
        )
        holdings = holdings_result.scalars().all()
        if not holdings:
            continue  # not a private portfolio subscription, skip

        history_result = await db.execute(
            select(PortfolioValuation)
            .where(PortfolioValuation.subscription_id == sub.id)
            .order_by(PortfolioValuation.valuation_date.asc())
        )
        history = history_result.scalars().all()
        latest = history[-1] if history else None

        out.append({
            "subscription_id": str(sub.id),
            "reference": sub.reference,
            "current_value": latest.total_value if latest else None,
            "equities_value": latest.equities_value if latest else None,
            "fixed_income_value": latest.fixed_income_value if latest else None,
            "as_of": latest.valuation_date.isoformat() if latest else None,
            # Per-holding values from the most recent valuation only. The
            # dashboard needs these to show each holding's current value and
            # its gain/loss against cost price. Deliberately not sent for
            # every historical snapshot, since the chart only needs totals
            # and sending every breakdown would bloat the response.
            "show_breakdown": show_breakdown,
            "latest_breakdown": _group_equity_breakdown(latest.breakdown if latest else []) if show_breakdown else [],
            "holdings": _group_equity_holdings(holdings) if show_breakdown else [],
            "value_history": [
                # `breakdown` (per-holding values) is only included on the
                # LATEST snapshot — that's all the dashboard needs to show
                # each holding's current value and gain/loss colouring, and
                # sending it for every historical point would bloat the
                # response for no benefit (the chart only needs the totals).
                {"date": v.valuation_date.isoformat(), "total_value": v.total_value,
                 "equities_value": v.equities_value, "fixed_income_value": v.fixed_income_value,
                 **({"breakdown": _group_equity_breakdown(v.breakdown)} if show_breakdown and latest and v.id == latest.id else {})}
                for v in history
            ],
        })

    return out
