import asyncio
import sys
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker  # type: ignore[attr-defined]
from pgqueuer.db import AsyncpgPoolDriver
from pgqueuer.queries import Queries

from oddish.config import settings
from oddish.db.models import Base

# Ensure we use asyncpg driver explicitly (URL should already have +asyncpg).
db_url = settings.database_url

# Disable prepared statements for connection poolers (Supavisor, PgBouncer)
# that run in transaction mode
engine = create_async_engine(
    db_url,
    echo=False,
    connect_args={"statement_cache_size": 0},
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_pool_max_overflow,
)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)

# Global connection pool for asyncpg (used by PGQueuer)
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Get or create the asyncpg connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.asyncpg_url,
            min_size=settings.asyncpg_pool_min_size,
            max_size=settings.asyncpg_pool_max_size,
            # Disable prepared statement caching for compatibility with
            # transaction/statement poolers (PgBouncer, Supavisor, etc).
            statement_cache_size=0,
        )
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Get a database session with automatic commit/rollback.

    Oddish relies on transactional semantics:
    - API submissions must persist task/trial inserts.
    - Workers must persist claim/complete state transitions.

    This context manager commits on success and rolls back on error.
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# =============================================================================
# Database Initialization
# =============================================================================


async def init_db():
    """Initialize database schema."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db():
    """Drop all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def reset_db():
    """Drop and recreate all tables."""
    await drop_db()
    await init_db()


async def purge_db_data(
    *,
    include_alembic_version: bool = False,
) -> None:
    """Delete all rows from all tables in the `public` schema.

    This is intentionally non-destructive to schema: it clears data while keeping
    tables, types, functions, and migrations intact.

    By default, this preserves Alembic's migration tracking table (`alembic_version`)
    so you don't accidentally "forget" which migrations have been applied.
    It also preserves auth/org tables so cleanup doesn't wipe access control data.
    """

    def _quote_ident(name: str) -> str:
        # Safe identifier quoting for dynamic table/schema names.
        return '"' + name.replace('"', '""') + '"'

    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT schemaname, tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                    ORDER BY schemaname, tablename
                    """
                )
            )
        ).all()

        protected_tables = {"alembic_version", "organizations", "users", "api_keys"}
        tables: list[str] = []
        for schemaname, tablename in rows:
            if not include_alembic_version and tablename == "alembic_version":
                continue
            if tablename in protected_tables:
                continue
            tables.append(f"{_quote_ident(schemaname)}.{_quote_ident(tablename)}")

        if not tables:
            return

        # One statement avoids FK issues like "trials references tasks".
        await conn.execute(
            text("TRUNCATE TABLE " + ", ".join(tables) + " RESTART IDENTITY CASCADE;")
        )


# =============================================================================
# PGQueuer Setup
# =============================================================================


async def install_pgqueuer():
    """Install PGQueuer tables.

    This creates the pgqueuer queue tables. Run this once during setup.
    """
    pool = await get_pool()
    driver = AsyncpgPoolDriver(pool)
    queries = Queries(driver)
    try:
        await queries.install()
    except asyncpg.exceptions.DuplicateObjectError:
        # Re-running setup against an existing dev DB is common; treat this as
        # "already installed" rather than failing the whole setup.
        return


async def uninstall_pgqueuer():
    """Uninstall PGQueuer tables."""
    pool = await get_pool()
    driver = AsyncpgPoolDriver(pool)
    queries = Queries(driver)
    await queries.uninstall()


# =============================================================================
# CLI entry point for `python -m oddish.db`
# =============================================================================


def _run_cli():
    command = sys.argv[1] if len(sys.argv) > 1 else "init"

    if command == "init":
        print("Initializing database with Alembic migrations...")
        print("Running: alembic upgrade head")
        result = subprocess.run(["alembic", "upgrade", "head"], capture_output=False)
        if result.returncode == 0:
            print("✓ Database initialized!")
        else:
            print("✗ Database initialization failed")
            sys.exit(1)

    elif command == "setup":
        print("Full setup: Alembic migrations + PGQueuer...")
        print("Running: alembic upgrade head")
        result = subprocess.run(["alembic", "upgrade", "head"], capture_output=False)
        if result.returncode != 0:
            print("✗ Alembic migration failed")
            sys.exit(1)

        print("\nInstalling PGQueuer tables...")
        asyncio.run(install_pgqueuer())
        print("✓ PGQueuer installed!")
        print("\n✓ Full setup complete!")

    elif command == "install-pgqueuer":
        print("Installing PGQueuer tables...")
        asyncio.run(install_pgqueuer())
        print("✓ PGQueuer installed!")

    elif command == "uninstall-pgqueuer":
        print("Uninstalling PGQueuer tables...")
        asyncio.run(uninstall_pgqueuer())
        print("✓ PGQueuer uninstalled!")

    elif command == "reset":
        print("WARNING: This will drop all tables and recreate them!")
        print("Press Ctrl+C to cancel, or wait 3 seconds to continue...")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)

        print("\nDropping all tables...")
        asyncio.run(drop_db())
        print("Running: alembic upgrade head")
        result = subprocess.run(["alembic", "upgrade", "head"], capture_output=False)
        if result.returncode != 0:
            print("✗ Alembic migration failed")
            sys.exit(1)
        print("Installing PGQueuer...")
        asyncio.run(install_pgqueuer())
        print("✓ Database reset complete!")

    elif command == "purge":
        print(
            "WARNING: This will delete ALL rows from ALL tables in the public schema!"
        )
        print("Press Ctrl+C to cancel, or wait 3 seconds to continue...")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)

        asyncio.run(purge_db_data())
        print("✓ Database data purged (public schema, preserving alembic_version)!")

    else:
        print(f"Unknown command: {command}")
        print("\nAvailable commands:")
        print("  init             - Run Alembic migrations")
        print("  setup            - Full setup (Alembic + PGQueuer)")
        print("  install-pgqueuer - Install PGQueuer tables")
        print("  uninstall-pgqueuer - Remove PGQueuer tables")
        print(
            "  reset            - Drop and recreate all tables (WARNING: destructive)"
        )
        print(
            "  purge            - Delete all rows from all public tables (preserves alembic_version)"
        )
        sys.exit(1)


if __name__ == "__main__":
    _run_cli()
