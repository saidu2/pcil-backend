# ─────────────────────────────────────────────────────────────────────────────
# app/db/seed.py
#
# Database seeding script — run ONCE after migrations to populate initial data.
#
# What this seeds:
#   1. Super admin account (Saidu Safiyanu)
#   2. All 9 investment products
#   3. Default fee configuration (singleton)
#   4. Default payment account (Zenith Bank NGN)
#   5. Default system settings (company info + alert banner off)
#
# HOW TO RUN:
#   Make sure venv is active and you're in pcil-backend/ folder, then:
#   python -m app.db.seed
#
# IMPORTANT: This script is idempotent — safe to run multiple times.
# It checks if data already exists before inserting, so it won't
# create duplicates.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.core.security import hash_password
from app.models.models import (
    AdminUser, Product, FeeConfig,
    PaymentAccount, SystemSettings,
    Workflow, WorkflowStep,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


async def seed_super_admin(db: AsyncSession):
    """
    Create the super admin account.
    Email and password come from .env (SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD).
    Only created if no super admin exists yet.
    """
    from app.core.config import settings

    existing = await db.execute(
        select(AdminUser).where(AdminUser.role == "super_admin")
    )
    if existing.scalar_one_or_none():
        logger.info("Super admin already exists — skipping.")
        return

    admin = AdminUser(
        email=settings.SUPER_ADMIN_EMAIL,
        hashed_password=hash_password(settings.SUPER_ADMIN_PASSWORD),
        full_name=settings.SUPER_ADMIN_NAME,
        role="super_admin",
        permissions=[],   # Super admin ignores permissions — has full access always
        is_active=True,
    )
    db.add(admin)
    logger.info(f"✓ Super admin created: {settings.SUPER_ADMIN_EMAIL}")


async def seed_products(db: AsyncSession):
    """
    Seed all 9 Prime Capital investment products.
    These replace the hardcoded products in frontend mockData.js.
    Only seeds if no products exist yet.
    """
    existing = await db.execute(select(Product))
    if existing.scalars().first():
        logger.info("Products already seeded — skipping.")
        return

    products = [
        # ── Ethical / Sharia Products ──────────────────────────────────────
        Product(
            name="Prime Al-Amanah (Al-Wakala)",
            category="Ethical/Sharia",
            product_type="Sharia",
            currency="NGN",
            discretionary="Non-Discretionary",
            min_amount=500_000_000,
            min_amount_display="₦500,000,000",
            roi="Profit-sharing (variable)",
            duration="12 months (renewable)",
            risk="Conservative",
            target_investors="HNI | Institutional",
            description=(
                "A Sharia-compliant Al-Wakala investment structure for high-net-worth "
                "individuals and institutional investors. Capital is deployed into "
                "pre-approved Sharia-compliant instruments."
            ),
            features=[
                "Sharia-compliant (Al-Wakala structure)",
                "Capital preservation focus",
                "Quarterly profit distribution",
                "Dedicated relationship manager",
            ],
            is_active=True,
        ),
        Product(
            name="Prime Al-Barakah (Non-Discretionary)",
            category="Ethical/Sharia",
            product_type="Sharia",
            currency="NGN",
            discretionary="Non-Discretionary",
            min_amount=50_000_000,
            min_amount_display="₦50,000,000",
            roi="~14-18% p.a.",
            duration="12 months",
            risk="Conservative",
            target_investors="HNI | Retail",
            description=(
                "Sharia-compliant non-discretionary investment. Client selects "
                "approved instruments from a pre-defined portfolio menu."
            ),
            features=[
                "Sharia-compliant",
                "Client controls instrument selection",
                "~14-18% target annual return",
                "Annual profit distribution",
            ],
            is_active=True,
        ),
        Product(
            name="Prime Al-Barakah (Discretionary)",
            category="Ethical/Sharia",
            product_type="Sharia",
            currency="NGN",
            discretionary="Discretionary",
            min_amount=5_000_000,
            min_amount_display="₦5,000,000",
            roi="~14-18% p.a.",
            duration="12 months",
            risk="Conservative",
            target_investors="Retail | HNI",
            description=(
                "Sharia-compliant discretionary investment. Prime Capital manages "
                "the portfolio on behalf of the client within Sharia guidelines."
            ),
            features=[
                "Sharia-compliant",
                "Fully managed by Prime Capital",
                "~14-18% target annual return",
                "Accessible entry point",
            ],
            is_active=True,
        ),
        Product(
            name="Prime Kids Al-Barakah",
            category="Ethical/Sharia",
            product_type="Sharia",
            currency="NGN",
            discretionary="Discretionary",
            min_amount=500_000,
            min_amount_display="₦500,000",
            roi="~12-15% p.a.",
            duration="12-36 months",
            risk="Conservative",
            target_investors="Minor accounts (guardian-managed)",
            description=(
                "A Sharia-compliant investment product designed for children. "
                "Managed by a guardian until the child reaches adulthood. "
                "Builds a financial foundation from an early age."
            ),
            features=[
                "Sharia-compliant",
                "Guardian-managed account",
                "Low minimum investment",
                "Long-term wealth building for children",
            ],
            is_active=True,
        ),
        Product(
            name="Prime Women Al-Barakah",
            category="Ethical/Sharia",
            product_type="Sharia",
            currency="NGN",
            discretionary="Discretionary",
            min_amount=1_000_000,
            min_amount_display="₦1,000,000",
            roi="~14-18% p.a.",
            duration="12 months",
            risk="Conservative",
            target_investors="Female investors",
            description=(
                "A Sharia-compliant investment product exclusively designed for women. "
                "Empowering female financial independence through ethical investing."
            ),
            features=[
                "Sharia-compliant",
                "Exclusively for women",
                "~14-18% target annual return",
                "Dedicated women's investment advisory",
            ],
            is_active=True,
        ),

        # ── Fixed Income ───────────────────────────────────────────────────
        Product(
            name="Prime Steady Income",
            category="Fixed Income",
            product_type="Conventional",
            currency="NGN",
            discretionary="Non-Discretionary",
            min_amount=5_000_000,
            min_amount_display="₦5,000,000",
            roi="~16-20% p.a.",
            duration="90-365 days",
            risk="Conservative",
            target_investors="Retail | HNI | Corporate",
            description=(
                "A conventional fixed-income product offering predictable returns "
                "through investments in money market instruments, bonds, and "
                "commercial paper."
            ),
            features=[
                "Predictable fixed returns",
                "~16-20% target annual return",
                "Flexible tenure (90-365 days)",
                "Monthly or quarterly income option",
            ],
            is_active=True,
        ),

        # ── FX / Dollar ────────────────────────────────────────────────────
        Product(
            name="Prime Dollar",
            category="FX/Dollar",
            product_type="Conventional",
            currency="USD",
            discretionary="Non-Discretionary",
            min_amount=5_000,
            min_amount_display="USD 5,000",
            roi="~8-12% p.a. (USD)",
            duration="12 months",
            risk="Balanced",
            target_investors="HNI | Diaspora | Corporate",
            description=(
                "A US Dollar-denominated investment product providing returns in USD. "
                "Protects against Naira devaluation while earning competitive "
                "dollar returns."
            ),
            features=[
                "USD-denominated returns",
                "Naira devaluation hedge",
                "~8-12% target annual return in USD",
                "Ideal for diaspora investors",
            ],
            is_active=True,
        ),

        # ── Equity ────────────────────────────────────────────────────────
        Product(
            name="Prime Alpha Equity",
            category="Equity",
            product_type="Conventional",
            currency="NGN",
            discretionary="Discretionary",
            min_amount=0,
            min_amount_display="Open to all",
            roi="Market-linked (variable)",
            duration="Open-ended",
            risk="Aggressive",
            target_investors="Retail | HNI",
            description=(
                "An actively managed equity portfolio targeting Nigerian Stock Exchange "
                "listed securities with alpha-generating strategies."
            ),
            features=[
                "NSE-listed equity focus",
                "Active portfolio management",
                "Open-ended investment",
                "Capital growth oriented",
            ],
            is_active=True,
        ),
        Product(
            name="Prime Steady Equity",
            category="Equity",
            product_type="Conventional",
            currency="NGN",
            discretionary="Discretionary",
            min_amount=0,
            min_amount_display="Open to all",
            roi="Market-linked (variable)",
            duration="Open-ended",
            risk="Balanced",
            target_investors="Retail | HNI",
            description=(
                "A balanced equity portfolio focused on dividend-paying, blue-chip "
                "NSE-listed stocks. Steady growth with lower volatility than Alpha Equity."
            ),
            features=[
                "Blue-chip equity focus",
                "Dividend income + capital growth",
                "Lower volatility than pure growth funds",
                "Open-ended investment",
            ],
            is_active=True,
        ),
    ]

    for p in products:
        db.add(p)
    logger.info(f"✓ {len(products)} investment products seeded.")


async def seed_fee_config(db: AsyncSession):
    """
    Seed default fee configuration (singleton — only one record, id=1).
    Managed from admin panel → Fees & Penalties section.
    """
    existing = await db.execute(select(FeeConfig).where(FeeConfig.id == 1))
    if existing.scalar_one_or_none():
        logger.info("Fee config already exists — skipping.")
        return

    fee_config = FeeConfig(
        id=1,
        premature_penalty_pct=20.0,     # 20% of accrued profit for early exit
        management_fee_pct=1.5,          # Annual management fee
        performance_fee_pct=10.0,        # Performance fee above benchmark
        liquidation_notice_days=5,       # Minimum 5 working days notice
        min_investment_ngn=500_000,      # ₦500,000 minimum for most products
        min_tenure_days=90,              # 90 days minimum investment period
        updated_by="System (seed)",
    )
    db.add(fee_config)
    logger.info("✓ Default fee configuration seeded.")


async def seed_payment_accounts(db: AsyncSession):
    """
    Seed default bank accounts shown to clients during subscription.
    Managed from admin panel → Payment Accounts section.
    Currently: Zenith Bank NGN only.
    TODO: Add USD account when Prime Dollar subscriptions go live.
    """
    existing = await db.execute(select(PaymentAccount))
    if existing.scalars().first():
        logger.info("Payment accounts already seeded — skipping.")
        return

    accounts = [
        PaymentAccount(
            bank="Zenith Bank",
            account_name="Prime Capital & Investment Ltd",
            account_number="2019283746",
            currency="NGN",
            label="Naira (₦) Investments",
            instruction=(
                "Please transfer your investment amount to the account above. "
                "Use your full name as the payment narration. "
                "Upload your payment receipt to complete your subscription."
            ),
            is_active=True,
        ),
        # ── TODO: Uncomment when USD account is ready ──────────────────────
        # PaymentAccount(
        #     bank="Zenith Bank",
        #     account_name="Prime Capital & Investment Ltd",
        #     account_number="YOUR_USD_ACCOUNT_NUMBER",
        #     currency="USD",
        #     label="Dollar (USD) Investments — Prime Dollar Product",
        #     instruction="Transfer USD to the account above. Use your full name as narration.",
        #     is_active=True,
        # ),
    ]

    for acc in accounts:
        db.add(acc)
    logger.info(f"✓ {len(accounts)} payment account(s) seeded.")


async def seed_system_settings(db: AsyncSession):
    """
    Seed default system settings (singleton — only one record, id=1).
    Contains company info and system alert banner (off by default).
    Managed from admin panel → System Settings section.
    """
    existing = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    if existing.scalar_one_or_none():
        logger.info("System settings already exist — skipping.")
        return

    settings_record = SystemSettings(
        id=1,
        company_name="Prime Capital & Investment Ltd",
        short_name="Prime Capital",
        email="info@primecapital.ng",
        phone="08100276250",
        whatsapp="2348100276250",
        address="No. 3 Sankuru Close, Off Rima Street, Maitama, Abuja",
        city="Abuja, Nigeria",
        website="www.primecapital.ng",
        regulator="Securities & Exchange Commission (SEC) Nigeria",
        # System alert banner — off by default
        alert_active=False,
        alert_message=None,
        alert_severity="info",
        # Signature images — None until uploaded via admin panel
        # TODO: Admin uploads MD/CEO and Company Secretary signatures
        # via System Settings → these URLs are then used on certificates
        md_signature_url=None,
        secretary_signature_url=None,
        updated_by="System (seed)",
    )
    db.add(settings_record)
    logger.info("✓ Default system settings seeded.")


async def seed_workflows(db: AsyncSession):
    """
    Seed the four default workflows from the v11 spec (NEW in v11):
      1. KYC Approval        — Operations Review → Compliance Approval → IT Activation
      2. Subscription Processing — Operations Review → Finance Confirmation
      3. Redemption Processing   — Operations Review → Finance Approval
      4. Client Account Creation — IT Setup (single step, mostly a checklist/notify step)

    Only seeds if no workflows exist yet — safe to run multiple times.
    IT Admin can edit/reconfigure all of this later from the Admin Panel;
    this just gives sensible working defaults out of the box.
    """
    existing = await db.execute(select(Workflow))
    if existing.scalars().first():
        logger.info("Workflows already seeded — skipping.")
        return

    # ── 1. KYC Approval ────────────────────────────────────────────────────
    kyc_wf = Workflow(
        name="KYC Approval",
        trigger="kyc_submitted",
        description="Reviews and approves client KYC submissions before account activation.",
        is_active=True,
    )
    db.add(kyc_wf)
    await db.flush()
    db.add_all([
        WorkflowStep(
            workflow_id=kyc_wf.id, step_order=1, name="Operations Review",
            description="Operations checks the submission is complete and documents are legible.",
            required_permission="can_approve_kyc",
            available_actions="approve,reject,request_info",
            sla_hours=24, notify_ceo=False,
        ),
        WorkflowStep(
            workflow_id=kyc_wf.id, step_order=2, name="Compliance Approval",
            description="Compliance verifies regulatory requirements and approves for onboarding.",
            required_permission="can_approve_kyc",
            available_actions="approve,reject,escalate",
            sla_hours=48, notify_ceo=False,
        ),
        WorkflowStep(
            workflow_id=kyc_wf.id, step_order=3, name="IT Activation",
            description="IT Admin activates the client's account access following approval.",
            required_permission="can_manage_clients",
            available_actions="approve,reject",
            sla_hours=24, notify_ceo=False,
        ),
    ])

    # ── 2. Subscription Processing ─────────────────────────────────────────
    sub_wf = Workflow(
        name="Subscription Processing",
        trigger="subscription_created",
        description="Reviews payment receipts and confirms subscriptions for activation.",
        is_active=True,
    )
    db.add(sub_wf)
    await db.flush()
    db.add_all([
        WorkflowStep(
            workflow_id=sub_wf.id, step_order=1, name="Operations Review",
            description="Operations verifies the uploaded payment receipt matches the subscription amount.",
            required_permission="can_manage_subscriptions",
            available_actions="approve,reject,request_info",
            sla_hours=24, notify_ceo=False,
        ),
        WorkflowStep(
            workflow_id=sub_wf.id, step_order=2, name="Finance Confirmation",
            description="Finance confirms funds received in the company account before activation.",
            required_permission="can_manage_subscriptions",
            available_actions="approve,reject",
            sla_hours=24, notify_ceo=True,
        ),
    ])

    # ── 3. Redemption Processing ────────────────────────────────────────────
    red_wf = Workflow(
        name="Redemption Processing",
        trigger="redemption_requested",
        description="Reviews and approves client redemption (withdrawal) requests before payout.",
        is_active=True,
    )
    db.add(red_wf)
    await db.flush()
    db.add_all([
        WorkflowStep(
            workflow_id=red_wf.id, step_order=1, name="Operations Review",
            description="Operations verifies the redemption request against the subscription record.",
            required_permission="can_manage_redemptions",
            available_actions="approve,reject,request_info",
            sla_hours=24, notify_ceo=False,
        ),
        WorkflowStep(
            workflow_id=red_wf.id, step_order=2, name="Finance Approval",
            description="Finance approves the payout and processes the transfer to the client.",
            required_permission="can_manage_redemptions",
            available_actions="approve,reject,escalate",
            sla_hours=48, notify_ceo=True,
        ),
    ])

    # ── 4. Client Account Creation ─────────────────────────────────────────
    client_wf = Workflow(
        name="Client Account Creation",
        trigger="client_account_created",
        description="Follow-up checklist after IT Admin creates a client account (e.g. welcome call, KYC nudge).",
        is_active=True,
    )
    db.add(client_wf)
    await db.flush()
    db.add_all([
        WorkflowStep(
            workflow_id=client_wf.id, step_order=1, name="Operations Follow-Up",
            description="Operations confirms the client received their login details and completed KYC.",
            required_permission="can_manage_clients",
            available_actions="approve,reject",
            sla_hours=72, notify_ceo=False,
        ),
    ])

    logger.info("✓ Default workflows seeded: KYC Approval, Subscription Processing, "
                "Redemption Processing, Client Account Creation")


async def run_seed():
    """Run all seed functions in a single database transaction."""
    logger.info("=" * 50)
    logger.info("  Prime Capital — Database Seeding")
    logger.info("=" * 50)

    async with AsyncSessionLocal() as db:
        try:
            await seed_super_admin(db)
            await seed_products(db)
            await seed_fee_config(db)
            await seed_payment_accounts(db)
            await seed_system_settings(db)
            await seed_workflows(db)
            await db.commit()
            logger.info("=" * 50)
            logger.info("  ✓ All seed data committed successfully!")
            logger.info("=" * 50)
        except Exception as e:
            await db.rollback()
            logger.error(f"Seeding failed: {e}")
            raise


if __name__ == "__main__":
    asyncio.run(run_seed())
