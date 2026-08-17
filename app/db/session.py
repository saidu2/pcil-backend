# ─────────────────────────────────────────────────────────────────────────────
# app/db/session.py
#
# SQLAlchemy async database engine and session factory.
# FastAPI uses dependency injection to provide a database session
# to each request — the session is automatically closed when the
# request finishes (even if an exception is raised).
# ─────────────────────────────────────────────────────────────────────────────

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


# ── pgbouncer compatibility ───────────────────────────────────────────────────
# Supabase offers two ways in:
#
#   direct  db.<project>.supabase.co:5432   plain Postgres
#   pooled  <region>.pooler.supabase.com:6543   behind pgbouncer
#
# The pooled endpoint runs pgbouncer in transaction mode, which does not
# support prepared statements. That is only a problem for the pooled
# endpoint, so the workaround below is applied conditionally rather than
# always: switching it off unnecessarily costs a little query performance
# locally.
#
# NOTE: passing statement_cache_size / prepared_statement_cache_size via
# connect_args is NOT enough on its own. SQLAlchemy's asyncpg dialect runs
# its own internal setup query (a JSON/JSONB codec introspection) during
# connection bootstrap, BEFORE connect_args are applied, using a fixed
# statement name. Under pgbouncer's transaction pooling that collides
# across reused backends and raises DuplicatePreparedStatementError no
# matter what connect_args says. The fix is async_creator, which bypasses
# SQLAlchemy's dialect bootstrap entirely and hands it a raw asyncpg
# connection that we control — so statement_cache_size=0 actually applies
# to every connection, including SQLAlchemy's own internal ones.
_IS_POOLED = "pooler.supabase.com" in settings.DATABASE_URL or ":6543" in settings.DATABASE_URL

if _IS_POOLED:
    # asyncpg.connect() wants a plain "postgresql://" DSN, not the
    # "postgresql+asyncpg://" SQLAlchemy dialect prefix.
    _PLAIN_DSN = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    async def _asyncpg_creator() -> asyncpg.Connection:
        """
        Raw asyncpg connection factory, passed to SQLAlchemy as async_creator.
        statement_cache_size=0 disables ALL prepared-statement caching
        inside asyncpg for every connection made this way.
        """
        return await asyncpg.connect(
            dsn=_PLAIN_DSN,
            statement_cache_size=0,
        )

    # ── Engine (pooled / pgbouncer) ───────────────────────────────────────────
    engine: AsyncEngine = create_async_engine(
        settings.DATABASE_URL,
        async_creator=_asyncpg_creator,
        poolclass=NullPool,          # pgbouncer already pools; avoid double-pooling
        echo=settings.APP_DEBUG,
    )
else:
    # ── Engine (direct connection) ────────────────────────────────────────────
    # pool_pre_ping=True tests a connection before handing it out, which avoids
    # "connection closed" errors after an idle period. That matters on hosts
    # that sleep or restart often, such as Render's free tier.
    #
    # POOL SIZING: values below are per worker. With a small number of
    # gunicorn workers this stays well inside Supabase's free tier connection
    # limit, leaving headroom for migrations, the Supabase dashboard, or any
    # manual query.
    engine: AsyncEngine = create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        echo=settings.APP_DEBUG,
        pool_size=5,                 # persistent connections per worker
        max_overflow=5,              # extra connections at peak, per worker
        pool_recycle=1800,           # recycle after 30 min, avoids stale sockets
    )


# ── Session Factory ───────────────────────────────────────────────────────────
# AsyncSessionLocal creates new database sessions.
# expire_on_commit=False: Allows accessing model attributes after commit
# without re-querying (important in async context).
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ── Base Class ────────────────────────────────────────────────────────────────
# All SQLAlchemy models inherit from this Base.
# Import this in every model file: from app.db.session import Base
class Base(DeclarativeBase):
    pass


# ── Dependency ────────────────────────────────────────────────────────────────
async def get_db() -> AsyncSession:
    """
    FastAPI dependency that provides a database session per request.

    Usage in any endpoint:
        @router.get("/something")
        async def my_endpoint(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(User))
            ...

    The 'async with' block ensures the session is always closed,
    and rolls back on exception to prevent partial writes.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
