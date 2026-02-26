from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
# NOTE: This is the first cloud migration. It requires OSS oddish migrations
# to be run first (up to b2c3d4e5f6a7_add_cloud_ready_columns).
# Run: cd ../oddish && alembic upgrade head
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Requires OSS oddish schema to be set up first.
    This migration adds cloud-specific tables (organizations, users, api_keys)
    and FK constraints that link tasks to these tables.
    """
    # Check that tasks table exists (OSS migration must have run)
    bind = op.get_bind()
    inspector = inspect(bind)
    if "tasks" not in inspector.get_table_names():
        raise RuntimeError(
            "OSS oddish migrations must be run first. "
            "Run: cd ../oddish && alembic upgrade head"
        )
    # ==========================================================================
    # 1. Create organizations table
    # ==========================================================================
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            slug VARCHAR(255) UNIQUE NOT NULL,
            plan VARCHAR(32) DEFAULT 'free' NOT NULL,
            settings JSONB DEFAULT '{}' NOT NULL,
            is_active BOOLEAN DEFAULT 'true' NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )
        """
    )

    # ==========================================================================
    # 2. Create users table
    # ==========================================================================
    op.execute(
        """
        DO $$
        BEGIN
            CREATE TYPE userrole AS ENUM ('owner', 'admin', 'member');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id VARCHAR(64) PRIMARY KEY,
            supabase_user_id VARCHAR(64) UNIQUE,
            org_id VARCHAR(64) NOT NULL,
            role userrole DEFAULT 'member' NOT NULL,
            email VARCHAR(255) NOT NULL,
            name VARCHAR(255),
            avatar_url TEXT,
            is_active BOOLEAN DEFAULT 'true' NOT NULL,
            last_login_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            CONSTRAINT uq_users_org_email UNIQUE (org_id, email)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_org_id ON users (org_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_supabase_user_id ON users (supabase_user_id)"
    )

    # ==========================================================================
    # 3. Create api_keys table
    # ==========================================================================
    op.execute(
        """
        DO $$
        BEGIN
            CREATE TYPE apikeyscope AS ENUM ('full', 'tasks', 'read');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id VARCHAR(64) PRIMARY KEY,
            org_id VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL,
            key_prefix VARCHAR(16) NOT NULL,
            key_hash VARCHAR(128) UNIQUE NOT NULL,
            scope apikeyscope DEFAULT 'full' NOT NULL,
            created_by_user_id VARCHAR(64),
            is_active BOOLEAN DEFAULT 'true' NOT NULL,
            expires_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_org_id ON api_keys (org_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys (key_hash)"
    )

    # ==========================================================================
    # 4. Add cloud columns to tasks table and create FK constraints
    # ==========================================================================
    # Note: These columns may already exist from OSS migrations (as plain strings).
    # We add them idempotently and then create FK constraints for cloud.

    # Add org_id column if not exists
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS org_id VARCHAR(64)")
    # Create index if not exists
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_org_id ON tasks (org_id)")
    # Add FK constraint for cloud (links to organizations table)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_tasks_org_id'
            ) THEN
                ALTER TABLE tasks
                ADD CONSTRAINT fk_tasks_org_id
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )

    # Add created_by_user_id column if not exists
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_by_user_id VARCHAR(64)"
    )
    # Add FK constraint for cloud (links to users table)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_tasks_created_by_user_id'
            ) THEN
                ALTER TABLE tasks
                ADD CONSTRAINT fk_tasks_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_users_org_id'
            ) THEN
                ALTER TABLE users
                ADD CONSTRAINT fk_users_org_id
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_api_keys_org_id'
            ) THEN
                ALTER TABLE api_keys
                ADD CONSTRAINT fk_api_keys_org_id
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_api_keys_created_by_user_id'
            ) THEN
                ALTER TABLE api_keys
                ADD CONSTRAINT fk_api_keys_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Remove FK constraints from tasks (keep columns for OSS compatibility)
    op.drop_constraint("fk_tasks_created_by_user_id", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_org_id", "tasks", type_="foreignkey")
    # Note: We keep the columns (org_id, created_by_user_id) as they're part of OSS schema now

    # Drop api_keys table
    op.drop_index("idx_api_keys_key_hash", table_name="api_keys")
    op.drop_index("idx_api_keys_org_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.execute("DROP TYPE IF EXISTS apikeyscope")

    # Drop users table
    op.drop_index("idx_users_supabase_user_id", table_name="users")
    op.drop_index("idx_users_email", table_name="users")
    op.drop_index("idx_users_org_id", table_name="users")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS userrole")

    # Drop organizations table
    op.drop_table("organizations")
