"""add reports tables

Revision ID: r5p9q8s2t1u4
Revises: h9c0d1e2f3a4
Create Date: 2026-05-14 12:00:00.000000

Introduces the ``reports`` primitive: a curated, shareable cartesian grid
of (task_version, agent) that resolves to a trials table at read time.

Five tables capture the spec relationally so it can be queried from SQL
("which reports reference this trial?", "which pin this task_version?"):

* ``reports``                       — parent row + selection/source/share/backfill fields
* ``report_agents``                 — one row per column
* ``report_task_versions``          — one row per row
* ``report_source_experiments``     — optional source.experiment_ids scope
* ``report_cell_pins``              — explicit per-cell trial_id overrides

Plus two tables for the backfill audit trail:

* ``report_backfill_events``        — one row per /backfill/execute call
* ``report_backfill_event_trials``  — which trials each event produced

Free-form per-agent Harbor config stays as JSONB on
``report_agents.backfill_config`` since it's a pass-through to
``TaskSweepSubmission`` / ``HarborConfig``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "r5p9q8s2t1u4"
down_revision: Union[str, Sequence[str], None] = "637c45e5ac80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``000_initial_schema`` calls ``Base.metadata.create_all`` which
    # already materializes the reports tables for fresh DBs after the
    # ReportModel was added to the model graph. Bail in that case so we
    # don't re-emit the enum / table DDL.
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT to_regclass('public.reports') IS NOT NULL")
    ).scalar():
        # Tables already created by ``000_initial_schema`` /
        # ``Base.metadata.create_all`` on a fresh DB, OR by a prior run
        # of this migration. Either way, only patch in columns that
        # were added to the model graph after this migration first
        # landed — keeps preview / dev DBs from drifting when the
        # report schema iterates pre-merge. Idempotent via
        # ``ADD COLUMN IF NOT EXISTS``.
        op.execute(
            "ALTER TABLE report_task_versions "
            "ADD COLUMN IF NOT EXISTS environment VARCHAR(32)"
        )
        return

    # ---------------------------------------------------------------
    # reports
    # ---------------------------------------------------------------
    op.create_table(
        "reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column(
            "spec_version",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("trials_per_cell", sa.Integer(), nullable=False),
        sa.Column(
            "selection_strategy",
            sa.String(32),
            nullable=False,
            server_default="latest",
        ),
        sa.Column("selection_seed", sa.Integer(), nullable=True),
        sa.Column(
            "selection_tie_breaker",
            sa.String(32),
            nullable=False,
            server_default="trial_id",
        ),
        sa.Column(
            "source_include_superseded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "source_status",
            sa.dialects.postgresql.ARRAY(sa.String(16)),
            nullable=False,
            server_default=sa.text("ARRAY['success','failed']::varchar[]"),
        ),
        sa.Column(
            "backfill_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # ``priority`` is created by the earlier ``tasks.priority`` column
        # in the same Postgres enum. The labels are the member names
        # (``HIGH`` / ``LOW``), so we reuse the existing type. Use
        # ``postgresql.ENUM`` (not ``sa.Enum``) — the latter doesn't
        # propagate ``create_type=False`` reliably when compiled inside
        # ``op.create_table``, so SQLAlchemy emits a ``CREATE TYPE
        # priority`` even though the type already exists, and the
        # migration dies with DuplicateObjectError. Python-side default
        # handles inserts.
        sa.Column(
            "backfill_priority",
            postgresql.ENUM(
                "HIGH", "LOW", name="priority", create_type=False
            ),
            nullable=False,
            server_default="LOW",
        ),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("public_token", sa.String(128), nullable=True),
        sa.Column(
            "backfill_experiment_id",
            sa.String(64),
            sa.ForeignKey("experiments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trials_per_cell >= 1", name="ck_reports_trials_per_cell_pos"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_reports_public_token",
        "reports",
        ["public_token"],
        unique=True,
        if_not_exists=True,
    )
    op.create_index(
        "idx_reports_org_created_at",
        "reports",
        ["org_id", "created_at"],
        if_not_exists=True,
    )

    # ---------------------------------------------------------------
    # report_agents
    # ---------------------------------------------------------------
    op.create_table(
        "report_agents",
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("column_key", sa.String(255), nullable=False),
        sa.Column("agent", sa.String(128), nullable=False),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column(
            "backfill_config",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "report_id", "column_key", name="uq_report_agents_column_key"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_report_agents_report_id",
        "report_agents",
        ["report_id"],
        if_not_exists=True,
    )

    # ---------------------------------------------------------------
    # report_task_versions
    # ---------------------------------------------------------------
    op.create_table(
        "report_task_versions",
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column(
            "task_version_id",
            sa.String(128),
            sa.ForeignKey("task_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("environment", sa.String(32), nullable=True),
        sa.UniqueConstraint(
            "report_id",
            "task_version_id",
            name="uq_report_task_versions_task_version_id",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_report_task_versions_report_id",
        "report_task_versions",
        ["report_id"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_report_task_versions_task_version_id",
        "report_task_versions",
        ["task_version_id"],
        if_not_exists=True,
    )

    # ---------------------------------------------------------------
    # report_source_experiments
    # ---------------------------------------------------------------
    op.create_table(
        "report_source_experiments",
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "experiment_id",
            sa.String(64),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        if_not_exists=True,
    )

    # ---------------------------------------------------------------
    # report_cell_pins
    # ---------------------------------------------------------------
    op.create_table(
        "report_cell_pins",
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "task_version_id",
            sa.String(128),
            sa.ForeignKey("task_versions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("column_key", sa.String(255), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column(
            "trial_id",
            sa.String(128),
            sa.ForeignKey("trials.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["report_id", "column_key"],
            ["report_agents.report_id", "report_agents.column_key"],
            ondelete="CASCADE",
            name="fk_report_cell_pins_column",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_report_cell_pins_trial_id",
        "report_cell_pins",
        ["trial_id"],
        if_not_exists=True,
    )

    # ---------------------------------------------------------------
    # report_backfill_events
    # ---------------------------------------------------------------
    op.create_table(
        "report_backfill_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("initiated_by_user_id", sa.String(64), nullable=True),
        sa.Column("initiated_by_api_key_id", sa.String(64), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "initiated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "plan_snapshot",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("est_cost_usd_total", sa.Float(), nullable=True),
        sa.Column(
            "trials_submitted",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="submitted",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.create_index(
        "idx_report_backfill_events_report_initiated",
        "report_backfill_events",
        ["report_id", "initiated_at"],
        if_not_exists=True,
    )

    # ---------------------------------------------------------------
    # report_backfill_event_trials
    # ---------------------------------------------------------------
    op.create_table(
        "report_backfill_event_trials",
        sa.Column(
            "event_id",
            sa.String(64),
            sa.ForeignKey(
                "report_backfill_events.id", ondelete="CASCADE"
            ),
            primary_key=True,
        ),
        sa.Column(
            "trial_id",
            sa.String(128),
            sa.ForeignKey("trials.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_report_backfill_event_trials_trial_id",
        "report_backfill_event_trials",
        ["trial_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_report_backfill_event_trials_trial_id",
        table_name="report_backfill_event_trials",
    )
    op.drop_table("report_backfill_event_trials")

    op.drop_index(
        "idx_report_backfill_events_report_initiated",
        table_name="report_backfill_events",
    )
    op.drop_table("report_backfill_events")

    op.drop_index(
        "idx_report_cell_pins_trial_id", table_name="report_cell_pins"
    )
    op.drop_table("report_cell_pins")

    op.drop_table("report_source_experiments")

    op.drop_index(
        "idx_report_task_versions_task_version_id",
        table_name="report_task_versions",
    )
    op.drop_index(
        "idx_report_task_versions_report_id", table_name="report_task_versions"
    )
    op.drop_table("report_task_versions")

    op.drop_index("idx_report_agents_report_id", table_name="report_agents")
    op.drop_table("report_agents")

    op.drop_index("idx_reports_org_created_at", table_name="reports")
    op.drop_index("idx_reports_public_token", table_name="reports")
    op.drop_table("reports")
