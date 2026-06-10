"""add tag tables (core write model)

Creates the org-scoped tag vocabulary, assignments, exclusions, grants,
audit events, governance policies, and saved tag-filters. All new tables
are empty at creation, so plain (non-CONCURRENT) indexes are safe.

Partial-unique indexes use the validated ``COALESCE(org_id, '')``
expression form so soft-deleted rows free up their name slot.

Revision ID: aa00ta01core
Revises: aa03ta04kind
Create Date: 2026-06-06 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "aa00ta01core"
down_revision: Union[str, Sequence[str], None] = "aa03ta04kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ENUM_DROP_ORDER = [
    "tag_policy_profanity_mode",
    "tag_policy_who_can_create",
    "saved_tag_filter_visibility",
    "tag_event_source",
    "tag_event_actor",
    "tag_event_action",
    "tag_grant_capability",
    "tag_grant_principal",
    "tag_assignment_source",
    "tag_assignment_state",
    "tag_assignment_scope",
    "tag_visibility",
    "tag_state",
]


def _create_enum_if_missing(name: str, sql: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = '{name}'
            ) THEN
                {sql};
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _create_enum_if_missing(
        "tag_state",
        "CREATE TYPE tag_state AS ENUM ('ACTIVE', 'ARCHIVED', 'MERGED', 'DELETED')",
    )
    _create_enum_if_missing(
        "tag_visibility",
        "CREATE TYPE tag_visibility AS ENUM ('PRIVATE', 'PUBLIC')",
    )
    _create_enum_if_missing(
        "tag_assignment_scope",
        "CREATE TYPE tag_assignment_scope AS ENUM ('VERSION', 'TASK', 'EXPERIMENT')",
    )
    _create_enum_if_missing(
        "tag_assignment_state",
        "CREATE TYPE tag_assignment_state AS ENUM ('ACTIVE', 'REMOVED')",
    )
    _create_enum_if_missing(
        "tag_assignment_source",
        "CREATE TYPE tag_assignment_source AS ENUM ("
        "'DIRECT', 'EXPERIMENT_SNAPSHOT', 'EXPERIMENT_LIVING')",
    )
    _create_enum_if_missing(
        "tag_grant_principal",
        "CREATE TYPE tag_grant_principal AS ENUM ('USER', 'ALL_MEMBERS')",
    )
    _create_enum_if_missing(
        "tag_grant_capability",
        "CREATE TYPE tag_grant_capability AS ENUM ("
        "'RENAME', 'MERGE', 'DELETE', 'EDIT')",
    )
    _create_enum_if_missing(
        "tag_event_action",
        "CREATE TYPE tag_event_action AS ENUM ("
        "'CREATE', 'EDIT', 'RENAME', 'ARCHIVE', 'UNARCHIVE', 'MERGE', "
        "'DELETE', 'APPLY', 'REMOVE', 'EXCLUDE', 'UNEXCLUDE', 'GRANT', "
        "'REVOKE', 'SET_VISIBILITY', 'POLICY_CHANGE')",
    )
    _create_enum_if_missing(
        "tag_event_actor",
        "CREATE TYPE tag_event_actor AS ENUM ('USER', 'API_KEY', 'SYSTEM')",
    )
    _create_enum_if_missing(
        "tag_event_source",
        "CREATE TYPE tag_event_source AS ENUM ("
        "'UI', 'API', 'CLI', 'INHERITANCE', 'RECONCILER')",
    )
    _create_enum_if_missing(
        "saved_tag_filter_visibility",
        "CREATE TYPE saved_tag_filter_visibility AS ENUM ('PRIVATE', 'ORG')",
    )
    _create_enum_if_missing(
        "tag_policy_who_can_create",
        "CREATE TYPE tag_policy_who_can_create AS ENUM ('ANY_MEMBER', 'ADMIN_ONLY')",
    )
    _create_enum_if_missing(
        "tag_policy_profanity_mode",
        "CREATE TYPE tag_policy_profanity_mode AS ENUM ('ENFORCE', 'REPORT', 'OFF')",
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id              TEXT PRIMARY KEY,
            org_id          TEXT,
            key             TEXT NOT NULL,
            normalized_key  TEXT NOT NULL,
            value           TEXT,
            normalized_value TEXT,
            color           TEXT,
            description     TEXT,
            visibility      tag_visibility NOT NULL DEFAULT 'PRIVATE',
            state           tag_state      NOT NULL DEFAULT 'ACTIVE',
            merged_into_id  TEXT REFERENCES tags(id) ON DELETE SET NULL,
            owner_user_id   TEXT,
            row_version     INTEGER NOT NULL DEFAULT 1,
            created_by_user_id TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at      TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_org_normalized
        ON tags (COALESCE(org_id, ''), normalized_key, COALESCE(normalized_value, ''))
        WHERE deleted_at IS NULL AND state <> 'DELETED'
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_tags_org_state ON tags (org_id, state)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tags_org_visibility ON tags (org_id, visibility)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_assignments (
            id                   TEXT PRIMARY KEY,
            tag_id               TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            org_id               TEXT,
            scope                tag_assignment_scope NOT NULL,
            target_id            TEXT NOT NULL,
            task_id              TEXT,
            state                tag_assignment_state NOT NULL DEFAULT 'ACTIVE',
            source               tag_assignment_source NOT NULL DEFAULT 'DIRECT',
            source_experiment_id TEXT,
            source_assignment_id TEXT,
            row_version          INTEGER NOT NULL DEFAULT 1,
            assigned_by_user_id  TEXT,
            assigned_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            removed_at           TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at           TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_assignments_target
        ON tag_assignments (COALESCE(org_id, ''), tag_id, scope, target_id)
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_assignments_tag_scope_state "
        "ON tag_assignments (tag_id, scope, state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_assignments_scope_target_state "
        "ON tag_assignments (scope, target_id, state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_assignments_org_tag_state "
        "ON tag_assignments (org_id, tag_id, state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_assignments_source_experiment "
        "ON tag_assignments (source_experiment_id) "
        "WHERE source_experiment_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tag_assignments_tag_org_created_target
        ON tag_assignments (tag_id, org_id, created_at DESC, target_id)
        WHERE deleted_at IS NULL AND state = 'ACTIVE'
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_exclusions (
            id                 TEXT PRIMARY KEY,
            tag_id             TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            org_id             TEXT,
            experiment_id      TEXT NOT NULL,
            scope              tag_assignment_scope NOT NULL,
            target_id          TEXT NOT NULL,
            created_by_user_id TEXT,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at         TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_exclusions_target
        ON tag_exclusions (experiment_id, tag_id, scope, target_id)
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_exclusions_tag_id ON tag_exclusions (tag_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_grants (
            id                 TEXT PRIMARY KEY,
            tag_id             TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            org_id             TEXT,
            principal_type     tag_grant_principal NOT NULL,
            principal_user_id  TEXT,
            capability         tag_grant_capability NOT NULL,
            granted_by_user_id TEXT,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at         TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tag_grants_principal
        ON tag_grants (
            tag_id,
            principal_type,
            COALESCE(principal_user_id, ''),
            capability
        )
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_grants_tag_id ON tag_grants (tag_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_events (
            id            BIGSERIAL PRIMARY KEY,
            event_uuid    TEXT NOT NULL,
            org_id        TEXT,
            action        tag_event_action NOT NULL,
            tag_id        TEXT,
            scope         tag_assignment_scope,
            target_id     TEXT,
            actor_user_id TEXT,
            actor_type    tag_event_actor NOT NULL DEFAULT 'USER',
            source        tag_event_source NOT NULL DEFAULT 'API',
            reason        TEXT,
            payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
            occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_events_org_tag_occurred_at "
        "ON tag_events (org_id, tag_id, occurred_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_events_event_uuid "
        "ON tag_events (event_uuid)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_policies (
            org_id              TEXT PRIMARY KEY,
            max_tags_per_entity INTEGER NOT NULL DEFAULT 10,
            name_max_len        INTEGER NOT NULL DEFAULT 64,
            name_charset        TEXT    NOT NULL DEFAULT '[a-z0-9._-]',
            reserved_prefixes   TEXT[] NOT NULL DEFAULT '{}',
            who_can_create      tag_policy_who_can_create NOT NULL DEFAULT 'ANY_MEMBER',
            profanity_mode      tag_policy_profanity_mode NOT NULL DEFAULT 'ENFORCE',
            profanity_allowlist TEXT[] NOT NULL DEFAULT '{}',
            profanity_denylist  TEXT[] NOT NULL DEFAULT '{}',
            updated_by_user_id  TEXT,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_tag_project_active
        ON worker_jobs (kind, subject_table, subject_id)
        WHERE kind = 'TAG_PROJECT'
          AND status IN ('QUEUED', 'RETRYING')
          AND subject_id IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_tag_filters (
            id            TEXT PRIMARY KEY,
            org_id        TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            name          TEXT NOT NULL,
            filter_ast    JSONB NOT NULL DEFAULT '{}'::jsonb,
            visibility    saved_tag_filter_visibility NOT NULL DEFAULT 'PRIVATE',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at    TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_tag_filters_owner_name
        ON saved_tag_filters (org_id, owner_user_id, name)
        WHERE deleted_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_saved_tag_filters_org_visibility "
        "ON saved_tag_filters (org_id, visibility)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_worker_jobs_tag_project_active")
    op.execute(
        "DROP INDEX IF EXISTS idx_tag_assignments_tag_org_created_target"
    )
    for table in (
        "saved_tag_filters",
        "tag_policies",
        "tag_events",
        "tag_grants",
        "tag_exclusions",
        "tag_assignments",
        "tags",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for name in _ENUM_DROP_ORDER:
        op.execute(f"DROP TYPE IF EXISTS {name}")
