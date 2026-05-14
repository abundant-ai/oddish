import asyncio
import re as _re
import sys
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from sqlalchemy import text
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker  # type: ignore[attr-defined]
from oddish.config import settings
from oddish.db.models import Base
from oddish.db.soft_delete import install_soft_delete_filter

# Install the soft-delete auto-filter exactly once at module load. The
# listener is keyed on the SQLAlchemy ``Session`` class, so every session
# minted by the shared session maker -- in oddish *and* in the backend
# (both packages reuse this engine) -- inherits the filter automatically.
install_soft_delete_filter()

# Ensure we use asyncpg driver explicitly (URL should already have +asyncpg).
db_url = settings.database_url


def _base_connect_args() -> dict[str, object]:
    """Connection kwargs used by both SQLAlchemy (via connect_args) and the
    direct asyncpg.create_pool path.

    The `server_settings` block is what prevents the Supavisor / terminated-
    worker zombie-transaction class of incident: it makes Postgres itself
    abort any transaction left idle too long, releasing its locks, even if
    the client process was SIGKILLed without a chance to rollback.

    IMPORTANT: called fresh on every engine creation (not cached at import
    time) so `reconfigure_database_connections()` picks up any runtime
    settings overrides -- e.g. backend/worker/functions.py mutating
    Settings.db_pool_size before importing this module.
    """
    return {
        "statement_cache_size": 0,
        "timeout": 10,
        "command_timeout": 30,
        "server_settings": settings.asyncpg_server_settings(),
    }


def _create_engine() -> AsyncEngine:
    # Disable prepared statements for connection poolers (Supavisor, PgBouncer)
    # that run in transaction mode. Pre-ping and LIFO checkout reduce failures
    # from stale pooled connections in long-lived API containers.
    connect_args = _base_connect_args()
    if settings.db_use_null_pool:
        return create_async_engine(
            db_url,
            echo=False,
            connect_args=connect_args,
            poolclass=pool.NullPool,
        )

    return create_async_engine(
        db_url,
        echo=False,
        connect_args=connect_args,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_use_lifo=True,
    )


def _create_session_maker(
    db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


engine = _create_engine()
async_session_maker = _create_session_maker(engine)

# Global connection pool for asyncpg (used by queue workers)
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
            timeout=10,
            command_timeout=30,
            server_settings=settings.asyncpg_server_settings(),
        )
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def close_engine() -> None:
    """Dispose the SQLAlchemy engine and release pooled connections."""
    await engine.dispose()


async def close_database_connections() -> None:
    """Close both asyncpg and SQLAlchemy database connections."""
    await close_pool()
    await close_engine()


async def reconfigure_database_connections() -> None:
    """Rebuild DB clients after runtime pool-size overrides change.

    Modal workers adjust pool sizes at runtime, but the SQLAlchemy engine is
    created at import time. Recreate it so the current settings actually take
    effect, especially in reused worker containers.
    """
    global engine, async_session_maker
    await close_database_connections()
    engine = _create_engine()
    async_session_maker = _create_session_maker(engine)


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
        except BaseException:
            await session.rollback()
            raise


# =============================================================================
# Database Initialization
# =============================================================================


async def init_db():
    """Initialize database schema."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# Conservative identifier shape for role names we're willing to splice
# into a DDL statement. Postgres technically allows more (quoted
# identifiers can contain anything but nul), but Supabase/standard
# deployments only ever produce names in this set, and we'd rather
# fail loudly than do risky escaping.
_ROLE_NAME_RE = _re.compile(r"^[A-Za-z0-9_.]{1,64}$")


async def apply_role_defaults() -> dict[str, str]:
    """Install server-side defaults that aren't reliably delivered via
    per-connection `server_settings`.

    The big one is `idle_in_transaction_session_timeout`: Supavisor (the
    transaction-mode pooler in front of Supabase) silently drops
    client-supplied server_settings, so the only way to guarantee that
    orphaned transactions are auto-aborted is to stick the GUC onto the
    connecting role itself. New Postgres backends inherit it; already-
    established pool backends will pick it up when they next reconnect
    (or you can terminate them to force a refresh).

    Idempotent. Safe to call at startup. Raises if the connected role
    lacks ALTER ROLE privilege -- call sites should handle that.
    """
    from sqlalchemy import text as _text

    timeout_ms = int(settings.idle_in_transaction_session_timeout_ms)
    async with engine.begin() as conn:
        # Who are we actually connecting as? Supavisor uses the shared
        # `postgres` role internally, while direct Postgres connections
        # usually use an app-specific role. `current_user` in the context
        # of the running session is what gets the default applied.
        current_user = (await conn.execute(_text("SELECT current_user"))).scalar_one()
        # Defense in depth: we have to interpolate the role name into
        # DDL (parameter binding doesn't work for identifiers), and
        # `current_user` comes from Postgres itself -- but validate
        # the shape anyway so we fail safely on anything weird.
        if not isinstance(current_user, str) or not _ROLE_NAME_RE.match(current_user):
            raise ValueError(
                f"Refusing to ALTER ROLE: unexpected current_user shape: "
                f"{current_user!r}"
            )
        await conn.execute(
            _text(
                f'ALTER ROLE "{current_user}" '
                f"SET idle_in_transaction_session_timeout = '{timeout_ms}'"
            )
        )
    return {
        "role": current_user,
        "idle_in_transaction_session_timeout_ms": str(timeout_ms),
    }


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
        print("Full setup: running Alembic migrations...")
        print("Running: alembic upgrade head")
        result = subprocess.run(["alembic", "upgrade", "head"], capture_output=False)
        if result.returncode != 0:
            print("✗ Alembic migration failed")
            sys.exit(1)
        print("\n✓ Full setup complete!")

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
        print("  setup            - Full setup (Alembic migrations)")
        print(
            "  reset            - Drop and recreate all tables (WARNING: destructive)"
        )
        print(
            "  purge            - Delete all rows from all public tables (preserves alembic_version)"
        )
        sys.exit(1)


if __name__ == "__main__":
    _run_cli()
