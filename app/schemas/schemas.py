# ─────────────────────────────────────────────────────────────────────────────
# app/schemas/schemas.py
#
# Pydantic v2 schemas — define what the API accepts (request) and returns
# (response). These are completely separate from SQLAlchemy models.
#
# Naming convention:
#   XxxCreate  — body of POST requests (creating a resource)
#   XxxUpdate  — body of PUT/PATCH requests (updating a resource)
#   XxxResponse — shape of what the API returns to the client
# ─────────────────────────────────────────────────────────────────────────────

from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator
from typing import Optional, List, Any, Literal
from datetime import datetime
from uuid import UUID


# ─────────────────────────────────────────────────────────────────────────────
# AUTH SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class _NormalisedEmailMixin(BaseModel):
    """
    Shared input hygiene for anything accepting an email address or free-text
    name.

    On SQL injection specifically: the app builds no raw SQL anywhere, and
    SQLAlchemy sends every value as a bound parameter, so input like
    "' OR '1'='1" is stored and compared as literal text and can never be
    executed. These validators are about DATA QUALITY rather than injection.

    What they actually prevent:
      - Duplicate accounts from "John@Gmail.com" vs "john@gmail.com", which
        would also break password reset lookups for one of the two.
      - Leading/trailing whitespace from copy-paste, which silently makes a
        login fail with no visible reason.
      - Control characters and absurdly long values in names.
    """

    @field_validator("email", mode="before", check_fields=False)
    @classmethod
    def _normalise_email(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("full_name", "phone", mode="before", check_fields=False)
    @classmethod
    def _clean_text(cls, v):
        if not isinstance(v, str):
            return v
        # Strip control characters (including any newlines pasted in), then
        # collapse runs of whitespace so "John   Doe" stores as "John Doe".
        cleaned = "".join(ch for ch in v if ch.isprintable())
        return " ".join(cleaned.split()).strip()

    @field_validator("full_name", check_fields=False)
    @classmethod
    def _name_must_have_letters(cls, v):
        if isinstance(v, str) and v and not any(ch.isalpha() for ch in v):
            raise ValueError("Please enter a valid name.")
        return v


class UserRegister(_NormalisedEmailMixin):
    """Body for POST /api/v1/auth/register"""
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=30)
    account_type: str = Field(default="individual")


class AdminCreateClient(_NormalisedEmailMixin):
    """
    Body for POST /api/v1/admin/clients — staff creates a client account
    on the client's behalf (NEW in v11). Gated by StaffRole.can_manage_clients,
    not limited to a specific role/department — any staff role configured
    with that flag can use this.

    Two modes (NEW):
      - onboarding_type="new" (default) — client starts fresh, goes through
        KYC and subscription themselves via the portal. Unchanged behavior.
      - onboarding_type="existing" — client was already onboarded offline
        (KYC done on paper, payment already made, already in D365). Staff
        enters their existing investment details directly; the account is
        created with KYC pre-approved and an active subscription already
        in place, so the client's first login shows a real portfolio
        instead of an empty "please submit KYC" state.
    """
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    phone: Optional[str] = None
    account_type: str = Field(default="individual")
    temp_password: Optional[str] = Field(default=None, min_length=8, max_length=100)
    # If omitted, the backend generates a secure random temp password.

    onboarding_type: str = Field(default="new")  # "new" | "existing"

    # Only used when onboarding_type == "existing":
    product_id: Optional[UUID] = None
    investment_amount: Optional[float] = Field(default=None, gt=0)
    investment_currency: str = Field(default="NGN")
    investment_start_date: Optional[datetime] = None
    investment_maturity_date: Optional[datetime] = None
    d365_reference: Optional[str] = None


class ClientCreatedResponse(BaseModel):
    """
    Response from POST /api/v1/admin/clients. temp_password is shown here
    ONCE — it's hashed immediately on the server and cannot be retrieved
    again, so the creating staff member must share it with the client now.
    """
    id: UUID
    full_name: str
    email: str
    temp_password: str
    subscription_created: bool = False  # NEW — true if onboarding_type="existing" set up an active subscription
    message: str = "Share this temporary password with the client now — it cannot be retrieved again."


class UserLogin(_NormalisedEmailMixin):
    """Body for POST /api/v1/auth/login"""
    email: EmailStr
    password: str


class ChangePassword(BaseModel):
    """
    Body for POST /api/v1/auth/change-password.
    Used for the forced temp-password change flow (NEW in v11) and for
    any future voluntary password change.
    """
    current_password: str
    new_password: str = Field(min_length=8, max_length=100)


class TokenResponse(BaseModel):
    """Response from login/register/refresh — returned to frontend"""
    access_token: Optional[str] = None
    token_type: str = "bearer"
    must_change_password: bool = False  # NEW in v11 — True if client has a temp password active
    mfa_required: bool = False  # NEW in v11 — True if this account has MFA enabled (client MFA is optional)
    mfa_token: Optional[str] = None  # NEW in v11 — set alongside mfa_required, exchanged at /auth/mfa/login-verify
    email_verification_required: bool = False  # NEW — True on register(), signals frontend to route to /verify-email-pending instead of the dashboard
    # Note: refresh_token is set as an HttpOnly cookie, not in this body


class AdminLogin(_NormalisedEmailMixin):
    """Body for POST /api/v1/auth/admin/login"""
    email: EmailStr
    password: str


class AdminLoginResponse(BaseModel):
    """
    Response from POST /api/v1/auth/admin/login (NEW in v11 — replaces the
    plain TokenResponse for admins specifically, to support the two-step
    MFA handshake).

    Three possible shapes:
      1. MFA already enabled     -> mfa_required=True, mfa_token set, no access_token yet.
                                     Frontend must call /auth/admin/mfa/login-verify next.
      2. MFA not yet set up      -> access_token set normally, but
                                     mfa_setup_required=True. Frontend should log the
                                     admin in AND push them to enroll (mandatory for staff).
      3. (future) MFA disabled entirely for this deployment -> same as case 2
                                     but mfa_setup_required stays False. Not used yet —
                                     every admin currently gets nudged to enroll.
    """
    access_token: Optional[str] = None
    token_type: str = "bearer"
    mfa_required: bool = False
    mfa_token: Optional[str] = None
    mfa_setup_required: bool = False
    # True when the account is on a temp password (freshly created, or reset
    # by a super admin) and must set a new one before using the panel.
    must_change_password: bool = False


class MfaSetupResponse(BaseModel):
    """
    Response from POST /auth/admin/mfa/setup. secret is shown once for
    manual entry as a fallback if the QR code can't be scanned — same
    secret is encoded into qr_code, not a separate one.
    """
    secret: str
    qr_code: str  # data:image/png;base64,... — ready for an <img src>
    otpauth_uri: str


class MfaVerifyRequest(BaseModel):
    """Body for POST /auth/admin/mfa/verify — confirms enrollment."""
    code: str = Field(min_length=6, max_length=6)


class MfaLoginVerifyRequest(BaseModel):
    """Body for POST /auth/admin/mfa/login-verify — second step of login."""
    mfa_token: str
    code: str = Field(min_length=6, max_length=6)


class MfaDisableRequest(BaseModel):
    """
    Body for POST /auth/mfa/disable (client-side only — client MFA is
    optional, so unlike staff, clients can turn it back off). Requires a
    current valid code as proof of possession before disabling.
    """
    code: str = Field(min_length=6, max_length=6)


# ─────────────────────────────────────────────────────────────────────────────
# USER SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    """
    Public user profile returned to the frontend after login.
    Maps to what AuthContext.jsx stores in sessionStorage.
    Never include hashed_password in responses.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    phone: Optional[str]
    account_type: str
    kyc_status: str
    kyc_denied_reason: Optional[str]
    is_active: bool
    is_verified: bool
    temp_password_active: bool
    mfa_enabled: bool
    avatar_url: Optional[str] = None
    created_at: datetime


class UserUpdate(BaseModel):
    """Body for PATCH /api/v1/users/me"""
    full_name: Optional[str] = None
    phone: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN USER SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# STAFF ROLES (NEW in v11)
# ─────────────────────────────────────────────────────────────────────────────

STAFF_PERMISSION_FIELDS = [
    "can_manage_staff_users", "can_configure_roles", "can_manage_clients",
    "can_approve_kyc", "can_manage_subscriptions", "can_manage_redemptions",
    "can_enter_valuations", "can_manage_nav", "can_configure_workflows",
    "can_manage_products", "can_manage_fees", "can_manage_certificates",
    "can_manage_maturity", "can_manage_payments", "can_view_reports",
    "can_view_audit_log", "can_manage_system_settings",
]


class StaffRoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    department: Optional[str]
    description: Optional[str]
    is_active: bool
    can_manage_staff_users: bool
    can_configure_roles: bool
    can_manage_clients: bool
    can_approve_kyc: bool
    can_manage_subscriptions: bool
    can_manage_redemptions: bool
    can_enter_valuations: bool
    can_manage_nav: bool
    can_configure_workflows: bool
    can_manage_products: bool
    can_manage_fees: bool
    can_manage_certificates: bool
    can_manage_maturity: bool
    can_manage_payments: bool
    can_view_reports: bool
    can_view_audit_log: bool
    can_manage_system_settings: bool
    created_at: datetime


class StaffRoleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    department: Optional[str] = None
    description: Optional[str] = None
    can_manage_staff_users: bool = False
    can_configure_roles: bool = False
    can_manage_clients: bool = False
    can_approve_kyc: bool = False
    can_manage_subscriptions: bool = False
    can_manage_redemptions: bool = False
    can_enter_valuations: bool = False
    can_manage_nav: bool = False
    can_configure_workflows: bool = False
    can_manage_products: bool = False
    can_manage_fees: bool = False
    can_manage_certificates: bool = False
    can_manage_maturity: bool = False
    can_manage_payments: bool = False
    can_view_reports: bool = False
    can_view_audit_log: bool = False
    can_manage_system_settings: bool = False


class StaffRoleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    department: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    can_manage_staff_users: Optional[bool] = None
    can_configure_roles: Optional[bool] = None
    can_manage_clients: Optional[bool] = None
    can_approve_kyc: Optional[bool] = None
    can_manage_subscriptions: Optional[bool] = None
    can_manage_redemptions: Optional[bool] = None
    can_enter_valuations: Optional[bool] = None
    can_manage_nav: Optional[bool] = None
    can_configure_workflows: Optional[bool] = None
    can_manage_products: Optional[bool] = None
    can_manage_fees: Optional[bool] = None
    can_manage_certificates: Optional[bool] = None
    can_manage_maturity: Optional[bool] = None
    can_manage_payments: Optional[bool] = None
    can_view_reports: Optional[bool] = None
    can_view_audit_log: Optional[bool] = None
    can_manage_system_settings: Optional[bool] = None


class AssignStaffRole(BaseModel):
    """Body for PATCH /api/v1/admin/users/{id}/role"""
    staff_role_id: Optional[UUID] = None  # None = unassign


class AdminUserResponse(BaseModel):
    """
    Admin profile returned after admin login.

    NOTE: `staff_role` is only populated when the query that produced this
    admin eager-loads the relationship (selectinload(AdminUser.staff_role)).
    Async SQLAlchemy cannot lazy-load during Pydantic serialization — accessing
    an un-loaded relationship here would raise MissingGreenlet, not just
    return None. Every endpoint returning AdminUserResponse must eager-load it.
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    role: str
    permissions: Optional[List[str]]
    department: Optional[str]
    mfa_enabled: bool
    is_active: bool
    staff_role_id: Optional[UUID] = None
    staff_role: Optional[StaffRoleResponse] = None
    created_at: datetime


class AdminUserCreate(_NormalisedEmailMixin):
    """Body for POST /api/v1/admin/users — super admin creates junior admin"""
    full_name: str
    email: EmailStr
    password: str = Field(min_length=8)
    permissions: List[str] = Field(default_factory=list)
    # Example permissions: ["dashboard", "subscriptions", "notifications", "reports"]
    department: Optional[str] = None
    staff_role_id: Optional[UUID] = None  # NEW in v11 — optional, can also assign later via PATCH .../role


class AdminUserUpdate(BaseModel):
    """Body for PATCH /api/v1/admin/users/{id}"""
    full_name: Optional[str] = None
    permissions: Optional[List[str]] = None
    department: Optional[str] = None
    is_active: Optional[bool] = None


# ─────────────────────────────────────────────────────────────────────────────
# KYC SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class KycSubmit(BaseModel):
    """
    Body for POST /api/v1/kyc/submit
    Sent by the frontend KYC.jsx form on submission.
    Documents are uploaded separately via POST /api/v1/kyc/documents/upload
    """
    # Account type — the client's actual choice on the KYC form. Was
    # previously not collected at all here, meaning every user's
    # account_type stayed stuck at whatever it was set to at signup
    # (never Corporate/Joint/Minor, no matter what was chosen on this
    # form) — see submit_kyc() in kyc.py for the other half of the fix.
    account_type: Optional[Literal["individual", "joint", "minor", "corporate"]] = None

    # Personal
    date_of_birth: Optional[str] = None
    nationality: Optional[str] = None
    state: Optional[str] = None
    lga: Optional[str] = None
    address: Optional[str] = None
    occupation: Optional[str] = None
    employer: Optional[str] = None
    annual_income: Optional[str] = None
    investment_experience: Optional[str] = None
    risk_profile: Optional[str] = None
    pep_status: bool = False

    # Corporate
    company_name: Optional[str] = None
    rc_number: Optional[str] = None
    company_address: Optional[str] = None
    signatories: Optional[List[dict]] = None

    # Extra data — all additional form fields (employment, next of kin,
    # bank details, investment preferences, minor/joint/corporate extras)
    # Stored as JSON blob, pushed to D365 when integration is live.
    extra_data: Optional[dict] = None


class KycResponse(BaseModel):
    """KYC submission details returned to the frontend"""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    status: str
    denial_reason: Optional[str]
    d365_synced: bool
    d365_synced_at: Optional[datetime]
    submitted_at: datetime
    reviewed_at: Optional[datetime]


class KycAdminOverride(BaseModel):
    """
    Body for PATCH /api/v1/admin/kyc/{id}/override
    Super admin manually overrides KYC status.
    """
    status: str   # approved | denied
    reason: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class ProductResponse(BaseModel):
    """Investment product returned to frontend — replaces mockData.js products"""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    category: str
    product_type: str
    currency: str
    discretionary: str
    min_amount: float
    min_amount_display: str
    roi: str
    duration: str
    risk: str
    target_investors: str
    description: Optional[str]
    features: Optional[List[str]]
    is_active: bool


class ProductCreate(BaseModel):
    """Body for POST /api/v1/admin/products"""
    name: str
    category: str
    product_type: str
    currency: str = "NGN"
    discretionary: str = "Non-Discretionary"
    min_amount: float = 0
    min_amount_display: str
    roi: str
    duration: str
    risk: str
    target_investors: str
    description: Optional[str] = None
    features: Optional[List[str]] = None
    payment_account_id: Optional[UUID] = None   # Links to custodian bank account


class ProductUpdate(BaseModel):
    """Body for PATCH /api/v1/admin/products/{id}"""
    name: Optional[str] = None
    category: Optional[str] = None
    product_type: Optional[str] = None
    currency: Optional[str] = None
    discretionary: Optional[str] = None
    min_amount: Optional[float] = None
    min_amount_display: Optional[str] = None
    roi: Optional[str] = None
    duration: Optional[str] = None
    risk: Optional[str] = None
    target_investors: Optional[str] = None
    description: Optional[str] = None
    features: Optional[List[str]] = None
    is_active: Optional[bool] = None
    payment_account_id: Optional[UUID] = None   # Links to custodian bank account


# ─────────────────────────────────────────────────────────────────────────────
# SUBSCRIPTION SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class SubscriptionCreate(BaseModel):
    """Body for POST /api/v1/subscriptions"""
    product_id: UUID
    amount: float
    currency: str = "NGN"


class SubscriptionResponse(BaseModel):
    """Subscription details returned to frontend"""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    product_id: UUID
    amount: float
    currency: str
    reference: str
    status: str
    receipt_url: Optional[str]
    activated_at: Optional[datetime]
    maturity_date: Optional[datetime]
    submitted_at: datetime


class SubscriptionAdminAction(BaseModel):
    """
    Body for PATCH /api/v1/admin/subscriptions/{id}/action
    Admin activates or denies a pending subscription.
    """
    action: str       # activate | deny
    reason: Optional[str] = None
    maturity_date: Optional[datetime] = None  # Required when activating


# ─────────────────────────────────────────────────────────────────────────────
# REDEMPTION SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class RedemptionCreate(BaseModel):
    """Body for POST /api/v1/redemptions — client requests redemption.

    Two shapes, depending on what's being redeemed:
      - Fixed income / subscription-level: subscription_id + amount.
        (unchanged from before)
      - Equity holding: subscription_id + holding_id + units, no amount —
        the client can't know the sale price in advance, so there's nothing
        to enter it against. The real price is set by admin at approval.
    """
    subscription_id: UUID
    amount: Optional[float] = None
    holding_id: Optional[UUID] = None
    units: Optional[float] = Field(default=None, gt=0)
    note: Optional[str] = None


class RedemptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    subscription_id: UUID
    holding_id: Optional[UUID] = None
    amount: float
    currency: str
    penalty: float
    net_amount: float
    reference: str
    is_premature: bool
    units_sold: Optional[float] = None
    sale_price: Optional[float] = None
    cost_price_at_sale: Optional[float] = None
    realized_gain: Optional[float] = None
    status: str
    requested_at: datetime
    processed_at: Optional[datetime]


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATION SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    message: str
    is_read: bool
    notification_type: str
    created_at: datetime


class AdminSendNotification(BaseModel):
    """Body for POST /api/v1/admin/notifications/send"""
    title: str
    message: str
    # Who to send to: 'all' | 'approved' | 'pending' | specific user UUID
    recipient: str = "all"


# ─────────────────────────────────────────────────────────────────────────────
# ANNOUNCEMENT SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class AnnouncementCreate(BaseModel):
    """Body for POST /api/v1/admin/announcements"""
    title: str
    body: str
    audience: str = "all"


class AnnouncementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    body: str
    audience: str
    is_active: bool
    published_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# CERTIFICATE SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class CertificateCreate(BaseModel):
    """Body for POST /api/v1/admin/certificates"""
    user_id: UUID
    subscription_id: Optional[UUID] = None
    product_name: str
    amount: str
    roi: Optional[str] = None
    issue_date: str
    maturity_date: Optional[str] = None
    account_type: str = "Individual"


class CertificateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reference: str
    product_name: str
    amount: str
    roi: Optional[str]
    issue_date: str
    maturity_date: Optional[str]
    status: str
    cert_url: Optional[str]
    issued_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM / SETTINGS SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class SystemAlertResponse(BaseModel):
    """
    Response for GET /api/v1/system/alert
    Polled by frontend SystemAlertBanner on page load.
    Public endpoint — no auth required.
    """
    active: bool
    message: Optional[str]
    severity: str    # info | warning | critical
    updated_at: Optional[datetime]


class SystemAlertUpdate(BaseModel):
    """Body for PATCH /api/v1/admin/settings/alert — super admin only"""
    active: bool
    message: Optional[str] = None
    severity: str = "info"


class SystemSettingsUpdate(BaseModel):
    """Body for PATCH /api/v1/admin/settings/company"""
    company_name: Optional[str] = None
    short_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    website: Optional[str] = None
    # Controls whether clients see the per-holding breakdown of their private
    # portfolio (stock names, units, allocation donut). Total value and chart
    # stay visible either way.
    show_portfolio_breakdown: Optional[bool] = None


class FeeConfigUpdate(BaseModel):
    """Body for PATCH /api/v1/admin/settings/fees"""
    premature_penalty_pct: Optional[float] = None
    management_fee_pct: Optional[float] = None
    performance_fee_pct: Optional[float] = None
    liquidation_notice_days: Optional[int] = None
    min_investment_ngn: Optional[float] = None
    min_tenure_days: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────
# D365 WEBHOOK SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class D365WebhookPayload(BaseModel):
    """
    Shape of the webhook payload fired by Microsoft Dynamics 365.

    ── D365 Integration Point ──────────────────────────────────────────────────
    Configure D365 to POST to: POST /api/v1/webhooks/d365
    D365 → Settings → Webhooks → New Webhook
    URL: https://yourbackend.azurewebsites.net/api/v1/webhooks/d365
    Authentication: HMAC / Shared Secret (set D365_WEBHOOK_SECRET in .env)

    D365 sends these event types:
      KYC_APPROVED     — compliance team approved a KYC submission
      KYC_DENIED       — compliance team denied a KYC submission
      SUB_APPROVED     — operations team activated a subscription
      SUB_DENIED       — operations team denied a subscription
      SUB_MATURED      — investment tenure completed

    IMPORTANT: The event names below must match exactly what D365 sends.
    Coordinate with your D365 developer/partner to confirm these.
    ────────────────────────────────────────────────────────────────────────────
    """
    event: str          # KYC_APPROVED | KYC_DENIED | SUB_APPROVED | SUB_DENIED | SUB_MATURED
    record_id: str      # D365 record GUID — maps to d365_record_id in our DB
    user_id: Optional[str] = None
    reason: Optional[str] = None
    timestamp: str
    additional_data: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# GENERIC RESPONSE SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class MessageResponse(BaseModel):
    """Generic success response"""
    message: str
    success: bool = True


class PaginatedResponse(BaseModel):
    """Wrapper for paginated list responses"""
    items: List[Any]
    total: int
    page: int
    per_page: int
    pages: int


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW ENGINE (NEW in v11)
# ─────────────────────────────────────────────────────────────────────────────

WORKFLOW_TRIGGERS = ["kyc_submitted", "subscription_created", "redemption_requested", "client_account_created"]
WORKFLOW_ACTIONS = ["approve", "reject", "escalate", "request_info"]


class WorkflowStepCreate(BaseModel):
    step_order: int
    name: str = Field(min_length=2, max_length=200)
    description: Optional[str] = None
    required_permission: str  # e.g. "can_approve_kyc"
    available_actions: str = "approve,reject"  # comma-separated
    sla_hours: int = Field(default=24, ge=1)
    notify_ceo: bool = False


class WorkflowStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    workflow_id: UUID
    step_order: int
    name: str
    description: Optional[str]
    required_permission: str
    available_actions: str
    sla_hours: int
    notify_ceo: bool


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    trigger: str  # must be one of WORKFLOW_TRIGGERS
    description: Optional[str] = None
    steps: List[WorkflowStepCreate] = Field(default_factory=list)


class WorkflowUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class WorkflowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    trigger: str
    description: Optional[str]
    is_active: bool
    steps: List[WorkflowStepResponse] = []
    created_at: datetime


class WorkflowInstanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    workflow_id: UUID
    record_type: str
    record_id: UUID
    client_id: Optional[UUID]
    client_name: Optional[str]
    current_step: int
    status: str
    step_history: Optional[List[Any]] = []
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
    # Populated at query time — not ORM fields
    workflow_name: Optional[str] = None
    current_step_name: Optional[str] = None
    current_step_actions: Optional[str] = None


class WorkflowStepAction(BaseModel):
    """Body for PATCH /admin/workflows/instances/{id}/action"""
    action: str  # approve | reject | escalate | request_info
    note: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE PORTFOLIO — HOLDINGS, PRICES, VALUATIONS (NEW)
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioHoldingCreate(BaseModel):
    holding_type: str  # equity | fixed_income
    instrument_name: str = Field(min_length=1, max_length=200)
    # Equity
    units: Optional[float] = Field(default=None, gt=0)
    cost_price: Optional[float] = Field(default=None, gt=0)
    # Fixed income
    principal: Optional[float] = Field(default=None, gt=0)
    roi_pct: Optional[float] = Field(default=None, gt=0)
    start_date: Optional[datetime] = None
    maturity_date: Optional[datetime] = None


class PortfolioHoldingRedeem(BaseModel):
    """Body for partially or fully redeeming one holding."""
    units_or_amount: float = Field(gt=0)  # units for equity, currency amount for fixed income
    sale_price: Optional[float] = Field(default=None, gt=0)  # REQUIRED for equity — price per unit the sale executed at, used to compute realized gain/loss. Not applicable to fixed income.
    note: Optional[str] = None


class PortfolioHoldingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    subscription_id: UUID
    holding_type: str
    instrument_name: str
    units: Optional[float]
    cost_price: Optional[float]
    principal: Optional[float]
    roi_pct: Optional[float]
    start_date: Optional[datetime]
    maturity_date: Optional[datetime]
    status: str
    created_at: datetime


class InstrumentPriceEntry(BaseModel):
    instrument_name: str = Field(min_length=1, max_length=200)
    price: float = Field(gt=0)


class InstrumentPriceBatchSubmit(BaseModel):
    """Investment Manager submits today's closing price for one or more stocks in one call."""
    price_date: Optional[datetime] = None  # defaults to now if omitted
    entries: list[InstrumentPriceEntry] = Field(min_length=1)


class RunValuationRequest(BaseModel):
    """Trigger revaluation of all affected portfolios using the latest prices."""
    valuation_date: Optional[datetime] = None  # defaults to now
    subscription_ids: Optional[list[UUID]] = None  # None = revalue every active portfolio


class PortfolioValuationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    subscription_id: UUID
    valuation_date: datetime
    equities_value: float
    fixed_income_value: float
    total_value: float
    breakdown: Optional[list] = []
    created_at: datetime


class PortfolioHoldingUpdate(BaseModel):
    """
    Body for PATCH /admin/portfolio/holdings/{id} — correct any field on an
    existing holding. All optional; only what's sent gets changed.
    """
    instrument_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    units: Optional[float] = Field(default=None, gt=0)
    cost_price: Optional[float] = Field(default=None, gt=0)
    principal: Optional[float] = Field(default=None, gt=0)
    roi_pct: Optional[float] = Field(default=None, gt=0)
    start_date: Optional[datetime] = None
    maturity_date: Optional[datetime] = None


class InstrumentRename(BaseModel):
    """
    Body for PATCH /admin/portfolio/instruments/rename. Renames an instrument
    across every holding and price record at once, since the name is the only
    thing linking those together.
    """
    old_name: str = Field(min_length=1, max_length=200)
    new_name: str = Field(min_length=1, max_length=200)


# ─────────────────────────────────────────────────────────────────────────────
# PASSWORD MANAGEMENT (NEW)
# ─────────────────────────────────────────────────────────────────────────────

class AdminResetPassword(BaseModel):
    """
    Body for a super admin resetting someone else's password (staff or client).
    Leave new_password blank to have a secure temp password generated. Either
    way the account is flagged so the user must change it at next login.
    """
    new_password: Optional[str] = Field(default=None, min_length=8, max_length=100)


class PasswordResetResponse(BaseModel):
    """
    Returned after an admin resets a password. temp_password is shown ONCE and
    is hashed immediately, so it cannot be retrieved again — it must be passed
    to the user now.
    """
    id: UUID
    full_name: str
    email: str
    temp_password: str
    message: str = "Share this temporary password now. It cannot be retrieved again, and must be changed at next login."


class AdminChangePassword(BaseModel):
    """Body for a staff member changing their own password."""
    current_password: str
    new_password: str = Field(min_length=8, max_length=100)


class ForgotPasswordRequest(_NormalisedEmailMixin):
    """Body for POST /auth/forgot-password"""
    email: EmailStr


class ResetPasswordWithToken(BaseModel):
    """Body for POST /auth/reset-password"""
    token: str
    new_password: str = Field(min_length=8, max_length=100)


class VerifyEmailRequest(BaseModel):
    """Body for POST /auth/verify-email"""
    token: str


class WorkflowStepUpdate(BaseModel):
    """
    Body for PATCH /admin/workflows/{id}/steps/{step_id}. All optional, so a
    caller can correct just one field. Previously a step could only be deleted
    and re-added, which lost its position in the sequence.
    """
    step_order: Optional[int] = Field(default=None, ge=1)
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    description: Optional[str] = None
    required_permission: Optional[str] = None
    available_actions: Optional[str] = None
    sla_hours: Optional[int] = Field(default=None, ge=1)
    notify_ceo: Optional[bool] = None


# ─────────────────────────────────────────────────────────────────────────────
# CERTIFICATE SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class CertificateCreate(BaseModel):
    """Body for POST /admin/certificates — admin issues a certificate."""
    user_id: UUID
    subscription_id: Optional[UUID] = None
    product_name: str
    amount: str                        # display string, e.g. "₦5,000,000"
    roi: Optional[str] = None
    issue_date: str
    maturity_date: Optional[str] = None
    account_type: str


class CertificateUpdate(BaseModel):
    """
    Body for PATCH /admin/certificates/{id} — edit a previously issued
    certificate. All optional, so a correction only needs to touch the
    field(s) that were wrong. Also used to revoke a certificate by setting
    status="revoked".
    """
    product_name: Optional[str] = None
    amount: Optional[str] = None
    roi: Optional[str] = None
    issue_date: Optional[str] = None
    maturity_date: Optional[str] = None
    account_type: Optional[str] = None
    status: Optional[str] = None       # issued | emailed | revoked


class CertificateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    subscription_id: Optional[UUID] = None
    reference: str
    product_name: str
    amount: str
    roi: Optional[str] = None
    issue_date: str
    maturity_date: Optional[str] = None
    account_type: str
    status: str
    emailed_at: Optional[datetime] = None
    issued_at: datetime
