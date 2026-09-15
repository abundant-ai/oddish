"""Convert the retired finding category to must_fix, including saved snapshots.

Only severity fields change. Finding IDs, evidence, acknowledgments, and
historical verdicts remain intact. Normalize writes from older workers too.
"""

from alembic import op
from sqlalchemy import text

revision = "merge_finding_tiers_001"
down_revision = "worker_resources_001"
branch_labels = None
depends_on = None

# These columns own source audits, retained findings, run/probe analyses,
# task verdict findings, and the frozen copies shown in delivery history.
FINDING_COLUMNS = {
    "task_versions": ("pre_trial", "reported_findings"),
    "trials": ("analysis",),
    "tasks": ("verdict",),
    "delivery_snapshots": ("snapshot",),
}


BATCH_SIZE = 500


def upgrade() -> None:
    # Each DDL statement and data batch commits independently. A failed upgrade
    # can be retried: functions/triggers are replaced and conversion is idempotent.
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous_lock_timeout = connection.execute(
            text("SHOW lock_timeout")
        ).scalar_one()
        connection.execute(text("SET lock_timeout = '5s'"))
        try:
            _install_triggers()
            _convert_history()
        finally:
            connection.execute(
                text("SELECT set_config('lock_timeout', :value, false)"),
                {"value": previous_lock_timeout},
            )


def _install_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION normalize_finding_tiers(payload jsonb) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        DECLARE result jsonb;
        BEGIN
            IF jsonb_typeof(payload) = 'object' THEN
                SELECT COALESCE(jsonb_object_agg(key,
                    CASE WHEN key IN ('tier', 'severity', 'priority', 'recorded_tier')
                              AND value = '"should_fix"'::jsonb
                         THEN '"must_fix"'::jsonb
                         ELSE normalize_finding_tiers(value) END), '{}'::jsonb)
                INTO result FROM jsonb_each(payload)
                WHERE key <> 'pre_trial_should_fix';
                RETURN result;
            ELSIF jsonb_typeof(payload) = 'array' THEN
                SELECT COALESCE(jsonb_agg(normalize_finding_tiers(value)
                    ORDER BY position), '[]'::jsonb)
                INTO result FROM jsonb_array_elements(payload)
                    WITH ORDINALITY AS items(value, position);
                RETURN result;
            END IF;
            RETURN payload;
        END $$
    """
    )
    for table, columns in FINDING_COLUMNS.items():
        body = "\n".join(
            f"IF NEW.{column}::text LIKE '%should_fix%' THEN "
            f"NEW.{column} := normalize_finding_tiers(NEW.{column}); END IF;"
            for column in columns
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION normalize_{table}_finding_tiers() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                {body}
                RETURN NEW;
            END $$
        """
        )
        op.execute(
            f"""
            CREATE OR REPLACE TRIGGER normalize_finding_tiers
            BEFORE INSERT OR UPDATE OF {", ".join(columns)} ON {table}
            FOR EACH ROW EXECUTE FUNCTION normalize_{table}_finding_tiers()
        """
        )


def _convert_history() -> None:
    connection = op.get_bind()
    for table, columns in FINDING_COLUMNS.items():
        assignments = ", ".join(
            f"{column} = normalize_finding_tiers(target.{column})" for column in columns
        )
        predicate = " OR ".join(
            f"batch.{column}::text LIKE '%should_fix%'" for column in columns
        )
        last_id = None
        while True:
            # Limit rows by primary key BEFORE inspecting JSON, so clean history
            # also takes bounded transactions. New writes are already normalized
            # by the committed triggers, even if their IDs precede this cursor.
            after = "WHERE id > :last_id" if last_id is not None else ""
            last_id = connection.execute(
                text(
                    f"""
                    WITH batch AS MATERIALIZED (
                        SELECT id, {", ".join(columns)} FROM {table} {after}
                        ORDER BY id LIMIT :batch_size
                    ), converted AS (
                        UPDATE {table} AS target SET {assignments}
                        FROM batch
                        WHERE target.id = batch.id AND ({predicate})
                        RETURNING target.id
                    )
                    SELECT max(id) FROM batch
                """
                ),
                {"last_id": last_id, "batch_size": BATCH_SIZE},
            ).scalar_one()
            if last_id is None:
                break


def downgrade() -> None:
    raise RuntimeError(
        "Converted finding severities cannot be distinguished; use a forward migration."
    )
