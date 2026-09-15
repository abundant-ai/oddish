"""Convert the retired finding category to must_fix, including saved snapshots.

Only severity fields change. Finding IDs, evidence, acknowledgments, and
historical verdicts remain intact. Normalize writes from older workers too.
"""

from alembic import op

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


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION normalize_finding_tiers(payload jsonb) RETURNS jsonb
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
    """)
    for table, columns in FINDING_COLUMNS.items():
        # Avoid rebuilding wide JSON for the overwhelmingly common clean row.
        assignments = ", ".join(
            f"{column} = normalize_finding_tiers({column})" for column in columns
        )
        predicate = " OR ".join(
            f"{column}::text LIKE '%should_fix%'" for column in columns
        )
        op.execute(f"UPDATE {table} SET {assignments} WHERE {predicate}")
        body = "\n".join(
            f"IF NEW.{column}::text LIKE '%should_fix%' THEN "
            f"NEW.{column} := normalize_finding_tiers(NEW.{column}); END IF;"
            for column in columns
        )
        op.execute(f"""
            CREATE FUNCTION normalize_{table}_finding_tiers() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                {body}
                RETURN NEW;
            END $$
        """)
        op.execute(f"""
            CREATE TRIGGER normalize_finding_tiers
            BEFORE INSERT OR UPDATE OF {", ".join(columns)} ON {table}
            FOR EACH ROW EXECUTE FUNCTION normalize_{table}_finding_tiers()
        """)


def downgrade() -> None:
    raise RuntimeError(
        "Converted finding severities cannot be distinguished; use a forward migration."
    )
