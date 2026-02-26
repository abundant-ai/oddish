import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Add src directory to path so we can import oddish
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

# Import your models
from oddish.config import settings  # noqa: E402
from oddish.db.models import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set the database URL from environment
db_url = settings.database_url
# Ensure we use asyncpg driver explicitly (URL should already have +asyncpg).
# If URL doesn't have +asyncpg, add it to avoid falling back to psycopg2.
if db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

config.set_main_option("sqlalchemy.url", db_url)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table="alembic_version_backend",
        include_object=lambda obj, name, type_, reflected, compare_to: (
            # Exclude pgqueuer tables from autogenerate
            not (type_ == "table" and name and name.startswith("pgqueuer"))
        ),
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_schemas=False,
        version_table="alembic_version_backend",
        include_object=lambda obj, name, type_, reflected, compare_to: (
            # Exclude pgqueuer tables from autogenerate
            not (type_ == "table" and name and name.startswith("pgqueuer"))
        ),
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async support."""
    # Use the asyncpg URL to avoid psycopg2 dependency.
    connectable = create_async_engine(
        db_url,
        # Disable prepared statement caching for compatibility with
        # transaction/statement poolers (PgBouncer, Supavisor, etc).
        connect_args={"statement_cache_size": 0},
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
