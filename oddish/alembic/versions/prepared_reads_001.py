"""Persist dashboard summaries and transactionally record dependent changes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "prepared_reads_001"
down_revision = "merge_finding_tiers_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiment_summaries",
        sa.Column(
            "experiment_id",
            sa.String(64),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "last_dirty_txid",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("txid_current()"),
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "built_revision", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "dirty_since",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_experiment_summaries_pending",
        "experiment_summaries",
        ["next_attempt_at", "dirty_since"],
        postgresql_where=sa.text("revision > built_revision"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_experiment_summaries_reconcile",
        "experiment_summaries",
        ["refreshed_at"],
        if_not_exists=True,
    )
    op.execute("""
        CREATE FUNCTION dirty_experiment_summary(eid text) RETURNS void LANGUAGE sql AS $$
            INSERT INTO experiment_summaries (experiment_id)
            SELECT id FROM experiments WHERE id = eid
            ON CONFLICT (experiment_id) DO UPDATE SET
                revision = experiment_summaries.revision + 1,
                last_dirty_txid = txid_current(),
                dirty_since = CASE WHEN experiment_summaries.revision = experiment_summaries.built_revision
                    THEN now() ELSE experiment_summaries.dirty_since END
            WHERE experiment_summaries.last_dirty_txid IS DISTINCT FROM txid_current()
        $$
    """)
    op.execute("""
        CREATE FUNCTION record_experiment_summary_change() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE item jsonb; eid text; taskid text; trialid text;
        BEGIN
            -- OLD and NEW both matter for moves, soft deletion, and restoration.
            FOR item IN SELECT DISTINCT value FROM jsonb_array_elements(
                jsonb_build_array(CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
                                  CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END))
                WHERE value <> 'null'::jsonb
            LOOP
                IF TG_TABLE_NAME = 'experiments' THEN
                    PERFORM dirty_experiment_summary(item->>'id');
                    PERFORM dirty_experiment_summary(item->>'shadow_of');
                ELSIF TG_TABLE_NAME IN ('task_experiments', 'experiment_trials') THEN
                    PERFORM dirty_experiment_summary(item->>'experiment_id');
                ELSE
                    taskid := CASE WHEN TG_TABLE_NAME = 'tasks' THEN item->>'id' ELSE item->>'task_id' END;
                    trialid := CASE WHEN TG_TABLE_NAME = 'trials' THEN item->>'id' ELSE NULL END;
                    FOR eid IN
                        SELECT experiment_id FROM task_experiments WHERE task_id = taskid AND deleted_at IS NULL
                        UNION SELECT experiment_id FROM trials WHERE task_id = taskid AND deleted_at IS NULL
                            AND TG_TABLE_NAME <> 'trials'
                        UNION SELECT et.experiment_id FROM experiment_trials et JOIN trials t ON t.id = et.trial_id
                            WHERE et.deleted_at IS NULL AND t.task_id = taskid AND TG_TABLE_NAME <> 'trials'
                        UNION SELECT item->>'experiment_id' WHERE TG_TABLE_NAME = 'trials'
                        UNION SELECT experiment_id FROM experiment_trials WHERE trial_id = trialid AND deleted_at IS NULL
                        ORDER BY 1
                    LOOP
                        PERFORM dirty_experiment_summary(eid);
                    END LOOP;
                END IF;
            END LOOP;
            RETURN NULL;
        END $$
    """)
    for table, columns in DEPENDENCIES.items():
        update = "UPDATE OF " + ", ".join(f'"{column}"' for column in columns)
        op.execute(
            f"CREATE TRIGGER experiment_summary_changed AFTER INSERT OR DELETE OR {update} ON {table} FOR EACH ROW EXECUTE FUNCTION record_experiment_summary_change()"
        )
    op.execute(
        "INSERT INTO experiment_summaries (experiment_id) SELECT id FROM experiments ON CONFLICT DO NOTHING"
    )


# Only fields consumed by the summary calculation. Heartbeat/token checkpoints
# must not repeatedly dirty experiments while leaving every visible count intact.
DEPENDENCIES = {
    "experiments": [
        "name",
        "owner",
        "owner_user_id",
        "link",
        "is_public",
        "shadow_of",
        "deleted_at",
        "org_id",
        "last_activity_at",
    ],
    "tasks": [
        "created_at",
        "user",
        "tags",
        "link",
        "created_by_user_id",
        "current_version_id",
        "run_analysis",
        "verdict",
        "verdict_status",
        "deleted_at",
        "org_id",
    ],
    "task_versions": ["version", "task_id", "deleted_at"],
    "trials": [
        "created_at",
        "experiment_id",
        "task_id",
        "task_version_id",
        "status",
        "reward",
        "billed_user_id",
        "superseded_by_trial_id",
        "kind",
        "is_probe",
        "agent",
        "deleted_at",
        "org_id",
    ],
    "task_experiments": ["task_id", "experiment_id", "deleted_at"],
    "experiment_trials": ["trial_id", "experiment_id", "deleted_at"],
}


def downgrade():
    for table in DEPENDENCIES:
        op.execute(f"DROP TRIGGER experiment_summary_changed ON {table}")
    op.execute("DROP FUNCTION record_experiment_summary_change()")
    op.execute("DROP FUNCTION dirty_experiment_summary(text)")
    op.drop_table("experiment_summaries")
