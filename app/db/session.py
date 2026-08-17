import uuid
import asyncpg

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings

_IS_POOLED = "pooler.supabase.com" in settings.DATABASE_URL or ":6543" in settings.DATABASE_URL

if _IS_POOLED:
    # asyncpg.connect() wants a plain "postgresql://" DSN, not the
    # "postgresql+asyncpg://" SQLAlchemy dialect prefix.
    _PLAIN_DSN = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    async def _asyncpg_creator() -> asyncpg.Connection:
        """
        Raw asyncpg connection factory, passed to SQLAlchemy as async_creator.

        SQLAlchemy's asyncpg dialect normally runs its own setup query
        (a JSON/JSONB codec introspection) BEFORE any connect_args are
        applied, using a fixed statement name. Under PgBouncer's
        transaction pooling, that collides across reused backends and
        raises DuplicatePreparedStatementError no matter what
        connect_args says. async_creator bypasses that dialect bootstrap
        entirely, so statement_cache_size=0 actually applies to every
        connection, including SQLAlchemy's own internal ones.
        """
        return await asyncpg.connect(
            dsn=_PLAIN_DSN,
            statement_cache_size=0,
        )

    engine: AsyncEngine = create_async_engine(
        settings.DATABASE_URL,
        async_creator=_asyncpg_creator,
        poolclass=NullPool,          # pgbouncer already pools; avoid double-pooling
        echo=settings.APP_DEBUG,
    )
else:
    engine: AsyncEngine = create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        echo=settings.APP_DEBUG,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
    )