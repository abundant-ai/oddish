"""Add normalized, incrementally maintained task-browser projections.

Revision ID: task_browse_rollup_001
Revises: verdict_state_001
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "task_browse_rollup_001"
down_revision = "verdict_state_001"
branch_labels = None
depends_on = None

_SELECT_TRIALS = sa.text(
    """
    SELECT id, name, task_id, task_version_id, org_id, idempotency_key,
           superseded_by_trial_id, deleted_at, is_probe, status, reward,
           error_message, agent, model, cost_usd, input_tokens, output_tokens,
           cache_tokens, cache_write_tokens, total_steps, billed_user_id,
           started_at, finished_at, created_at
    FROM trials
    WHERE id > :after AND task_version_id IS NOT NULL
    ORDER BY id
    LIMIT 1000
    """
)
_INSERT_PROJECTION = sa.text(
    """
    INSERT INTO task_browse_trial_projection
        (trial_id, task_id, task_version_id, org_id, name, agent, model,
         model_key, status, status_bucket, reward, total_tokens, tokens_known,
         runtime_seconds, total_steps, error_message, resolved_cost_usd,
         cost_estimated, billed, is_mutable, activity_at, finished_at,
         trial_created_at,
         projected_at)
    VALUES
        (:trial_id, :task_id, :task_version_id, :org_id, :name, :agent, :model,
         :model_key, :status, :status_bucket, :reward, :total_tokens,
         :tokens_known, :runtime_seconds, :total_steps, :error_message,
         :resolved_cost_usd, :cost_estimated, :billed, :is_mutable, :activity_at,
         :finished_at, :trial_created_at, now())
    """
)
_INSERT_ROLLUPS = sa.text(
    """
    INSERT INTO task_version_browse_rollups
        (task_version_id, last_run_at, total_trials, completed_trials,
         failed_trials, reward_success, reward_sum, reward_total, total_tokens,
         runtime_total, runtime_count, cost_usd, cost_trial_count,
         cost_estimated_count, cost_native_count, billed_cost_usd,
         billed_trial_count, billed_estimated_count, billed_native_count,
         pass_count, partial_count, fail_count, harness_error_count,
         scoreless_count, skipped_count, pending_count, queued_count,
         running_count, updated_at)
    SELECT tv.id, max(p.activity_at),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status='success'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status='failed'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.reward=1),
           coalesce(sum(p.reward) FILTER (WHERE NOT p.is_mutable), 0),
           count(p.reward) FILTER (WHERE NOT p.is_mutable),
           coalesce(sum(p.total_tokens) FILTER (WHERE NOT p.is_mutable), 0),
           coalesce(sum(p.runtime_seconds) FILTER (WHERE NOT p.is_mutable), 0),
           count(p.runtime_seconds) FILTER (WHERE NOT p.is_mutable),
           coalesce(sum(p.resolved_cost_usd) FILTER (WHERE NOT p.is_mutable), 0),
           count(p.resolved_cost_usd) FILTER (WHERE NOT p.is_mutable),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.resolved_cost_usd IS NOT NULL AND p.cost_estimated),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.resolved_cost_usd IS NOT NULL AND NOT p.cost_estimated),
           coalesce(sum(p.resolved_cost_usd) FILTER (WHERE NOT p.is_mutable AND p.billed), 0),
           count(p.resolved_cost_usd) FILTER (WHERE NOT p.is_mutable AND p.billed),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.billed AND p.resolved_cost_usd IS NOT NULL AND p.cost_estimated),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.billed AND p.resolved_cost_usd IS NOT NULL AND NOT p.cost_estimated),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='pass'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='partial'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='fail'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='harness-error'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='scoreless'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='skipped'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='pending'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='queued'),
           count(p.trial_id) FILTER (WHERE NOT p.is_mutable AND p.status_bucket='running'),
           now()
    FROM task_versions tv
    LEFT JOIN task_browse_trial_projection p ON p.task_version_id=tv.id
    GROUP BY tv.id
    """
)
_INSERT_GROUPS = sa.text(
    """
    INSERT INTO task_version_browse_agent_rollups
        (task_version_id, agent, model_key, trial_count, pass_count,
         reward_sum, reward_total, token_sum, token_count, runtime_sum,
         runtime_count, steps_sum, steps_count, cost_usd, updated_at)
    SELECT task_version_id, agent, model_key, count(*),
           count(*) FILTER (WHERE status_bucket='pass'),
           coalesce(sum(reward), 0), count(reward),
           coalesce(sum(total_tokens) FILTER (WHERE tokens_known), 0),
           count(*) FILTER (WHERE tokens_known),
           coalesce(sum(runtime_seconds), 0), count(runtime_seconds),
           coalesce(sum(total_steps), 0), count(total_steps),
           coalesce(sum(resolved_cost_usd), 0), now()
    FROM task_browse_trial_projection
    WHERE NOT is_mutable
    GROUP BY task_version_id, agent, model_key
    """
)
_INSERT_METRICS = sa.text(
    """
    INSERT INTO task_version_browse_metric_values
        (task_version_id, agent, model_key, metric, value, trial_count)
    SELECT task_version_id, agent, model_key, metric, value, count(*)
    FROM task_browse_trial_projection
    CROSS JOIN LATERAL (
        VALUES ('reward', reward::double precision),
               ('runtime', runtime_seconds::double precision),
               ('tokens', CASE WHEN tokens_known THEN total_tokens::double precision END),
               ('steps', total_steps::double precision)
    ) AS metrics(metric, value)
    WHERE NOT is_mutable AND value IS NOT NULL
    GROUP BY task_version_id, agent, model_key, metric, value
    """
)
_INSERT_COSTS = sa.text(
    """
    INSERT INTO task_version_browse_cost_rollups
        (task_version_id, model_key, finished_at, cost_usd, trial_count)
    SELECT task_version_id, model_key, finished_at,
           sum(resolved_cost_usd), count(*)
    FROM task_browse_trial_projection
    WHERE NOT is_mutable
      AND finished_at IS NOT NULL
      AND resolved_cost_usd IS NOT NULL
    GROUP BY task_version_id, model_key, finished_at
    """
)


def _create_tables() -> None:
    op.create_table(
        "task_browse_trial_projection",
        sa.Column(
            "trial_id",
            sa.String(160),
            sa.ForeignKey("trials.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("model_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_bucket", sa.String(32), nullable=False),
        sa.Column("reward", sa.Float(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column("tokens_known", sa.Boolean(), nullable=False),
        sa.Column("runtime_seconds", sa.Float(), nullable=True),
        sa.Column("total_steps", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("resolved_cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_estimated", sa.Boolean(), nullable=False),
        sa.Column("billed", sa.Boolean(), nullable=False),
        sa.Column("is_mutable", sa.Boolean(), nullable=False),
        sa.Column("activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
    )
    rollup_columns = [
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        *[
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "total_trials",
                "completed_trials",
                "failed_trials",
                "reward_success",
                "reward_total",
                "runtime_count",
                "cost_trial_count",
                "cost_estimated_count",
                "cost_native_count",
                "billed_trial_count",
                "billed_estimated_count",
                "billed_native_count",
                "pass_count",
                "partial_count",
                "fail_count",
                "harness_error_count",
                "scoreless_count",
                "skipped_count",
                "pending_count",
                "queued_count",
                "running_count",
            )
        ],
        sa.Column("reward_sum", sa.Float(), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column("runtime_total", sa.Float(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("billed_cost_usd", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]
    op.create_table("task_version_browse_rollups", *rollup_columns)
    op.create_table(
        "task_version_browse_agent_rollups",
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("agent", sa.String(64), primary_key=True),
        sa.Column("model_key", sa.String(128), primary_key=True),
        sa.Column("trial_count", sa.Integer(), nullable=False),
        sa.Column("reward_sum", sa.Float(), nullable=False),
        sa.Column("reward_total", sa.Integer(), nullable=False),
        sa.Column("pass_count", sa.Integer(), nullable=False),
        sa.Column("token_sum", sa.BigInteger(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("runtime_sum", sa.Float(), nullable=False),
        sa.Column("runtime_count", sa.Integer(), nullable=False),
        sa.Column("steps_sum", sa.BigInteger(), nullable=False),
        sa.Column("steps_count", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "task_version_browse_metric_values",
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("agent", sa.String(64), primary_key=True),
        sa.Column("model_key", sa.String(128), primary_key=True),
        sa.Column("metric", sa.String(16), primary_key=True),
        sa.Column("value", sa.Float(), primary_key=True),
        sa.Column("trial_count", sa.Integer(), nullable=False),
    )
    op.create_index(
        "idx_task_browse_metric_agent",
        "task_version_browse_metric_values",
        ["task_version_id", "agent", "metric", "value"],
    )
    op.create_index(
        "idx_task_browse_metric_model",
        "task_version_browse_metric_values",
        ["task_version_id", "model_key", "metric", "value"],
    )
    op.create_table(
        "task_version_browse_cost_rollups",
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model_key", sa.String(128), primary_key=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("trial_count", sa.Integer(), nullable=False),
    )
    op.create_index(
        "idx_task_browse_cost_scope",
        "task_version_browse_cost_rollups",
        ["task_version_id", "finished_at", "model_key"],
    )
    op.create_index(
        "idx_task_browse_projection_version_preview",
        "task_browse_trial_projection",
        [
            "task_version_id",
            "is_mutable",
            sa.text("trial_created_at DESC"),
            sa.text("trial_id DESC"),
        ],
    )
    op.create_index(
        "idx_task_browse_projection_version_activity",
        "task_browse_trial_projection",
        ["task_version_id", sa.text("activity_at DESC")],
    )
    op.create_index(
        "idx_task_browse_projection_org_agent_version",
        "task_browse_trial_projection",
        ["org_id", "agent", "task_version_id"],
        postgresql_where=sa.text("NOT is_mutable"),
    )
    op.create_index(
        "idx_task_browse_projection_org_model_version",
        "task_browse_trial_projection",
        ["org_id", "model_key", "task_version_id"],
        postgresql_where=sa.text("NOT is_mutable"),
    )
    op.create_index(
        "idx_task_version_browse_rollups_last_run",
        "task_version_browse_rollups",
        [sa.text("last_run_at DESC")],
    )


def _backfill(bind: sa.engine.Connection) -> None:
    from oddish.core.task_browse_contribution import build_trial_browse_contribution

    after = ""
    while rows := list(bind.execute(_SELECT_TRIALS, {"after": after}).mappings()):
        projections = [
            item.values()
            for row in rows
            if (item := build_trial_browse_contribution(row)) is not None
        ]
        if projections:
            bind.execute(_INSERT_PROJECTION, projections)
        after = str(rows[-1]["id"])
    bind.execute(_INSERT_ROLLUPS)
    bind.execute(_INSERT_GROUPS)
    bind.execute(_INSERT_METRICS)
    bind.execute(_INSERT_COSTS)


def upgrade() -> None:
    bind = op.get_bind()
    projection_tables = {
        "task_browse_trial_projection",
        "task_version_browse_rollups",
        "task_version_browse_agent_rollups",
        "task_version_browse_metric_values",
        "task_version_browse_cost_rollups",
    }
    if not projection_tables.issubset(sa.inspect(bind).get_table_names()):
        _create_tables()
    _backfill(bind)


def downgrade() -> None:
    op.drop_table("task_version_browse_cost_rollups")
    op.drop_table("task_version_browse_metric_values")
    op.drop_table("task_version_browse_agent_rollups")
    op.drop_table("task_version_browse_rollups")
    op.drop_table("task_browse_trial_projection")
