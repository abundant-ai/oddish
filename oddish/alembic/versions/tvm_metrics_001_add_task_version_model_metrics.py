"""Add per-(task version, agent, model) trial metrics.

Sibling of ``task_version_browse_summaries`` one grain finer, so pass rate and
trajectory length can be selected per model instead of only filtered on.
Backfill is deliberately NOT done here -- the table is inert until
``backfill_task_version_model_metrics`` populates it, which keeps this
migration fast on a hot ``trials`` table.

Revision ID: tvm_metrics_001
Revises: task_browse_summary_002
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "tvm_metrics_001"
down_revision: Union[str, Sequence[str], None] = "task_browse_summary_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "task_version_model_metrics"


def _table_exists(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _index_exists(table: str, name: str) -> bool:
    return name in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)
    }


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("task_version_id", sa.String(160), nullable=False),
            sa.Column("agent", sa.String(128), nullable=False),
            # NULL model is a real state on older trials; the empty string keeps
            # it inside the primary key without inventing a fake model name.
            sa.Column("model", sa.String(256), nullable=False, server_default=""),
            sa.Column("task_id", sa.String(128), nullable=False),
            # scored population
            _counter("n_pass"),
            _counter("n_partial"),
            _counter("n_fail"),
            # terminal but unscored, attributed by harbor_stage
            _counter("n_unscored_agent"),
            _counter("n_unscored_env"),
            _counter("n_unscored_verify"),
            _counter("n_cancelled_user"),
            _counter("n_cancelled_reaped"),
            _counter("n_cancelled_other"),
            _counter("n_skipped"),
            _counter("n_scoreless"),
            _counter("n_unscored_unknown"),
            # never in any denominator
            _counter("n_inflight"),
            # metric sums
            sa.Column(
                "sum_reward", sa.Float(), nullable=False, server_default="0"
            ),
            _counter("n_reward_present"),
            sa.Column(
                "sum_runtime", sa.Float(), nullable=False, server_default="0"
            ),
            _counter("n_runtime_present"),
            _counter("n_with_trajectory"),
            # step distribution -- nullable: NULL means "never measured", which
            # is distinct from a measured zero. See spec 4.6.
            _counter("n_steps_present"),
            sa.Column("sum_steps", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("steps_all_min", sa.Integer()),
            sa.Column("steps_all_p50", sa.Integer()),
            sa.Column("steps_all_max", sa.Integer()),
            _counter("steps_pass_n"),
            sa.Column("steps_pass_min", sa.Integer()),
            sa.Column("steps_pass_p50", sa.Integer()),
            sa.Column("steps_pass_max", sa.Integer()),
            _counter("steps_fail_n"),
            sa.Column("steps_fail_min", sa.Integer()),
            sa.Column("steps_fail_p50", sa.Integer()),
            sa.Column("steps_fail_max", sa.Integer()),
            _counter("steps_partial_n"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.PrimaryKeyConstraint("task_version_id", "agent", "model"),
            sa.ForeignKeyConstraint(
                ["task_version_id"], ["task_versions.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        )

    # Ratios are computed at read time rather than stored: Postgres generated
    # columns cannot reference other generated columns, and every denominator
    # here is a sum of counters that a partial index can cover just as well.
    if not _index_exists(_TABLE, "idx_tvm_metrics_model"):
        op.create_index("idx_tvm_metrics_model", _TABLE, ["model", "agent"])
    if not _index_exists(_TABLE, "idx_tvm_metrics_task_id"):
        op.create_index("idx_tvm_metrics_task_id", _TABLE, ["task_id"])
    # Anomaly triage (spec 4.7) seeks on a low passing-step minimum.
    if not _index_exists(_TABLE, "idx_tvm_metrics_steps_pass_min"):
        op.create_index(
            "idx_tvm_metrics_steps_pass_min",
            _TABLE,
            ["steps_pass_min"],
            postgresql_where=sa.text("steps_pass_n > 0"),
        )


def downgrade() -> None:
    op.drop_index("idx_tvm_metrics_steps_pass_min", table_name=_TABLE)
    op.drop_index("idx_tvm_metrics_task_id", table_name=_TABLE)
    op.drop_index("idx_tvm_metrics_model", table_name=_TABLE)
    op.drop_table(_TABLE)
