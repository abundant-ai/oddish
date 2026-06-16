"""add cloud FK constraints from tag tables to orgs/users

In cloud deployments we promote the plain TEXT ``org_id`` / ``*_user_id``
columns on the new tag tables into validated foreign keys. ``NOT VALID``
lets the constraint go live without scanning the existing (empty) rows;
the constraint behaves identically to a validated FK for new INSERT/UPDATE.

Revision ID: n0p1q2r3s4t5
Revises: m9n0p1q2r3s4
Create Date: 2026-06-06 12:20:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "n0p1q2r3s4t5"
down_revision: Union[str, Sequence[str], None] = "m9n0p1q2r3s4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FKS = [
    (
        "tags",
        "fk_tags_org",
        "FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    ),
    (
        "tags",
        "fk_tags_owner_user",
        "FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    (
        "tags",
        "fk_tags_created_by_user",
        "FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    (
        "tag_assignments",
        "fk_tag_assignments_org",
        "FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    ),
    (
        "tag_assignments",
        "fk_tag_assignments_assigned_by_user",
        "FOREIGN KEY (assigned_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    (
        "tag_exclusions",
        "fk_tag_exclusions_org",
        "FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    ),
    (
        "tag_exclusions",
        "fk_tag_exclusions_created_by_user",
        "FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    (
        "tag_grants",
        "fk_tag_grants_org",
        "FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    ),
    (
        "tag_grants",
        "fk_tag_grants_principal_user",
        "FOREIGN KEY (principal_user_id) REFERENCES users(id) ON DELETE CASCADE",
    ),
    (
        "tag_grants",
        "fk_tag_grants_granted_by_user",
        "FOREIGN KEY (granted_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    (
        "tag_events",
        "fk_tag_events_org",
        "FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    ),
    (
        "tag_events",
        "fk_tag_events_actor_user",
        "FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    (
        "tag_policies",
        "fk_tag_policies_org",
        "FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    ),
    (
        "tag_policies",
        "fk_tag_policies_updated_by_user",
        "FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    (
        "saved_tag_filters",
        "fk_saved_tag_filters_org",
        "FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE",
    ),
    (
        "saved_tag_filters",
        "fk_saved_tag_filters_owner_user",
        "FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE",
    ),
]


def upgrade() -> None:
    for table, name, body in _FKS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE {table}
                        ADD CONSTRAINT {name} {body} NOT VALID;
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    for table, name, _ in reversed(_FKS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
