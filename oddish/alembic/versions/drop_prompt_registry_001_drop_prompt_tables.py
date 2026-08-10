"""Drop the DB prompt registry and the features built on it.

Prompts are no longer stored in the database: the built-in analyzer prompts
(pre-trial audit, post-trial classifier, trajectory summary, verdict) now come
solely from the files packaged under ``oddish/analyze/``. That removes the
``prompts`` / ``prompt_versions`` registry and its two dependent features —
custom QA runs (``analyzer_runs``) and QA assignments (``qa_assignments``).

In-flight ``ANALYZER_BLOCK`` worker jobs are retired first: their subject
table (``analyzer_runs``) is dropped here, so a row that drained after this
revision would only crash. The ``ANALYZER_BLOCK`` value stays in the
``worker_job_kind`` enum (removing a Postgres enum value needs a type
rebuild, and historical terminal rows still reference it).

The ``analyzer_blocks.prompt`` / ``prompt_key`` / ``prompt_version`` /
``prompt_id`` lineage columns are kept: they record what a finished block ran
with, and historical rows keep their attribution.

Every statement is inspector-guarded because ``000_initial_schema`` bootstraps
a fresh database with ``Base.metadata.create_all()`` against the live model
graph — on such a database these tables never existed.

``downgrade()`` is intentionally a no-op: the registry contents are data, not
schema, and the feature's code is gone. Rolling back requires the code revert
and a restore from backup.

Revision ID: drop_prompt_registry_001
Revises: trial_facets_001
"""

from alembic import op
import sqlalchemy as sa


revision = "drop_prompt_registry_001"
down_revision = "trial_facets_001"
branch_labels = None
depends_on = None


def _tables(bind: sa.engine.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "worker_jobs" in tables:
        op.execute(
            sa.text(
                """
                UPDATE worker_jobs
                SET status = 'CANCELLED',
                    finished_at = NOW(),
                    error_message = 'Retired: the DB prompt registry and '
                                    'analyzer_runs were removed'
                WHERE kind = 'ANALYZER_BLOCK'
                  AND status IN ('QUEUED', 'RUNNING', 'RETRYING', 'BLOCKED')
                """
            )
        )

    # FK order: analyzer_runs references prompt_versions and qa_assignments;
    # qa_assignments references prompts; prompt_versions references prompts.
    for table in ("analyzer_runs", "qa_assignments", "prompt_versions", "prompts"):
        if table in tables:
            op.drop_table(table)


def downgrade() -> None:
    pass
