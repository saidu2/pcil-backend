# ─────────────────────────────────────────────────────────────────────────────
# app/models/models.py
#
# All SQLAlchemy database models for Prime Capital Investment Portal.
# Each class maps to a PostgreSQL table.
# Alembic reads these models to auto-generate migrations.
#
# TABLE SUMMARY:
#   users              — all client accounts (Individual/Joint/Minor/Corporate)
#   admin_users        — admin panel accounts (super_admin / junior_admin)
#   kyc_submissions    — KYC form data + document references + D365 sync status
#   products           — investment products (managed from admin panel)
#   subscriptions      — client investment subscriptions
#   redemptions        — client redemption requests
#   notifications      — in-app notifications per client
#   announcements      — market updates visible on client dashboard
#   audit_log          — every admin action (who, what, when)
#   payment_accounts   — bank accounts shown to clients (managed from admin)
#   fee_config         — fee/penalty configuration (managed from admin)
#   system_settings    — company info + system alert banner
#   certificates       — issued investment certificates
# ─────────────────────────────────────────────────────────────────────────────

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    String, Text, Boolean, Integer, Float, DateTime,
    ForeignKey, Enum as SAEnum, JSON, BigInteger,
    Index, UniqueConstraint, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.session import Base


# ── Helper: UTC timestamp ─────────────────────────────────────────────────────
def utc_now():
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# USERS (Client Accounts)
# ─────────────────────────────────────────────────────────────────────────────
class User(Base):
    """
    Client accounts — everyone who registers on the portal.
    Covers all account types: Individual, Minor, Joint, Corporate.
    KYC data is stored in KycSubmission (separate table).
    """
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # ── Login credentials ─────────────────────────────────────────────────────
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    # ── Basic profile ─────────────────────────────────────────────────────────
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    account_type: Mapped[str] = mapped_column(
        SAEnum("individual", "joint", "minor", "corporate", name="account_type_enum"),
        default="individual"
    )

    # ── KYC status ────────────────────────────────────────────────────────────
    # Mirrors the KYC_STATUS constants from frontend constants.js
    # Flow: not_submitted → pending → approved | denied
    kyc_status: Mapped[str] = mapped_column(
        SAEnum("not_submitted", "pending", "approved", "denied", name="kyc_status_enum"),
        default="not_submitted"
    )
    kyc_denied_reason: Mapped[Optional[str]] = mapped_column(Text)

    # ── Account flags ─────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)  # Email verified
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── Profile ───────────────────────────────────────────────────────────────
    # Uploaded via POST /api/v1/auth/avatar. Stored through app.core.storage,
    # so it lands on local disk in development and cloud storage in production.
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)

    # ── Temp password (NEW in v11) ────────────────────────────────────────────
    # Set True when IT Admin creates a client account with a system-generated
    # or manually-set temp password. Login flow checks this flag and forces
    # a password change before the client can reach their dashboard.
    temp_password_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── MFA (NEW in v11) ──────────────────────────────────────────────────────
    # Optional for clients — IT Admin can enforce per client from
    # User Management. TOTP secret is encrypted at rest by the app layer
    # before being written here, never stored or returned in plaintext.
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(255))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    kyc_submission: Mapped[Optional["KycSubmission"]] = relationship(
        back_populates="user", uselist=False
    )
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")
    redemptions: Mapped[list["Redemption"]] = relationship(back_populates="user")
    notifications: Mapped[list["Notification"]] = relationship(back_populates="user")
    certificates: Mapped[list["Certificate"]] = relationship(back_populates="user")

    __table_args__ = (
        Index("ix_users_kyc_status",   "kyc_status"),
        Index("ix_users_account_type", "account_type"),
        Index("ix_users_is_active",    "is_active"),
    )

    def __repr__(self):
        return f"<User {self.email} [{self.kyc_status}]>"


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN USERS
# ─────────────────────────────────────────────────────────────────────────────
class AdminUser(Base):
    """
    Admin panel accounts — completely separate from client accounts.
    Two roles:
      super_admin — full access, only Saidu Safiyanu
      junior_admin — section-level access defined by permissions JSON
    """
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[str] = mapped_column(
        SAEnum("super_admin", "junior_admin", name="admin_role_enum"),
        default="junior_admin"
    )

    # ── Permissions (Junior Admin only) ───────────────────────────────────────
    # Stored as JSON array of section IDs the junior admin can access.
    # Example: ["dashboard", "subscriptions", "notifications"]
    # Super admin ignores this — has full access always.
    permissions: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    # ── Department (NEW in v11) ───────────────────────────────────────────────
    # e.g. IT, Compliance, Operations, Investment, Finance, Executive.
    # Free text for now — will be formalised once staff_roles ships (Phase 1
    # step 2), at which point department will usually mirror the linked role.
    department: Mapped[Optional[str]] = mapped_column(String(100))

    # ── Temp password (NEW) ───────────────────────────────────────────────────
    # Set True when a super admin creates the account or resets the password.
    # Login then returns must_change_password so the staff member is forced to
    # set their own before reaching the panel — the same flow clients had.
    temp_password_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── MFA (NEW in v11) ──────────────────────────────────────────────────────
    # Mandatory for all staff roles. TOTP secret is encrypted at rest by the
    # app layer before being written here, never stored or returned in
    # plaintext. IT Admin can reset/bypass per user from User Management.
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(255))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Staff role (NEW in v11) ───────────────────────────────────────────────
    # Granular permission role — separate from the legacy super_admin/junior_admin
    # split above, which stays untouched. Nullable so existing accounts keep
    # working exactly as before until deliberately assigned a role here.
    # Once assigned, application logic can treat staff_role permission flags
    # as the source of truth for new (v11) features like workflow approvals,
    # while `role`/`permissions` above continue to gate the older sections.
    staff_role_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff_roles.id", ondelete="SET NULL"), nullable=True
    )
    staff_role: Mapped[Optional["StaffRole"]] = relationship(back_populates="admin_users")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self):
        return f"<AdminUser {self.email} [{self.role}]>"


# ─────────────────────────────────────────────────────────────────────────────
# STAFF ROLES (NEW in v11)
# Granular, configurable permission roles for staff — IT Admin, Compliance
# Officer, Operations Officer, Investment Manager, Finance Officer,
# CEO/Director, etc. Distinct from the legacy super_admin/junior_admin split
# on AdminUser, which is left untouched for backward compatibility.
# ─────────────────────────────────────────────────────────────────────────────
class StaffRole(Base):
    """
    A configurable staff role with granular permission flags.
    IT Admin creates/edits these from Admin Panel → Roles & Permissions.
    AdminUser.staff_role_id points here (nullable — optional until assigned).
    """
    __tablename__ = "staff_roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # e.g. IT, Compliance, Operations, Investment, Finance, Executive
    department: Mapped[Optional[str]] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(String(500))

    # ── Permission flags ──────────────────────────────────────────────────────
    can_manage_staff_users: Mapped[bool] = mapped_column(Boolean, default=False)
    can_configure_roles: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_clients: Mapped[bool] = mapped_column(Boolean, default=False)
    can_approve_kyc: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_subscriptions: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_redemptions: Mapped[bool] = mapped_column(Boolean, default=False)
    can_enter_valuations: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_nav: Mapped[bool] = mapped_column(Boolean, default=False)
    can_configure_workflows: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_products: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_fees: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_certificates: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_maturity: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_payments: Mapped[bool] = mapped_column(Boolean, default=False)
    can_view_reports: Mapped[bool] = mapped_column(Boolean, default=False)
    can_view_audit_log: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage_system_settings: Mapped[bool] = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    admin_users: Mapped[list["AdminUser"]] = relationship(back_populates="staff_role")

    def __repr__(self):
        return f"<StaffRole {self.name}>"


# ─────────────────────────────────────────────────────────────────────────────
# KYC SUBMISSIONS
# ─────────────────────────────────────────────────────────────────────────────
class KycSubmission(Base):
    """
    KYC form data submitted by clients.
    Documents (IDs, CAC certs, etc.) are stored in Azure Blob Storage.
    Only the blob URL is stored here.

    ── D365 Integration Point ──────────────────────────────────────────────────
    When a KYC is submitted:
      1. This record is created with status='pending'
      2. Celery task (tasks/d365_tasks.py) pushes it to D365 via REST API
      3. D365 compliance team reviews it
      4. D365 fires a webhook to POST /api/v1/webhooks/d365/kyc-decision
      5. Webhook handler updates this record's status + user.kyc_status
    ────────────────────────────────────────────────────────────────────────────
    """
    __tablename__ = "kyc_submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        SAEnum("pending", "approved", "denied", name="kyc_submission_status_enum"),
        default="pending"
    )
    denial_reason: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(255))  # Admin name or "D365"

    # ── D365 Sync ─────────────────────────────────────────────────────────────
    # d365_record_id: The GUID of this KYC record in Dynamics 365.
    # Set when the record is successfully pushed to D365.
    # Used to link back to D365 for status updates.
    d365_record_id: Mapped[Optional[str]] = mapped_column(String(100))
    d365_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    d365_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── Personal Data (Individual / Joint / Minor) ────────────────────────────
    # Full name stored on user model; extra personal data stored here
    date_of_birth: Mapped[Optional[str]] = mapped_column(String(20))
    nationality: Mapped[Optional[str]] = mapped_column(String(100))
    state: Mapped[Optional[str]] = mapped_column(String(100))
    lga: Mapped[Optional[str]] = mapped_column(String(100))
    address: Mapped[Optional[str]] = mapped_column(Text)
    occupation: Mapped[Optional[str]] = mapped_column(String(255))
    employer: Mapped[Optional[str]] = mapped_column(String(255))
    annual_income: Mapped[Optional[str]] = mapped_column(String(50))
    investment_experience: Mapped[Optional[str]] = mapped_column(String(50))
    risk_profile: Mapped[Optional[str]] = mapped_column(String(50))  # From quiz result
    pep_status: Mapped[bool] = mapped_column(Boolean, default=False)  # Politically Exposed Person

    # ── Corporate Data ────────────────────────────────────────────────────────
    company_name: Mapped[Optional[str]] = mapped_column(String(255))
    rc_number: Mapped[Optional[str]] = mapped_column(String(100))
    company_address: Mapped[Optional[str]] = mapped_column(Text)
    # Signatories stored as JSON array: [{name, title, email, phone}, ...]
    signatories: Mapped[Optional[list]] = mapped_column(JSON)

    # ── Document URLs (Azure Blob Storage) ───────────────────────────────────
    # These are Azure Blob Storage URLs for uploaded documents.
    # Actual files are in the 'kyc-documents' container.
    # TODO: When backend is wired, upload via POST /api/v1/kyc/documents/upload
    id_document_url: Mapped[Optional[str]] = mapped_column(Text)
    utility_bill_url: Mapped[Optional[str]] = mapped_column(Text)
    passport_photo_url: Mapped[Optional[str]] = mapped_column(Text)
    cac_certificate_url: Mapped[Optional[str]] = mapped_column(Text)        # Corporate
    board_resolution_url: Mapped[Optional[str]] = mapped_column(Text)       # Corporate
    memorandum_url: Mapped[Optional[str]] = mapped_column(Text)             # Corporate
    scuml_certificate_url: Mapped[Optional[str]] = mapped_column(Text)      # Corporate — NEW, was collected on the form but never had anywhere to be saved
    tin_certificate_url: Mapped[Optional[str]] = mapped_column(Text)        # Corporate — NEW, same as above
    board_resolution_director_signature_url: Mapped[Optional[str]] = mapped_column(Text)   # Corporate — NEW
    board_resolution_secretary_signature_url: Mapped[Optional[str]] = mapped_column(Text)  # Corporate — NEW
    minor_passport_photo_url: Mapped[Optional[str]] = mapped_column(Text)   # Minor — NEW, the minor's own photo
    minor_birth_certificate_url: Mapped[Optional[str]] = mapped_column(Text)  # Minor — NEW
    joint_passport_photo_url: Mapped[Optional[str]] = mapped_column(Text)   # Joint — NEW, the partner's own photo
    joint_id_document_url: Mapped[Optional[str]] = mapped_column(Text)      # Joint — NEW

    # ── Extra Data (JSON) ─────────────────────────────────────────────────────
    # Stores all additional KYC form data not in dedicated columns:
    # employment, next of kin, investment preferences, bank details, etc.
    # When D365 is live, this entire blob is pushed to Dataverse for compliance.
    # Declared as JSONB to match what the database actually has (created that
    # way back in the original extra_data migration). It was previously
    # declared as plain JSON here, and that mismatch made alembic propose a
    # spurious JSONB->JSON conversion on every single autogenerate.
    #
    # JSONB is the right choice regardless: it stores parsed binary rather
    # than raw text, so it can be indexed and queried inside efficiently,
    # which matters if we ever need to search within KYC data.
    extra_data: Mapped[Optional[dict]] = mapped_column(JSONB)

    # ── Timestamps ────────────────────────────────────────────────────────────
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(back_populates="kyc_submission")


# ─────────────────────────────────────────────────────────────────────────────
# INVESTMENT PRODUCTS
# ─────────────────────────────────────────────────────────────────────────────
class Product(Base):
    """
    Investment products managed entirely from the admin panel.
    No code changes needed to add/edit/deactivate products.
    These replace the hardcoded products in frontend mockData.js.
    """
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100))   # Fixed Income, Equity, Ethical, FX/Dollar
    product_type: Mapped[str] = mapped_column(String(50)) # Conventional | Sharia
    currency: Mapped[str] = mapped_column(String(10))     # NGN | USD
    discretionary: Mapped[str] = mapped_column(String(50)) # Discretionary | Non-Discretionary

    min_amount: Mapped[float] = mapped_column(Float, default=0)
    min_amount_display: Mapped[str] = mapped_column(String(50))  # e.g. "₦5,000,000"
    roi: Mapped[str] = mapped_column(String(100))                # e.g. "~14-18% p.a."
    duration: Mapped[str] = mapped_column(String(100))
    risk: Mapped[str] = mapped_column(String(50))                # Conservative | Balanced | Aggressive
    target_investors: Mapped[str] = mapped_column(String(255))   # e.g. "Retail | HNI | Institutional"
    description: Mapped[Optional[str]] = mapped_column(Text)
    features: Mapped[Optional[list]] = mapped_column(JSON)       # List of feature strings

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Linked custodian bank account ─────────────────────────────────────────
    # Set by admin when creating/editing the product.
    # When a client subscribes, the frontend reads this to show the correct
    # bank account to transfer funds to.
    payment_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_accounts.id", ondelete="SET NULL"),
        nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="product")
    payment_account: Mapped[Optional["PaymentAccount"]] = relationship("PaymentAccount", foreign_keys=[payment_account_id])


# ─────────────────────────────────────────────────────────────────────────────
# SUBSCRIPTIONS
# ─────────────────────────────────────────────────────────────────────────────
class Subscription(Base):
    """
    Client investment subscriptions.

    Status flow:
      pending_payment → client sees bank details, needs to transfer & upload receipt
      pending_review  → receipt uploaded, waiting for admin/D365 to activate
      active          → investment is live
      matured         → tenure completed
      redeemed        → client redeemed (full)
      denied          → rejected by admin

    ── D365 Integration Point ──────────────────────────────────────────────────
    When admin activates a subscription:
      1. status changes to 'active' here
      2. Celery task pushes record to D365 subscription entity
      3. D365 manages the investment lifecycle
      4. On maturity: D365 fires webhook to POST /api/v1/webhooks/d365/subscription
    ────────────────────────────────────────────────────────────────────────────
    """
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )

    # ── Financial details ─────────────────────────────────────────────────────
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    reference: Mapped[str] = mapped_column(String(100), unique=True, index=True)

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        SAEnum(
            "pending_payment", "pending_review", "active",
            "matured", "redeemed", "denied",
            name="subscription_status_enum"
        ),
        default="pending_payment"
    )
    denial_reason: Mapped[Optional[str]] = mapped_column(Text)

    # ── Payment receipt ───────────────────────────────────────────────────────
    # URL of uploaded receipt in Azure Blob Storage (payment-receipts container)
    receipt_url: Mapped[Optional[str]] = mapped_column(Text)
    receipt_data: Mapped[Optional[str]] = mapped_column(Text)       # base64 encoded file
    receipt_filename: Mapped[Optional[str]] = mapped_column(String(255))
    receipt_mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    receipt_uploaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── D365 Sync ─────────────────────────────────────────────────────────────
    d365_record_id: Mapped[Optional[str]] = mapped_column(String(100))
    d365_synced: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Timeline ──────────────────────────────────────────────────────────────
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    maturity_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    matured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(back_populates="subscriptions")
    product: Mapped["Product"] = relationship(back_populates="subscriptions")
    redemptions: Mapped[list["Redemption"]] = relationship(back_populates="subscription")

    __table_args__ = (
        Index("ix_subs_status",       "status"),
        Index("ix_subs_product_id",   "product_id"),
        Index("ix_subs_user_status",  "user_id", "status"),   # compound — speeds up "my active subs"
        Index("ix_subs_submitted_at", "submitted_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# REDEMPTIONS
# ─────────────────────────────────────────────────────────────────────────────
class Redemption(Base):
    """
    Client redemption (withdrawal) requests.
    Premature redemption attracts 20% penalty on accrued profit (configurable).
    """
    __tablename__ = "redemptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )

    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    penalty: Mapped[float] = mapped_column(Float, default=0.0)  # 20% of profit if premature
    net_amount: Mapped[float] = mapped_column(Float, default=0.0)  # amount - penalty
    reference: Mapped[str] = mapped_column(String(100), unique=True)
    is_premature: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(
        SAEnum("pending", "processing", "completed", "rejected", name="redemption_status_enum"),
        default="pending"
    )

    note: Mapped[Optional[str]] = mapped_column(Text)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    processed_by: Mapped[Optional[str]] = mapped_column(String(255))

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="redemptions")
    subscription: Mapped["Subscription"] = relationship(back_populates="redemptions")

    __table_args__ = (
        Index("ix_redemptions_status",     "status"),
        Index("ix_redemptions_user_status","user_id", "status"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# INVESTMENT CERTIFICATES
# ─────────────────────────────────────────────────────────────────────────────
class Certificate(Base):
    """
    Investment certificates issued by admin.
    The generated PNG is stored in Azure Blob Storage (investment-certs container).
    cert_url is the Azure Blob URL — used for email attachments.
    """
    __tablename__ = "certificates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    subscription_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True
    )

    reference: Mapped[str] = mapped_column(String(100), unique=True)
    product_name: Mapped[str] = mapped_column(String(255))
    amount: Mapped[str] = mapped_column(String(100))        # Display string e.g. "₦5,000,000"
    roi: Mapped[Optional[str]] = mapped_column(String(100))
    issue_date: Mapped[str] = mapped_column(String(20))
    maturity_date: Mapped[Optional[str]] = mapped_column(String(20))
    account_type: Mapped[str] = mapped_column(String(50))

    # Azure Blob URL for the generated PNG certificate
    # TODO: Set this after generating and uploading the certificate PNG
    cert_url: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        SAEnum("issued", "emailed", "revoked", name="cert_status_enum"),
        default="issued"
    )
    emailed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="certificates")


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATIONS (In-app, per client)
# ─────────────────────────────────────────────────────────────────────────────
class Notification(Base):
    """
    In-app notifications for individual clients.
    Shown in the client dashboard notification bell.
    Sent by admin from the Notifications section.
    Also auto-created on KYC approval, subscription activation, etc.
    """
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_type: Mapped[str] = mapped_column(
        String(50), default="general"
    )  # general | kyc | subscription | redemption | certificate | announcement

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="notifications")

    __table_args__ = (
        # Compound index — speeds up "get my unread notifications" on every page load
        Index("ix_notif_user_read", "user_id", "is_read"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ANNOUNCEMENTS (Market updates, visible to all clients)
# ─────────────────────────────────────────────────────────────────────────────
class Announcement(Base):
    """
    Market updates and announcements shown on client dashboard.
    Managed from admin panel → Announcements section.
    Admin can toggle live/hidden without code changes.
    """
    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(String(50), default="all")  # all | approved
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LOG (Every admin action)
# ─────────────────────────────────────────────────────────────────────────────
class AuditLog(Base):
    """
    Immutable record of every admin action.
    Written on every create/update/delete by any admin.
    Cannot be edited or deleted — append only.
    Critical for SEC compliance and internal governance.
    """
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. "KYC Approved"
    target: Mapped[Optional[str]] = mapped_column(String(255))         # e.g. "Aminu Musa"
    target_id: Mapped[Optional[str]] = mapped_column(String(100))      # UUID of affected record
    action_type: Mapped[str] = mapped_column(String(50))               # kyc | product | subscription | admin | settings
    performed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    performed_by_name: Mapped[str] = mapped_column(String(255), default="System")
    details: Mapped[Optional[dict]] = mapped_column(JSON)  # Any extra context

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW ENGINE (NEW in v11)
# IT Admin configures workflows → triggers fire instances → staff act via
# My Tasks. Three tables: workflows (definitions), workflow_steps (ordered
# steps within each workflow), workflow_instances (running instances tied
# to a specific KYC/subscription/redemption record).
# ─────────────────────────────────────────────────────────────────────────────

class Workflow(Base):
    """
    A named, configurable approval workflow.
    Trigger determines which event fires it automatically.
    IT Admin creates/edits from Admin Panel → Workflow Configuration.
    """
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # kyc_submitted | subscription_created | redemption_requested | client_account_created
    trigger: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="workflow", order_by="WorkflowStep.step_order", cascade="all, delete-orphan"
    )
    instances: Mapped[list["WorkflowInstance"]] = relationship(back_populates="workflow")

    def __repr__(self):
        return f"<Workflow {self.name} [{self.trigger}]>"


class WorkflowStep(Base):
    """
    A single ordered step within a workflow.
    Defines who handles it (via staff role flag), what actions are available,
    and the SLA (hours to act before it's considered overdue).
    """
    __tablename__ = "workflow_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    step_order: Mapped[int] = mapped_column(nullable=False)  # 1, 2, 3...
    name: Mapped[str] = mapped_column(String(200), nullable=False)  # e.g. "Operations Review"
    description: Mapped[Optional[str]] = mapped_column(String(500))
    # The staff_role permission flag required to handle this step
    # e.g. "can_approve_kyc", "can_manage_subscriptions", "can_manage_redemptions"
    required_permission: Mapped[str] = mapped_column(String(100), nullable=False)
    # Comma-separated available actions: approve,reject,escalate,request_info
    available_actions: Mapped[str] = mapped_column(String(200), default="approve,reject")
    sla_hours: Mapped[int] = mapped_column(default=24)  # Hours before overdue
    notify_ceo: Mapped[bool] = mapped_column(Boolean, default=False)

    workflow: Mapped["Workflow"] = relationship(back_populates="steps")

    def __repr__(self):
        return f"<WorkflowStep {self.step_order}: {self.name}>"


class WorkflowInstance(Base):
    """
    A running instance of a workflow tied to a specific record.
    Created automatically when a trigger fires (KYC submitted, etc.).
    Tracks current step, overall status, and per-step action history.
    """
    __tablename__ = "workflow_instances"
    __table_args__ = (
        # Enforces at MOST ONE active (pending/in_progress) instance per
        # record at the database level — closes a real race condition where
        # two near-simultaneous requests (e.g. a double-click, or a dev-mode
        # double effect fire) could both pass the application-level "does
        # one already exist?" check before either had committed, each
        # inserting its own row. The application-level check in
        # _fire_workflow() is still useful as a fast-path (avoids hitting
        # this constraint in the common case), but this index is what
        # actually guarantees it under concurrency. A duplicate insert now
        # raises IntegrityError instead of silently succeeding twice.
        Index(
            "ix_workflow_instances_one_active_per_record",
            "record_type", "record_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'in_progress')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    # The record this instance is tracking
    # e.g. kyc_submission.id, subscription.id, redemption.id
    record_type: Mapped[str] = mapped_column(String(50), nullable=False)  # kyc | subscription | redemption
    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The client this workflow is for — for "My Tasks" filtering
    client_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    client_name: Mapped[Optional[str]] = mapped_column(String(255))  # denormalized for fast task list queries

    current_step: Mapped[int] = mapped_column(default=1)
    # pending | in_progress | approved | rejected | escalated | completed
    status: Mapped[str] = mapped_column(String(50), default="pending")

    # JSON array of step history entries — each entry records who did what:
    # [{"step": 1, "action": "approve", "by": "Aminu Musa", "at": "...", "note": "..."}]
    step_history: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    workflow: Mapped["Workflow"] = relationship(back_populates="instances")

    def __repr__(self):
        return f"<WorkflowInstance {self.record_type}/{self.record_id} step={self.current_step} [{self.status}]>"


# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE PORTFOLIO — HOLDINGS, PRICES, VALUATIONS
#
# A "Portfolio" product's Subscription can hold a basket of individual
# positions: equities (specific stocks) and/or fixed income placements
# (T-bills, Sukuk, etc). Each holding is valued by its own logic:
#   - Equity: units * latest InstrumentPrice for that stock
#   - Fixed income: principal + accrued interest toward maturity
#
# InstrumentPrice is deliberately separate from PortfolioHolding, keyed by
# instrument name and date, not per client — entering one stock's closing
# price revalues every holding of that stock across every client's
# portfolio in one action ("Run Valuation"), instead of re-entering the
# same price once per client.
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioHolding(Base):
    """One position inside a client's private portfolio (Subscription)."""
    __tablename__ = "portfolio_holdings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    holding_type: Mapped[str] = mapped_column(String(20), nullable=False)  # equity | fixed_income
    instrument_name: Mapped[str] = mapped_column(String(200), nullable=False)  # e.g. "MTN Nigeria", "FGN Sukuk Jul 2027"

    # ── Equity fields ──────────────────────────────────────────────────────
    units: Mapped[Optional[float]] = mapped_column(Float)
    cost_price: Mapped[Optional[float]] = mapped_column(Float)  # price per unit at purchase

    # ── Fixed income fields ────────────────────────────────────────────────
    principal: Mapped[Optional[float]] = mapped_column(Float)
    roi_pct: Mapped[Optional[float]] = mapped_column(Float)
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    maturity_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # active: full holding still held. partially_redeemed: some units/principal
    # sold, holding stays open with the reduced amount. redeemed: fully sold,
    # excluded from future valuations but kept for history.
    status: Mapped[str] = mapped_column(String(20), default="active")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    subscription: Mapped["Subscription"] = relationship()

    def __repr__(self):
        return f"<PortfolioHolding {self.instrument_name} [{self.holding_type}] sub={self.subscription_id}>"


class InstrumentPrice(Base):
    """
    A stock's closing price on a given date, entered once by the Investment
    Manager — shared across every client holding that instrument. History
    is never overwritten (each date is its own row), so this also serves
    as the price chart data for that instrument.
    """
    __tablename__ = "instrument_prices"
    __table_args__ = (
        UniqueConstraint("instrument_name", "price_date", name="uq_instrument_price_per_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instrument_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    price_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | excel | ngx_api (future)
    entered_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<InstrumentPrice {self.instrument_name} {self.price} on {self.price_date}>"


class PortfolioValuation(Base):
    """
    A snapshot of a client's total portfolio value at a point in time,
    saved every time "Run Valuation" is executed. This is what powers the
    client's historical value-over-time chart.
    """
    __tablename__ = "portfolio_valuations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    valuation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    equities_value: Mapped[float] = mapped_column(Float, default=0)
    fixed_income_value: Mapped[float] = mapped_column(Float, default=0)
    total_value: Mapped[float] = mapped_column(Float, nullable=False)
    # Per-holding breakdown at the moment of this snapshot — [{holding_id, name, type, units/principal, price_or_accrual, value}]
    breakdown: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    subscription: Mapped["Subscription"] = relationship()

    def __repr__(self):
        return f"<PortfolioValuation sub={self.subscription_id} total={self.total_value} on {self.valuation_date}>"


class PasswordResetToken(Base):
    """
    Single-use, time-limited token for client self-service flows: password
    reset ("Forgot password?") and email address verification.

    The token itself is stored HASHED, exactly like a password. If this table
    ever leaked, the raw tokens still couldn't be used to take over accounts.
    """
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # password_reset | email_verification
    purpose: Mapped[str] = mapped_column(String(30), default="password_reset")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<PasswordResetToken user={self.user_id} purpose={self.purpose} used={bool(self.used_at)}>"


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT ACCOUNTS (Bank accounts shown to clients)
# ─────────────────────────────────────────────────────────────────────────────
class PaymentAccount(Base):
    """
    Bank accounts displayed to clients during subscription.
    Managed from admin panel → Payment Accounts section.
    Currently: Zenith Bank NGN. Add USD when ready.
    """
    __tablename__ = "payment_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bank: Mapped[str] = mapped_column(String(255), nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_number: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="NGN")
    label: Mapped[str] = mapped_column(String(100))      # e.g. "Naira (₦) Investments"
    instruction: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ─────────────────────────────────────────────────────────────────────────────
# FEE CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
class FeeConfig(Base):
    """
    Configurable fee and penalty settings.
    Managed from admin panel → Fees & Penalties section.
    Only ONE record exists (id=1) — this is a singleton config table.
    """
    __tablename__ = "fee_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    premature_penalty_pct: Mapped[float] = mapped_column(Float, default=20.0)   # 20% on profit
    management_fee_pct: Mapped[float] = mapped_column(Float, default=1.5)
    performance_fee_pct: Mapped[float] = mapped_column(Float, default=10.0)
    liquidation_notice_days: Mapped[int] = mapped_column(Integer, default=5)
    min_investment_ngn: Mapped[float] = mapped_column(Float, default=500_000)
    min_tenure_days: Mapped[int] = mapped_column(Integer, default=90)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM SETTINGS (Company info + System Alert Banner)
# ─────────────────────────────────────────────────────────────────────────────
class SystemSettings(Base):
    """
    Company-wide settings managed from admin panel → System Settings.
    Also stores the system alert banner state.
    Only ONE record exists (id=1) — singleton config table.

    The system alert banner is read by the frontend on every page load.
    When active=True, the banner displays across ALL client portal pages
    for ALL visitors — logged in, logged out, and mobile.
    """
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # ── Company Info ──────────────────────────────────────────────────────────
    company_name: Mapped[str] = mapped_column(String(255), default="Prime Capital & Investment Ltd")
    short_name: Mapped[str] = mapped_column(String(100), default="Prime Capital")
    email: Mapped[str] = mapped_column(String(255), default="info@primecapital.ng")
    phone: Mapped[str] = mapped_column(String(20), default="08100276250")
    whatsapp: Mapped[str] = mapped_column(String(30), default="2348100276250")
    address: Mapped[str] = mapped_column(Text, default="No. 3 Sankuru Close, Off Rima Street, Maitama, Abuja")
    city: Mapped[str] = mapped_column(String(100), default="Abuja, Nigeria")
    website: Mapped[str] = mapped_column(String(255), default="www.primecapital.ng")
    regulator: Mapped[str] = mapped_column(String(255), default="Securities & Exchange Commission (SEC) Nigeria")

    # ── System Alert Banner ───────────────────────────────────────────────────
    # This powers the SystemAlertBanner component in the React frontend.
    # Frontend polls GET /api/v1/system/alert to check if this is active.
    alert_active: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_message: Mapped[Optional[str]] = mapped_column(Text)
    alert_severity: Mapped[str] = mapped_column(
        SAEnum("info", "warning", "critical", name="alert_severity_enum"),
        default="info"
    )
    alert_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ── Client Dashboard Visibility ───────────────────────────────────────────
    # Controls whether clients can see the per-holding breakdown of their
    # private portfolio (individual stock names, units, allocation donut).
    # When False, they still see their total portfolio value and its chart,
    # just not the position-by-position detail behind it.
    show_portfolio_breakdown: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Signature Images (Future: for certificates) ───────────────────────────
    # TODO: When signature upload is built in admin Settings section,
    #       save the Azure Blob URLs here. certificateGenerator.js will
    #       then fetch and render these on generated certificates.
    md_signature_url: Mapped[Optional[str]] = mapped_column(Text)
    secretary_signature_url: Mapped[Optional[str]] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))

# ─────────────────────────────────────────────────────────────────────────────
# NAV RECORDS (Net Asset Value & Actual Returns)
# ─────────────────────────────────────────────────────────────────────────────
class NavRecord(Base):
    """
    Actual NAV (Net Asset Value) and return figures per product per period.

    The finance team enters these from the admin panel → NAV & Returns section.
    The client dashboard reads from this table to show real returns instead of
    estimated projections.

    ── D365 Integration Point ──────────────────────────────────────────────────
    When D365 is configured, the webhook handler calls upsert_nav_from_d365()
    in nav.py which writes to this same table with source='d365'.
    No schema change needed at that point — D365 just becomes the writer
    instead of the admin entering manually.
    ────────────────────────────────────────────────────────────────────────────
    """
    __tablename__ = "nav_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )

    period_label:   Mapped[str]      = mapped_column(String(50), nullable=False)
    period_start:   Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end:     Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    nav_per_unit:   Mapped[Optional[float]] = mapped_column(Float)
    return_pct:     Mapped[Optional[float]] = mapped_column(Float)
    cumulative_pct: Mapped[Optional[float]] = mapped_column(Float)
    total_aum:      Mapped[Optional[float]] = mapped_column(Float)

    notes:      Mapped[Optional[str]] = mapped_column(Text)
    source:     Mapped[str]           = mapped_column(String(20), default="manual")
    entered_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_nav_product_id", "product_id"),
        Index("ix_nav_period_end", "period_end"),
        UniqueConstraint("product_id", "period_label", name="uq_nav_product_period"),
    )

    def __repr__(self):
        return f"<NavRecord product={self.product_id} period={self.period_label} return={self.return_pct}%>"
