# ─────────────────────────────────────────────────────────────────────────────
# alembic/env.py
#
# Alembic migration environment.
# Alembic reads this file to know how to connect to the database and
# where to find the SQLAlchemy models.
#
# ── How to use Alembic ───────────────────────────────────────────────────────
#
#  First time setup (run once):
#    alembic upgrade head        ← creates all tables from models
#
#  After changing any model in app/models/models.py:
#    alembic revision --autogenerate -m "describe your change"
#    alembic upgrade head        ← apply the migration
#
#  To see migration history:
#    alembic history
#
#  To rollback one migration:
#    alembic downgrade -1
#
#  IMPORTANT: Never edit the database schema manually (no ALTER TABLE).
#  Always use Alembic migrations so changes are tracked in version control.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ── Make sure the project root (pcil-backend/) is on sys.path ───────────────
# When alembic is run as the installed console-script (venv\Scripts\alembic.exe
# on Windows, as opposed to `python -m alembic`), Python does NOT add the
# current working directory to sys.path — so `from app...` imports below
# fail with ModuleNotFoundError even though app/ is right there. This adds
# the folder one level up from alembic/ (i.e. pcil-backend/) explicitly.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Alembic config object (reads from alembic.ini)
config = context.config

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Import models so Alembic can detect them ─────────────────────────────────
# Every model class must be imported here (or in a module that imports them all).
# If you add a new model file, import it below.
from app.db.session import Base
from app.models import models  # noqa: F401 — imports all models
from app.core.config import settings

# Override the database URL from .env (takes priority over alembic.ini)
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Generates SQL scripts without connecting to the database.
    Useful for generating migration scripts to review before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using async SQLAlchemy engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to the database directly."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
