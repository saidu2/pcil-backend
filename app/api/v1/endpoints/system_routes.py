# app/api/v1/endpoints/system_routes.py
#
# PUBLIC endpoints — no authentication required.
# Mount in main.py as: app.include_router(system_router, prefix="/api/v1/system")
#
# These are called by the frontend for all visitors (not just logged-in users).

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import Announcement   # adjust if your model is named differently

system_router = APIRouter()


@system_router.get("/announcements", summary="Public: Get active announcements")
async def get_active_announcements(db: AsyncSession = Depends(get_db)):
    """
    Returns all active announcements.
    Called by AnnouncementBanner on the client-facing pages.
    No authentication required.
    """
    result = await db.execute(
        select(Announcement)
        .where(Announcement.is_active == True)
        .order_by(Announcement.published_at.desc())
    )
    announcements = result.scalars().all()
    return [
        {
            "id":           str(a.id),
            "title":        a.title,
            "body":         a.body,
            "is_active":    a.is_active,
            "audience":     getattr(a, "audience", "all"),
            "published_at": a.published_at.isoformat() if a.published_at else None,
        }
        for a in announcements
    ]
