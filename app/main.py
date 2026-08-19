# ─────────────────────────────────────────────────────────────────────────────
# app/main.py  (UPDATED — Session 10)
#
# Changes from previous version:
#   + SecurityHeadersMiddleware  — security response headers on every request
#   + RateLimitMiddleware        — IP-based rate limiting on auth endpoints
#   + GZipMiddleware             — compresses responses > 1KB (scaling)
#   + nav router registered      — new NAV / returns management admin section
#
# Run: uvicorn app.main:app --reload --port 8000
# ─────────────────────────────────────────────────────────────────────────────

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.core.config import settings
from app.core.security_middleware import SecurityHeadersMiddleware, RateLimitMiddleware
from app.api.v1.endpoints import (
    auth, webhooks, products, kyc, subscriptions,
    redemptions, system, admin_users, nav, staff_roles, workflows, portfolio, certificates,
)

logging.basicConfig(
    level=logging.DEBUG if settings.APP_DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 55)
    logger.info(f"  {settings.APP_NAME}")
    logger.info(f"  Environment : {settings.APP_ENV}")
    logger.info(f"  Docs        : http://localhost:8000/docs")
    logger.info("=" * 55)
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Prime Capital & Investment Ltd — Investment Portal API\n\n"
        "**Client auth:** Bearer token from `POST /api/v1/auth/login`\n\n"
        "**Admin auth:** Bearer token from `POST /api/v1/auth/admin/login`"
    ),
    version="1.1.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    lifespan=lifespan,
)

# ── Middleware (order matters — first added = outermost = runs last on response)
# Security headers — wraps everything, adds headers to every response
app.add_middleware(SecurityHeadersMiddleware)

# Rate limiting — blocks excessive login attempts before they hit the DB
app.add_middleware(RateLimitMiddleware)

# GZip compression — shrinks JSON responses > 1KB (helps on slow connections)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# CORS — must come after security middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-D365-Secret"],
    expose_headers=["Content-Disposition"],
)

V1 = "/api/v1"

# ── Static files (uploaded KYC documents, logo, etc.) ────────────────────────
# NEW — local disk storage as a working interim solution until real cloud
# storage (Azure Blob, S3, etc.) is configured for deployment. Files land in
# app/static/ and are served directly at /static/... — e.g. a KYC document
# saved to app/static/kyc-documents/{id}.pdf is reachable at
# http://localhost:8000/static/kyc-documents/{id}.pdf. Swap for real cloud
# storage in Phase 2 (deployment) without changing how URLs are consumed
# elsewhere in the app — just the upload endpoint's implementation changes.
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "kyc-documents").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health", tags=["Health"])
async def health():
    """Azure App Service pings this to verify the app is alive."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": "1.1.0",
        "environment": settings.APP_ENV,
    }


# ── Client + Auth routers ─────────────────────────────────────────────────────
app.include_router(auth.router,                prefix=f"{V1}/auth",              tags=["Auth"])
app.include_router(auth.admin_router,          prefix=f"{V1}/admin/clients",        tags=["Admin — Clients"])
app.include_router(webhooks.router,            prefix=f"{V1}/webhooks",          tags=["D365 Webhooks"])
app.include_router(products.router,            prefix=f"{V1}/products",          tags=["Products"])
app.include_router(kyc.router,                 prefix=f"{V1}/kyc",               tags=["KYC"])
app.include_router(subscriptions.router,       prefix=f"{V1}/subscriptions",     tags=["Subscriptions"])
app.include_router(redemptions.router,         prefix=f"{V1}/redemptions",       tags=["Redemptions"])
app.include_router(system.client_router,       prefix=f"{V1}/notifications",     tags=["Notifications"])
app.include_router(system.public_router,       prefix=f"{V1}/system",            tags=["System"])

# ── Admin routers ─────────────────────────────────────────────────────────────
app.include_router(products.admin_router,      prefix=f"{V1}/admin/products",        tags=["Admin — Products"])
app.include_router(kyc.admin_router,           prefix=f"{V1}/admin/kyc",             tags=["Admin — KYC"])
app.include_router(workflows.kyc_export_router, prefix=f"{V1}/admin/kyc",            tags=["Admin — KYC"])
app.include_router(subscriptions.admin_router, prefix=f"{V1}/admin/subscriptions",   tags=["Admin — Subscriptions"])
app.include_router(redemptions.admin_router,   prefix=f"{V1}/admin/redemptions",     tags=["Admin — Redemptions"])
app.include_router(certificates.admin_router,  prefix=f"{V1}/admin/certificates",    tags=["Admin — Certificates"])
app.include_router(system.admin_router,        prefix=f"{V1}/admin",                 tags=["Admin — Settings & Reports"])
app.include_router(admin_users.router,         prefix=f"{V1}/admin/users",           tags=["Admin — Users"])
app.include_router(staff_roles.router,         prefix=f"{V1}/admin/staff-roles",     tags=["Admin — Staff Roles"])
app.include_router(workflows.router,           prefix=f"{V1}/admin/workflows",       tags=["Admin — Workflows"])
app.include_router(nav.router,                 prefix=f"{V1}/admin/nav",             tags=["Admin — NAV & Returns"])
app.include_router(nav.public_router,          prefix=f"{V1}/nav",                   tags=["NAV & Returns"])
app.include_router(portfolio.router,           prefix=f"{V1}/admin/portfolio",       tags=["Admin — Private Portfolio"])
app.include_router(portfolio.public_router,    prefix=f"{V1}/portfolio",             tags=["Portfolio"])
app.include_router(portfolio.router,           prefix=f"{V1}/admin/portfolio",       tags=["Admin — Private Portfolio"])
app.include_router(portfolio.public_router,    prefix=f"{V1}/portfolio",             tags=["Portfolio"])

logger.info("All routers registered ✓")
