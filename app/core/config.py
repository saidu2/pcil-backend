# ─────────────────────────────────────────────────────────────────────────────
# app/core/config.py
#
# Central settings management using pydantic-settings.
# All values are read from the .env file automatically.
# Add any new environment variable here before using it in the app.
# ─────────────────────────────────────────────────────────────────────────────

from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl
from typing import List


class Settings(BaseSettings):
    """
    Application settings loaded from .env file.
    Pydantic validates every value on startup — the app will crash with a
    clear error message if a required variable is missing or wrongly typed.
    This is intentional: better to fail at startup than silently at runtime.
    """

    # ── Application ───────────────────────────────────────────────────────────
    APP_NAME: str = "Prime Capital & Investment Ltd"
    APP_ENV: str = "development"          # development | staging | production
    APP_DEBUG: bool = True
    APP_URL: str = "http://localhost:8000"
    FRONTEND_URL: str = "http://localhost:5173"

    # ── Security / JWT ────────────────────────────────────────────────────────
    SECRET_KEY: str                        # REQUIRED — must be set in .env
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str                      # REQUIRED — PostgreSQL connection string

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Azure Blob Storage ────────────────────────────────────────────────────
    AZURE_STORAGE_CONNECTION_STRING: str = ""
    AZURE_STORAGE_ACCOUNT_NAME: str = ""
    AZURE_KYC_CONTAINER: str = "kyc-documents"
    AZURE_RECEIPTS_CONTAINER: str = "payment-receipts"
    AZURE_CERTS_CONTAINER: str = "investment-certs"
    AZURE_SIGNATURES_CONTAINER: str = "signatures"

    # ── SendGrid ──────────────────────────────────────────────────────────────
    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = "noreply@primecapital.ng"
    SENDGRID_FROM_NAME: str = "Prime Capital & Investment Ltd"
    # SendGrid dynamic template IDs — create these in SendGrid dashboard
    SENDGRID_WELCOME_TEMPLATE: str = ""
    SENDGRID_KYC_APPROVED_TEMPLATE: str = ""
    SENDGRID_KYC_DENIED_TEMPLATE: str = ""
    SENDGRID_CERT_TEMPLATE: str = ""
    SENDGRID_SUB_ACTIVATED_TEMPLATE: str = ""

    # ── Microsoft Dynamics 365 (ERP) ─────────────────────────────────────────
    # These are used by app/services/d365_service.py
    # See .env.example for instructions on how to obtain these values
    AZURE_TENANT_ID: str = ""
    AZURE_CLIENT_ID: str = ""
    AZURE_CLIENT_SECRET: str = ""
    D365_BASE_URL: str = ""               # e.g. https://yourorg.crm.dynamics.com/api/data/v9.2
    D365_KYC_ENTITY: str = "pcil_kycsubmissions"
    D365_SUBSCRIPTION_ENTITY: str = "pcil_subscriptions"
    D365_WEBHOOK_SECRET: str = ""

    # ── Admin Bootstrap ───────────────────────────────────────────────────────
    SUPER_ADMIN_EMAIL: str = "admin@primecapital.ng"
    SUPER_ADMIN_PASSWORD: str = "ChangeThisImmediately!"
    SUPER_ADMIN_NAME: str = "Saidu Safiyanu"

    # ── CORS Origins ──────────────────────────────────────────────────────────
    # The list of frontend URLs allowed to call this API.
    # In production, replace with your actual Azure Static Web Apps URL.
    @property
    def CORS_ORIGINS(self) -> List[str]:
        return [
            self.FRONTEND_URL,
            "http://localhost:5173",   # Vite dev server
            "http://localhost:3000",   # Alternative dev port
            # TODO: Add production URL when deploying to Azure
            # "https://primecapital.azurestaticapps.net",
        ]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    class Config:
        env_file = ".env"
        case_sensitive = True


# ── Singleton instance ────────────────────────────────────────────────────────
# Import this anywhere in the app: from app.core.config import settings
settings = Settings()
