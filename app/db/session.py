# ─────────────────────────────────────────────────────────────────────────────
# app/db/session.py
#
# SQLAlchemy async database engine and session factory.
# FastAPI uses dependency injection to provide a database session
# to each request — the session is automatically closed when the
# request finishes (even if an exception is raised).
# ─────────────────────────────────────────────────────────────────────────────

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
# support prepared statements. asyncpg uses them by default and caches them
# per connection, so against the pooler you get:
#
#   DuplicatePreparedStatementError: prepared statement "__asyncpg_stmt_3__"
#   already exists
#
# The fix is to disable asyncpg's statement caching. That is only needed for
# the pooled endpoint, so it is applied conditionally rather than always:
# switching it off unnecessarily costs a little query performance locally.
_IS_POOLED = "pooler.supabase.com" in settings.DATABASE_URL or ":6543" in settings.DATABASE_URL

_connect_args = {}
if _IS_POOLED:
    _connect_args = {
        # Disable asyncpg's prepared statement cache entirely.
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }


# ── Engine ────────────────────────────────────────────────────────────────────
# pool_pre_ping=True tests a connection before handing it out, which avoids
# "connection closed" errors after an idle period. That matters on hosts that
# sleep or restart often, such as Render's free tier.
#
# POOL SIZING: the previous values (pool_size=10, max_overflow=20) allowed up
# to 30 connections PER WORKER. With 2 gunicorn workers that is 60, which is
# the entire connection limit on Supabase's free tier, leaving nothing for
# migrations, the Supabase dashboard, or any manual query. The values below
# are per worker and stay well inside the limit.
#
# When connecting through pgbouncer, SQLAlchemy's own pooling is redundant
# because pgbouncer is already pooling. NullPool avoids holding connections
# open on both sides, which is the recommended setup and further reduces
# the chance of exhausting the limit.
if _IS_POOLED:
    engine: AsyncEngine = create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        echo=settings.APP_DEBUG,
        poolclass=NullPool,          # pgbouncer handles pooling
        connect_args=_connect_args,
    )
else:
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
