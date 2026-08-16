"""
add_unitised_columns_migration.py
──────────────────────────────────
Adds the unitised fund model columns to the database.
SAFE TO RUN MULTIPLE TIMES — skips columns that already exist.

Columns added:
  products.initial_nav        FLOAT DEFAULT 100.0   (NAV per unit at product launch)
  subscriptions.nav_at_entry  FLOAT NULL            (NAV when client was activated)
  subscriptions.units_allocated FLOAT NULL          (units the client holds)

From pcil-backend folder with venv active:
  python add_unitised_columns_migration.py
"""

import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = "postgresql+asyncpg://postgres:PrimeCapital2026@localhost:5432/primecapital"


async def run():
    engine = create_async_engine(DATABASE_URL, echo=False)

    migrations = [
        (
            "products.initial_nav",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS initial_nav FLOAT DEFAULT 100.0",
        ),
        (
            "subscriptions.nav_at_entry",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS nav_at_entry FLOAT",
        ),
        (
            "subscriptions.units_allocated",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS units_allocated FLOAT",
        ),
    ]

    async with engine.begin() as conn:
        logger.info("Running unitised model migration...")
        logger.info("")

        for name, sql in migrations:
            try:
                await conn.execute(text(sql))
                logger.info(f"  ✓  {name}")
            except Exception as e:
                logger.warning(f"  ⚠  {name} — {e}")

        # Verify columns exist after migration
        logger.info("")
        logger.info("Verifying columns...")

        checks = [
            ("products",      "initial_nav"),
            ("subscriptions", "nav_at_entry"),
            ("subscriptions", "units_allocated"),
        ]
        all_ok = True
        for table, col in checks:
            result = await conn.execute(text(f"""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = '{table}' AND column_name = '{col}'
            """))
            exists = result.scalar() > 0
            status = "✓" if exists else "✗ MISSING"
            logger.info(f"  {status}  {table}.{col}")
            if not exists:
                all_ok = False

        logger.info("")
        if all_ok:
            logger.info("Migration complete. All columns present.")
            logger.info("Restart uvicorn: uvicorn app.main:app --port 8000")
        else:
            logger.error("Some columns are missing. Check the errors above.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
