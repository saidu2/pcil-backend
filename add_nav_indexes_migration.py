import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = "postgresql+asyncpg://postgres:PrimeCapital2026@localhost:5432/primecapital"


async def run():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:

        logger.info("Creating nav_records table...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS nav_records (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id      UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                period_label    VARCHAR(50) NOT NULL,
                period_start    TIMESTAMPTZ NOT NULL,
                period_end      TIMESTAMPTZ NOT NULL,
                nav_per_unit    FLOAT,
                return_pct      FLOAT,
                cumulative_pct  FLOAT,
                total_aum       FLOAT,
                notes           TEXT,
                source          VARCHAR(20) DEFAULT 'manual',
                entered_by      UUID REFERENCES admin_users(id),
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_nav_product_period UNIQUE (product_id, period_label)
            );
        """))
        logger.info("  nav_records table OK")

        indexes = [
            "CREATE INDEX IF NOT EXISTS ix_users_kyc_status    ON users(kyc_status)",
            "CREATE INDEX IF NOT EXISTS ix_users_account_type  ON users(account_type)",
            "CREATE INDEX IF NOT EXISTS ix_users_is_active     ON users(is_active)",
            "CREATE INDEX IF NOT EXISTS ix_subs_status         ON subscriptions(status)",
            "CREATE INDEX IF NOT EXISTS ix_subs_product_id     ON subscriptions(product_id)",
            "CREATE INDEX IF NOT EXISTS ix_subs_user_status    ON subscriptions(user_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_subs_submitted_at   ON subscriptions(submitted_at)",
            "CREATE INDEX IF NOT EXISTS ix_redemptions_status  ON redemptions(status)",
            "CREATE INDEX IF NOT EXISTS ix_redemptions_user_status ON redemptions(user_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_notif_user_read     ON notifications(user_id, is_read)",
            "CREATE INDEX IF NOT EXISTS ix_nav_product_id      ON nav_records(product_id)",
            "CREATE INDEX IF NOT EXISTS ix_nav_period_end      ON nav_records(period_end DESC)",
        ]

        logger.info("Adding indexes...")
        for sql in indexes:
            try:
                await conn.execute(text(sql))
                name = sql.split("IF NOT EXISTS ")[1].split(" ON")[0]
                logger.info(f"  {name} OK")
            except Exception as e:
                logger.warning(f"  Skipped: {e}")

    logger.info("")
    logger.info("Migration complete. Restart uvicorn now.")

asyncio.run(run())
