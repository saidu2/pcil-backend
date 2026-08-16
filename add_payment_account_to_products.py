# ─────────────────────────────────────────────────────────────────────────────
# migrations/add_payment_account_to_products.py
#
# Adds payment_account_id FK column to the products table.
# Run ONCE: python migrations/add_payment_account_to_products.py
# Safe to run multiple times (IF NOT EXISTS guard).
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = "postgresql+asyncpg://postgres:PrimeCapital2026@localhost:5432/primecapital"


async def run():
    engine = create_async_engine(DATABASE_URL)

    async with engine.begin() as conn:
        logger.info("Adding payment_account_id column to products table...")

        # Add column — safe to run multiple times
        await conn.execute(text("""
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS payment_account_id UUID
            REFERENCES payment_accounts(id) ON DELETE SET NULL;
        """))
        logger.info("✓ payment_account_id column added (or already existed)")

        # Add index for fast joins
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_products_payment_account_id
            ON products(payment_account_id);
        """))
        logger.info("✓ Index on payment_account_id created")

    await engine.dispose()
    logger.info("Migration complete.")


if __name__ == "__main__":
    asyncio.run(run())
