# ─────────────────────────────────────────────────────────────────────────────
# app/api/v1/endpoints/products.py
# ─────────────────────────────────────────────────────────────────────────────

import logging
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.models import Product, AuditLog, PaymentAccount
from app.schemas.schemas import ProductCreate, ProductUpdate, MessageResponse
from app.api.v1.deps import require_super_admin, require_permission

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


def _serialize(p: Product) -> dict:
    pa = None
    try:
        pa = p.payment_account
    except Exception:
        pass
    return {
        "id": str(p.id), "name": p.name, "category": p.category,
        "product_type": p.product_type, "currency": p.currency,
        "discretionary": p.discretionary, "min_amount": p.min_amount,
        "min_amount_display": p.min_amount_display, "roi": p.roi,
        "duration": p.duration, "risk": p.risk,
        "target_investors": p.target_investors, "description": p.description,
        "features": p.features, "is_active": p.is_active,
        "payment_account_id": str(p.payment_account_id) if getattr(p, "payment_account_id", None) else None,
        "payment_account": {
            "id": str(pa.id), "bank": pa.bank, "account_name": pa.account_name,
            "account_number": pa.account_number, "currency": pa.currency,
            "label": pa.label, "instruction": pa.instruction,
        } if pa else None,
    }


async def _fetch(db, product_id):
    r = await db.execute(
        select(Product).options(selectinload(Product.payment_account)).where(Product.id == product_id)
    )
    return r.scalar_one_or_none()


@router.get("")
async def list_products(db: AsyncSession = Depends(get_db)):
    r = await db.execute(
        select(Product).options(selectinload(Product.payment_account))
        .where(Product.is_active == True).order_by(Product.name)
    )
    return [_serialize(p) for p in r.scalars().all()]


@router.get("/{product_id}")
async def get_product(product_id: UUID, db: AsyncSession = Depends(get_db)):
    r = await db.execute(
        select(Product).options(selectinload(Product.payment_account))
        .where(Product.id == product_id, Product.is_active == True)
    )
    p = r.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Product not found.")
    return _serialize(p)


@admin_router.get("")
async def admin_list_products(db: AsyncSession = Depends(get_db), admin=Depends(require_permission("products"))):
    r = await db.execute(
        select(Product).options(selectinload(Product.payment_account)).order_by(Product.name)
    )
    return [_serialize(p) for p in r.scalars().all()]


@admin_router.post("", status_code=201)
async def create_product(body: ProductCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_permission("products"))):
    p = Product(**body.model_dump())
    db.add(p)
    db.add(AuditLog(action="Product Created", target=body.name, action_type="product",
                    performed_by=admin.id, performed_by_name=admin.full_name))
    await db.commit()
    await db.refresh(p)
    p = await _fetch(db, p.id)
    logger.info(f"Product created: {body.name} by {admin.email}")
    return _serialize(p)


@admin_router.patch("/{product_id}")
async def update_product(product_id: UUID, body: ProductUpdate, db: AsyncSession = Depends(get_db), admin=Depends(require_permission("products"))):
    p = await _fetch(db, product_id)
    if not p:
        raise HTTPException(404, "Product not found.")
    updates = body.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(p, k, v)
    # Serialize updates for audit log — convert UUID/non-JSON types to strings
    safe_details = {k: str(v) if not isinstance(v, (str, int, float, bool, list, dict, type(None))) else v for k, v in updates.items()}
    db.add(AuditLog(action="Product Updated", target=p.name, target_id=str(product_id),
                    action_type="product", performed_by=admin.id, performed_by_name=admin.full_name, details=safe_details))
    await db.commit()
    p = await _fetch(db, product_id)
    return _serialize(p)


@admin_router.delete("/{product_id}", response_model=MessageResponse)
async def deactivate_product(product_id: UUID, db: AsyncSession = Depends(get_db), admin=Depends(require_super_admin)):
    r = await db.execute(select(Product).where(Product.id == product_id))
    p = r.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Product not found.")
    p.is_active = False
    db.add(AuditLog(action="Product Deactivated", target=p.name, target_id=str(product_id),
                    action_type="product", performed_by=admin.id, performed_by_name=admin.full_name))
    await db.commit()
    return MessageResponse(message=f"Product '{p.name}' deactivated.")
